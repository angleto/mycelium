"""An agent accepts a suggestion only on a decision a person took (task f49138f7).

The plain accept stays out of an assistant credential's reach: agents
propose, people dispose. ``accept_suggestion_on_decision`` is how an agent
carries out a decision already taken, and everything here is about the
one property that makes it more than a formality: the server, not the
agent, establishes that a person decided. Each refusal below is a way an
agent could otherwise have approved itself.

Sessions are opened with the actor kind the transports publish: a person's
own bearer over REST is ``human_api`` with no subject, an agent token over
MCP is ``mcp_token`` with the token id as subject.
"""

from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.models.annotation import Annotation
from mycelium_core.models.note import NoteKind
from mycelium_core.models.task import Task
from mycelium_core.services import annotations as anno
from mycelium_core.services import identities
from mycelium_core.services import note_parts as np
from mycelium_core.services import notes as nt
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup


class _World:
    def __init__(self, org: uuid.UUID, user: uuid.UUID, agent: uuid.UUID) -> None:
        self.org, self.user, self.agent = org, user, agent
        self.token = uuid.uuid4()

    def person(self) -> AbstractAsyncContextManager[AsyncSession]:
        return tenant_session(str(self.org), str(self.user), actor_kind="human_api")

    def through_agent(self) -> AbstractAsyncContextManager[AsyncSession]:
        return tenant_session(
            str(self.org),
            str(self.user),
            actor_kind="mcp_token",
            actor_subject_id=str(self.token),
        )


async def _world() -> _World:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="DECIDE",
        )
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        a = AiAssistant(
            org_id=r.org_id,
            user_id=r.user_id,
            label="Claude",
            handle=f"claude-{uuid.uuid4().hex[:8]}",
            scope=["comments:write", "tasks:write", "notes:write"],
            is_active=True,
        )
        s.add(a)
        await s.flush()
        ident = await identities.ensure_for_ai_assistant(s, org_id=r.org_id, assistant_id=a.id)
    return _World(r.org_id, r.user_id, ident.id)


async def _task_with_suggestion(w: _World) -> tuple[uuid.UUID, Annotation]:
    async with w.person() as s:
        t = await tasks_svc.create_task(
            s, org_id=w.org, actor_id=w.user, title="t", description="ship on friday"
        )
    async with w.through_agent() as s:
        sug = await anno.propose_suggestion(
            s,
            org_id=w.org,
            actor_id=w.user,
            doc_kind="task_description",
            doc_id=t.id,
            original_text="friday",
            proposed_text="monday",
            author_identity_id=w.agent,
        )
    return t.id, sug


async def _comment(
    w: _World,
    doc_id: uuid.UUID,
    body: str,
    *,
    by_agent: bool = False,
    author: uuid.UUID | None = None,
    doc_kind: str = "task_description",
) -> Annotation:
    async with w.through_agent() if by_agent else w.person() as s:
        return await anno.create_comment(
            s,
            org_id=w.org,
            actor_id=w.user,
            doc_kind=doc_kind,
            doc_id=doc_id,
            body=body,
            author_identity_id=author,
        )


async def _execute(w: _World, sug: Annotation, decision: uuid.UUID) -> int:
    async with w.through_agent() as s:
        return await anno.accept_suggestion_on_decision(
            s,
            org_id=w.org,
            actor_id=w.user,
            annotation_id=sug.id,
            expected_version=sug.version,
            decision_comment_id=decision,
            executed_by_identity_id=w.agent,
            granted_scopes=["comments:write", "tasks:write"],
        )


async def _refused(w: _World, sug: Annotation, decision: uuid.UUID) -> str:
    with pytest.raises(DomainError) as e:
        await _execute(w, sug, decision)
    assert e.value.code == MessageCode.SUGGESTION_DECISION_INVALID
    reason = e.value.params["reason"]
    assert isinstance(reason, str)
    return reason


async def _description(w: _World, task_id: uuid.UUID) -> str:
    async with w.person() as s:
        t = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        return t.description or ""


async def test_a_persons_decision_is_carried_out_and_both_roles_are_recorded() -> None:
    w = await _world()
    task_id, sug = await _task_with_suggestion(w)
    dec = await _comment(w, task_id, f"looks right\naccept {sug.id.hex[:8]}")

    await _execute(w, sug, dec.id)

    assert await _description(w, task_id) == "ship on monday"
    async with w.person() as s:
        row = (await s.execute(select(Annotation).where(Annotation.id == sug.id))).scalar_one()
        audit = (
            await s.execute(
                select(ActivityLog).where(
                    ActivityLog.entity_id == sug.id, ActivityLog.action == "accept"
                )
            )
        ).scalar_one()
    assert row.status == "accepted"
    assert row.resolved_by_identity_id == w.agent
    assert row.decided_by_identity_id == dec.author_identity_id
    assert row.decided_by_identity_id != w.agent
    assert row.decision_comment_id == dec.id
    assert audit.diff is not None
    assert audit.diff["decision_comment_id"] == str(dec.id)
    assert audit.diff["executed_by_identity_id"] == str(w.agent)


async def test_an_agents_own_comment_is_not_a_decision() -> None:
    w = await _world()
    task_id, sug = await _task_with_suggestion(w)
    dec = await _comment(w, task_id, f"accept {sug.id}", by_agent=True, author=w.agent)
    assert await _refused(w, sug, dec.id) == "author_not_a_person"
    assert await _description(w, task_id) == "ship on friday"


async def test_a_comment_signed_as_the_person_but_written_through_an_agent_is_refused() -> None:
    """The forge the author column alone would let through: an agent token
    with no assistant bound writes as the person's own identity."""
    w = await _world()
    task_id, sug = await _task_with_suggestion(w)
    dec = await _comment(w, task_id, f"accept {sug.id}", by_agent=True)
    assert await _refused(w, sug, dec.id) == "written_through_an_agent"


async def test_a_persons_comment_rewritten_by_an_agent_is_refused() -> None:
    """An agent on an owner's token passes the admin override on edit, so
    it can turn the owner's comment into an accept. Every write counts."""
    w = await _world()
    task_id, sug = await _task_with_suggestion(w)
    dec = await _comment(w, task_id, "let me think about it")
    async with w.through_agent() as s:
        await anno.edit(
            s,
            org_id=w.org,
            actor_id=w.user,
            annotation_id=dec.id,
            body=f"accept {sug.id}",
            expected_version=dec.version,
            actor_identity_id=w.agent,
        )
    assert await _refused(w, sug, dec.id) == "written_through_an_agent"


async def test_the_decision_must_name_the_suggestion_on_an_accept_line() -> None:
    w = await _world()
    task_id, sug = await _task_with_suggestion(w)
    for body in (
        "yes, go ahead",
        f"do not accept {sug.id.hex[:8]}",
        f"accept {sug.id.hex[:7]}",
        f"accept {uuid.uuid4().hex[:8]}",
    ):
        dec = await _comment(w, task_id, body)
        assert await _refused(w, sug, dec.id) == "suggestion_not_named", body


async def test_a_decision_cannot_predate_what_it_decides() -> None:
    w = await _world()
    async with w.person() as s:
        t = await tasks_svc.create_task(
            s, org_id=w.org, actor_id=w.user, title="t", description="ship on friday"
        )
    early = await _comment(w, t.id, "accept everything")
    async with w.through_agent() as s:
        sug = await anno.propose_suggestion(
            s,
            org_id=w.org,
            actor_id=w.user,
            doc_kind="task_description",
            doc_id=t.id,
            original_text="friday",
            proposed_text="monday",
            author_identity_id=w.agent,
        )
    # Even an edit that adds the id later does not move its creation.
    async with w.person() as s:
        await anno.edit(
            s,
            org_id=w.org,
            actor_id=w.user,
            annotation_id=early.id,
            body=f"accept {sug.id}",
            expected_version=early.version,
        )
    assert await _refused(w, sug, early.id) == "before_suggestion"


async def test_a_decision_on_another_document_is_refused() -> None:
    w = await _world()
    _task_id, sug = await _task_with_suggestion(w)
    async with w.person() as s:
        other = await tasks_svc.create_task(s, org_id=w.org, actor_id=w.user, title="other")
    dec = await _comment(w, other.id, f"accept {sug.id}")
    assert await _refused(w, sug, dec.id) == "other_document"


async def test_a_prefix_that_names_two_suggestions_is_refused() -> None:
    w = await _world()
    task_id, sug = await _task_with_suggestion(w)
    twin = uuid.UUID(sug.id.hex[:8] + uuid.uuid4().hex[8:])
    async with w.person() as s:
        s.add(
            Annotation(
                id=twin,
                org_id=w.org,
                doc_kind="task_description",
                task_id=task_id,
                kind="suggestion",
                body="",
                original_text="ship",
                proposed_text="send",
                author_identity_id=w.agent,
            )
        )
    dec = await _comment(w, task_id, f"accept {sug.id.hex[:8]}")
    assert await _refused(w, sug, dec.id) == "ambiguous_id"


async def test_one_comment_decides_several_suggestions_across_a_notes_parts() -> None:
    w = await _world()
    async with w.person() as s:
        note = await nt.create_note(
            s, org_id=w.org, actor_id=w.user, kind=NoteKind.text, text="alpha one"
        )
        second = await np.create_part(
            s, org_id=w.org, actor_id=w.user, note_id=note.id, body="beta two"
        )
        parts = await np.list_parts(s, org_id=w.org, note_id=note.id)
    first_id = next(p.id for p in parts if p.id != second.id)
    sugs = []
    async with w.through_agent() as s:
        for part_id, old, new in ((first_id, "one", "1"), (second.id, "two", "2")):
            sugs.append(
                await anno.propose_suggestion(
                    s,
                    org_id=w.org,
                    actor_id=w.user,
                    doc_kind="note_part",
                    doc_id=part_id,
                    original_text=old,
                    proposed_text=new,
                    author_identity_id=w.agent,
                )
            )
    dec = await _comment(
        w,
        first_id,
        f"accept {sugs[0].id.hex[:8]} {sugs[1].id}",
        doc_kind="note_part",
    )
    for sug in sugs:
        async with w.through_agent() as s:
            await anno.accept_suggestion_on_decision(
                s,
                org_id=w.org,
                actor_id=w.user,
                annotation_id=sug.id,
                expected_version=sug.version,
                decision_comment_id=dec.id,
                executed_by_identity_id=w.agent,
                granted_scopes=None,
            )
    async with w.person() as s:
        bodies = {p.id: p.body for p in await np.list_parts(s, org_id=w.org, note_id=note.id)}
    assert bodies == {first_id: "alpha 1", second.id: "beta 2"}


async def test_a_decided_suggestion_still_needs_write_on_the_document() -> None:
    w = await _world()
    task_id, sug = await _task_with_suggestion(w)
    dec = await _comment(w, task_id, f"accept {sug.id}")
    with pytest.raises(Exception) as e:
        async with w.through_agent() as s:
            await anno.accept_suggestion_on_decision(
                s,
                org_id=w.org,
                actor_id=w.user,
                annotation_id=sug.id,
                expected_version=sug.version,
                decision_comment_id=dec.id,
                executed_by_identity_id=w.agent,
                granted_scopes=["comments:write"],
            )
    assert getattr(e.value, "code", None) == MessageCode.AGENT_SCOPE_DENIED
    assert await _description(w, task_id) == "ship on friday"


def test_the_agent_reaches_this_tool_and_still_not_the_plain_accept() -> None:
    from mycelium_mcp.server import _PRINCIPAL_SCOPE, _scope_permits

    tok = _PRINCIPAL_SCOPE.set(["comments:write", "notes:write"])
    try:
        assert _scope_permits("accept_suggestion_on_decision")
        assert not _scope_permits("accept_suggestion")
    finally:
        _PRINCIPAL_SCOPE.reset(tok)
