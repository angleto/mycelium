"""Instructions to an agent: a comment a person assigns to an assistant (task 0d323273).

There is no instruction kind. A comment says what to change, its assignee says
who acts, and when the author is a person and the assignee an assistant the
comment is an instruction. Two properties make it usable for review:

- a suggestion names the instruction it carries out (``instruction_id``), so
  a reviewer reads each edit next to what was asked;
- the person keeps the acts that close or rewrite the instruction. Through an
  assistant credential, resolve, reassign, edit, delete, restore and purge are
  refused, otherwise the agent could close its own work or move the yardstick
  it is checked against.

Sessions carry the actor kind the transports publish: a person's own bearer
is ``human_api`` with no subject, an agent token over MCP is ``mcp_token``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from contextlib import AbstractAsyncContextManager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError, ForbiddenError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.models.annotation import Annotation
from mycelium_core.models.note import NoteKind
from mycelium_core.services import annotations as anno
from mycelium_core.services import entity_revisions as revisions
from mycelium_core.services import identities
from mycelium_core.services import note_parts as np
from mycelium_core.services import notes as nt
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup


class _World:
    def __init__(self, org: uuid.UUID, user: uuid.UUID, me: uuid.UUID, agent: uuid.UUID) -> None:
        self.org, self.user, self.me, self.agent = org, user, me, agent
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
            org_name="INSTRUCT",
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
        agent = await identities.ensure_for_ai_assistant(s, org_id=r.org_id, assistant_id=a.id)
        me = await identities.ensure_for_user(s, org_id=r.org_id, user_id=r.user_id)
    return _World(r.org_id, r.user_id, me.id, agent.id)


async def _task(w: _World, description: str = "ship on friday, then rest") -> uuid.UUID:
    async with w.person() as s:
        t = await tasks_svc.create_task(
            s, org_id=w.org, actor_id=w.user, title="t", description=description
        )
    return t.id


async def _instruction(
    w: _World, doc_id: uuid.UUID, *, doc_kind: str = "task_description"
) -> Annotation:
    """A person's comment on 'friday', assigned to the agent."""
    async with w.person() as s:
        return await anno.create_comment(
            s,
            org_id=w.org,
            actor_id=w.user,
            doc_kind=doc_kind,
            doc_id=doc_id,
            body="make the day concrete",
            anchor_quote="friday",
            assignee_identity_id=w.agent,
        )


async def _propose(
    w: _World,
    doc_id: uuid.UUID,
    instruction_id: uuid.UUID | None,
    *,
    doc_kind: str = "task_description",
    original: str = "friday",
) -> Annotation:
    async with w.through_agent() as s:
        return await anno.propose_suggestion(
            s,
            org_id=w.org,
            actor_id=w.user,
            doc_kind=doc_kind,
            doc_id=doc_id,
            original_text=original,
            proposed_text="friday 30 October",
            author_identity_id=w.agent,
            instruction_id=instruction_id,
        )


async def _fresh(w: _World, ann_id: uuid.UUID) -> Annotation:
    async with w.person() as s:
        row = (await s.execute(select(Annotation).where(Annotation.id == ann_id))).scalar_one()
        s.expunge(row)
        return row


# --------------------------------------------------------------------------
# the link from a suggestion to its instruction
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_instruction_is_assigned_at_creation_and_answered_by_a_linked_suggestion() -> None:
    w = await _world()
    t = await _task(w)
    ins = await _instruction(w, t)
    assert ins.assigned_to_identity_id == w.agent
    sug = await _propose(w, t, ins.id)
    assert (await _fresh(w, sug.id)).instruction_id == ins.id

    async with w.person() as s:
        diffs = {
            r.entity_id: r.diff
            for r in (
                await s.execute(
                    select(ActivityLog).where(
                        ActivityLog.entity == "annotation", ActivityLog.action == "create"
                    )
                )
            ).scalars()
        }
    assert (diffs[ins.id] or {})["assigned_to_identity_id"] == str(w.agent)
    assert (diffs[sug.id] or {})["instruction_id"] == str(ins.id)


async def _link_refused(w: _World, doc_id: uuid.UUID, instruction_id: uuid.UUID) -> str:
    with pytest.raises(DomainError) as e:
        await _propose(w, doc_id, instruction_id)
    assert e.value.code == MessageCode.ANNOTATION_INSTRUCTION_INVALID
    reason = e.value.params["reason"]
    assert isinstance(reason, str)
    return reason


@pytest.mark.asyncio
async def test_a_suggestion_cannot_cite_what_is_not_an_open_comment_on_its_document() -> None:
    w = await _world()
    t = await _task(w)

    other = await _instruction(w, await _task(w))
    assert await _link_refused(w, t, other.id) == "other_document"

    another_suggestion = await _propose(w, t, None, original="rest")
    assert await _link_refused(w, t, another_suggestion.id) == "not_a_comment"
    assert await _link_refused(w, t, uuid.uuid4()) == "not_a_comment"

    gone = await _instruction(w, t)
    async with w.person() as s:
        await anno.soft_delete(
            s, org_id=w.org, actor_id=w.user, annotation_id=gone.id, expected_version=1
        )
    assert await _link_refused(w, t, gone.id) == "deleted"

    closed = await _instruction(w, t)
    async with w.person() as s:
        await anno.resolve(
            s, org_id=w.org, actor_id=w.user, annotation_id=closed.id, expected_version=1
        )
    assert await _link_refused(w, t, closed.id) == "closed"


@pytest.mark.asyncio
async def test_an_instruction_on_one_part_is_answered_on_another_part_of_the_same_note() -> None:
    w = await _world()
    async with w.person() as s:
        note = await nt.create_note(
            s, org_id=w.org, actor_id=w.user, kind=NoteKind.text, text="intro"
        )
        second = await np.create_part(
            s, org_id=w.org, actor_id=w.user, note_id=note.id, body="ship on friday"
        )
        parts = await np.list_parts(s, org_id=w.org, note_id=note.id)
    first_id = next(p.id for p in parts if p.id != second.id)
    async with w.person() as s:
        ins = await anno.create_comment(
            s,
            org_id=w.org,
            actor_id=w.user,
            doc_kind="note_part",
            doc_id=first_id,
            body="make the day in the next part concrete",
            assignee_identity_id=w.agent,
        )
    sug = await _propose(w, second.id, ins.id, doc_kind="note_part")
    assert (await _fresh(w, sug.id)).instruction_id == ins.id


# --------------------------------------------------------------------------
# the person closes the instruction
# --------------------------------------------------------------------------
async def _reserved(action: str, call: Awaitable[object]) -> None:
    with pytest.raises(ForbiddenError) as e:
        await call
    assert e.value.code == MessageCode.ANNOTATION_INSTRUCTION_RESERVED
    assert e.value.params["action"] == action


@pytest.mark.asyncio
async def test_an_assistant_cannot_close_reassign_or_rewrite_an_instruction() -> None:
    w = await _world()
    t = await _task(w)
    ins = await _instruction(w, t)
    common = {"org_id": w.org, "actor_id": w.user, "annotation_id": ins.id}

    async with w.through_agent() as s:
        await _reserved("resolve", anno.resolve(s, expected_version=1, **common))
    async with w.through_agent() as s:
        await _reserved("reassign", anno.assign(s, expected_version=1, clear=True, **common))
    async with w.through_agent() as s:
        await _reserved(
            "reassign",
            anno.assign(s, expected_version=1, assignee_identity_id=w.me, **common),
        )
    async with w.through_agent() as s:
        await _reserved(
            "change",
            anno.edit(
                s, body="never mind", expected_version=1, actor_identity_id=w.agent, **common
            ),
        )
    async with w.through_agent() as s:
        await _reserved(
            "change",
            anno.append_to_body(s, text="(done)", actor_identity_id=w.agent, **common),
        )
    async with w.through_agent() as s:
        await _reserved(
            "change",
            anno.soft_delete(s, expected_version=1, actor_identity_id=w.agent, **common),
        )
    async with w.through_agent() as s:
        await _reserved("purge", anno.purge(s, **common))

    # A caller that forgets to pass the assistant's identity makes the agent
    # look like the person who wrote the instruction; the credential decides.
    async with w.through_agent() as s:
        await _reserved("change", anno.edit(s, body="never mind", expected_version=1, **common))

    # Nothing moved: still open, still assigned, same words, same version.
    row = await _fresh(w, ins.id)
    assert (row.status, row.assigned_to_identity_id, row.body, row.version) == (
        "open",
        w.agent,
        "make the day concrete",
        1,
    )


@pytest.mark.asyncio
async def test_an_assistant_cannot_restore_an_instruction_or_one_of_its_revisions() -> None:
    w = await _world()
    t = await _task(w)
    ins = await _instruction(w, t)
    async with w.person() as s:
        v = await anno.edit(
            s,
            org_id=w.org,
            actor_id=w.user,
            annotation_id=ins.id,
            body="make the day concrete, with the date",
            expected_version=1,
        )
    async with w.person() as s:
        first_rev = (
            await revisions.list_revisions(
                s, entity_kind=revisions.ENTITY_KIND_ANNOTATION, entity_id=ins.id
            )
        )[-1]
    async with w.through_agent() as s:
        await _reserved(
            "change",
            anno.restore_revision(
                s,
                org_id=w.org,
                actor_id=w.user,
                annotation_id=ins.id,
                revision_id=first_rev.id,
                expected_version=v,
                actor_identity_id=w.agent,
            ),
        )
    async with w.person() as s:
        v = await anno.soft_delete(
            s, org_id=w.org, actor_id=w.user, annotation_id=ins.id, expected_version=v
        )
    async with w.through_agent() as s:
        await _reserved(
            "change",
            anno.restore(
                s,
                org_id=w.org,
                actor_id=w.user,
                annotation_id=ins.id,
                expected_version=v,
                actor_identity_id=w.agent,
            ),
        )


@pytest.mark.asyncio
async def test_the_person_closes_edits_and_reassigns_and_the_agent_answers_and_reopens() -> None:
    w = await _world()
    t = await _task(w)
    ins = await _instruction(w, t)

    async with w.through_agent() as s:
        reply = await anno.create_comment(
            s,
            org_id=w.org,
            actor_id=w.user,
            doc_kind="task_description",
            doc_id=t,
            body="done in a suggestion",
            parent_id=ins.id,
            author_identity_id=w.agent,
        )
    assert reply.parent_id == ins.id

    async with w.person() as s:
        v = await anno.edit(
            s,
            org_id=w.org,
            actor_id=w.user,
            annotation_id=ins.id,
            body="make the day concrete, please",
            expected_version=1,
        )
        v = await anno.resolve(
            s, org_id=w.org, actor_id=w.user, annotation_id=ins.id, expected_version=v
        )
    # Reopening gives the person their instruction back, so an agent may.
    async with w.through_agent() as s:
        v = await anno.reopen(
            s, org_id=w.org, actor_id=w.user, annotation_id=ins.id, expected_version=v
        )
    async with w.person() as s:
        await anno.assign(
            s,
            org_id=w.org,
            actor_id=w.user,
            annotation_id=ins.id,
            expected_version=v,
            assignee_identity_id=w.me,
        )
    # Reassigned to a person, it is no longer an instruction to an assistant.
    async with w.through_agent() as s:
        await anno.resolve(
            s, org_id=w.org, actor_id=w.user, annotation_id=ins.id, expected_version=v + 1
        )


@pytest.mark.asyncio
async def test_a_comment_that_is_not_an_instruction_stays_open_to_an_assistant() -> None:
    w = await _world()
    t = await _task(w)
    async with w.person() as s:
        unassigned = await anno.create_comment(
            s,
            org_id=w.org,
            actor_id=w.user,
            doc_kind="task_description",
            doc_id=t,
            body="looks fine",
        )
    async with w.through_agent() as s:
        agent_to_agent = await anno.create_comment(
            s,
            org_id=w.org,
            actor_id=w.user,
            doc_kind="task_description",
            doc_id=t,
            body="note to self",
            author_identity_id=w.agent,
            assignee_identity_id=w.agent,
        )
    for c in (unassigned, agent_to_agent):
        async with w.through_agent() as s:
            await anno.resolve(
                s,
                org_id=w.org,
                actor_id=w.user,
                annotation_id=c.id,
                expected_version=1,
                resolved_by_identity_id=w.agent,
            )
