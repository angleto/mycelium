"""An agent approves or rejects generated text only on a decision a person
took, written in a comment on one of the text's sources.

Approving and rejecting a proposed node are a person's act, fenced off from
every scoped credential. ``approve_node_on_decision`` and
``reject_node_on_decision`` are how an agent carries out a decision a person
already wrote down, the same pattern as accepting a suggestion (ADR-0066).
A proposed node cannot be commented on (its text must not leave the review
perimeter), so the decision lives on a SOURCE of the node: a note the
person can read, with a line ``approve <id>`` or ``reject <id>``.

Each refusal below is a way an agent could otherwise have decided alone.
Sessions are opened with the actor kind the transports publish: a person's
own bearer over REST is ``human_api`` with no subject, an agent token over
MCP is ``mcp_token`` with its token id as subject.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager
from decimal import Decimal

import pytest
from _fake_ai import FakeLLM
from _fake_embedder import FakeEmbedder
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.ai_providers import set_llm_override
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import ConflictError, DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.models.note_part import NotePart
from mycelium_core.services import annotations as anno
from mycelium_core.services import billing, identities
from mycelium_core.services import decomposition as decomp
from mycelium_core.services import garden_review as review
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup


@pytest.fixture
def _wire() -> Iterator[None]:
    set_llm_override(FakeLLM)
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_llm_override(None)
        set_embedder_override(None)


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
            org_name="GARDEN-DECIDE",
        )
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        a = AiAssistant(
            org_id=r.org_id,
            user_id=r.user_id,
            label="Claude",
            handle=f"claude-{uuid.uuid4().hex[:8]}",
            scope=["comments:write", "notes:write"],
            is_active=True,
        )
        s.add(a)
        await s.flush()
        ident = await identities.ensure_for_ai_assistant(s, org_id=r.org_id, assistant_id=a.id)
        await billing.grant_credits(s, org_id=r.org_id, actor_id=r.user_id, amount=Decimal(1000))
        await billing.upsert_rate_card(
            s,
            org_id=r.org_id,
            actor_id=r.user_id,
            model_id="fake-llm",
            provider="local",
            values={
                "credits_per_input": Decimal("0.001"),
                "credits_per_output": Decimal("0.001"),
            },
        )
    return _World(r.org_id, r.user_id, ident.id)


async def _inert_note(w: _World, title: str) -> uuid.UUID:
    """An archived note aged past the quiet window. Written on the default
    (system) session, as the other humus tests do: a person's own write
    would restart the note's liveness clock, and the activity log that
    clock reads is append-only, so it cannot be aged afterwards."""
    async with tenant_session(str(w.org), str(w.user)) as s:
        n = await nt.create_note(
            s,
            org_id=w.org,
            actor_id=w.user,
            kind=NoteKind.text,
            title=title,
            text="a finished thought, archived and aged, worth distilling into atoms",
        )
        n.is_archived = True
        await s.flush()
        await s.execute(
            text("UPDATE notes SET created_at = :t, updated_at = :t WHERE id = :id"),
            {"t": dt.datetime.now(dt.UTC) - dt.timedelta(days=30), "id": str(n.id)},
        )
        return n.id


async def _proposal(w: _World, source: uuid.UUID) -> Note:
    async with w.through_agent() as s:
        res = await decomp.distill_note(s, org_id=w.org, actor_id=w.user, note_id=source)
        atom = (await s.execute(select(Note).where(Note.id == res.distilled_note_id))).scalar_one()
        assert atom.review_state == "proposed"
        return atom


async def _comment(
    w: _World, note_id: uuid.UUID, body: str, *, by_agent: bool = False
) -> uuid.UUID:
    async with w.through_agent() if by_agent else w.person() as s:
        part = (
            await s.execute(select(NotePart.id).where(NotePart.note_id == note_id).limit(1))
        ).scalar_one()
        c = await anno.create_comment(
            s,
            org_id=w.org,
            actor_id=w.user,
            doc_kind="note_part",
            doc_id=part,
            body=body,
            author_identity_id=w.agent if by_agent else None,
        )
        return c.id


async def _edit(w: _World, comment: uuid.UUID, body: str, *, by_agent: bool = False) -> None:
    async with w.through_agent() if by_agent else w.person() as s:
        current = await anno.get_annotation(s, org_id=w.org, annotation_id=comment)
        await anno.edit(
            s,
            org_id=w.org,
            actor_id=w.user,
            annotation_id=comment,
            body=body,
            expected_version=current.version,
        )


async def _approve(w: _World, atom: Note, decision: uuid.UUID) -> Note:
    async with w.through_agent() as s:
        return await review.approve_node_on_decision(
            s,
            org_id=w.org,
            actor_id=w.user,
            note_id=atom.id,
            expected_version=atom.version,
            decision_comment_id=decision,
            executed_by_identity_id=w.agent,
        )


async def _reject(w: _World, atom: Note, decision: uuid.UUID) -> Note:
    async with w.through_agent() as s:
        return await review.reject_node_on_decision(
            s,
            org_id=w.org,
            actor_id=w.user,
            note_id=atom.id,
            expected_version=atom.version,
            decision_comment_id=decision,
            executed_by_identity_id=w.agent,
        )


async def _state(w: _World, note_id: uuid.UUID) -> tuple[str | None, bool, bool]:
    """``(review_state, deleted, humus_flag)`` read past the identity map."""
    async with tenant_session(str(w.org), str(w.user)) as s:
        row = (
            await s.execute(
                text("SELECT review_state, deleted_at, humus_flag FROM notes WHERE id = :id"),
                {"id": str(note_id)},
            )
        ).one()
    return row.review_state, row.deleted_at is not None, bool(row.humus_flag)


async def _refused(w: _World, atom: Note, decision: uuid.UUID, *, verb: str = "approve") -> str:
    with pytest.raises(DomainError) as e:
        await (_approve if verb == "approve" else _reject)(w, atom, decision)
    assert e.value.code == MessageCode.GARDEN_REVIEW_DECISION_INVALID
    reason = e.value.params["reason"]
    assert isinstance(reason, str)
    # Nothing moved.
    assert (await _state(w, atom.id))[:2] == ("proposed", False)
    return reason


# ── a person's decision is carried out ──────────────────────────────────────


async def test_an_agent_approves_on_a_persons_comment_on_the_source(_wire: None) -> None:
    w = await _world()
    source = await _inert_note(w, "source")
    atom = await _proposal(w, source)
    decision = await _comment(w, source, f"letto, va bene\napprove {atom.id.hex[:8]}")

    await _approve(w, atom, decision)

    assert (await _state(w, atom.id))[:2] == ("approved", False)
    # The comment on the source is not activity on it: the source is still
    # inert, so approving its distillation marks it humus.
    assert (await _state(w, source))[2] is True
    async with tenant_session(str(w.org), str(w.user)) as s:
        row = (
            await s.execute(
                select(ActivityLog).where(
                    ActivityLog.entity_id == atom.id,
                    ActivityLog.action == "garden_review:approve",
                )
            )
        ).scalar_one()
    assert row.diff is not None
    assert row.diff["decision_comment_id"] == str(decision)
    assert row.diff["executed_by_identity_id"] == str(w.agent)
    assert row.diff["decided_by_identity_id"] != str(w.agent)


async def test_an_agent_rejects_on_a_persons_comment(_wire: None) -> None:
    w = await _world()
    source = await _inert_note(w, "source")
    atom = await _proposal(w, source)
    decision = await _comment(w, source, f"reject {atom.id}")

    await _reject(w, atom, decision)

    assert (await _state(w, atom.id))[:2] == ("proposed", True)
    async with tenant_session(str(w.org), str(w.user)) as s:
        row = (
            await s.execute(
                select(ActivityLog).where(
                    ActivityLog.entity_id == atom.id,
                    ActivityLog.action == "garden_review:reject",
                )
            )
        ).scalar_one()
    assert row.diff is not None
    assert row.diff["decision_comment_id"] == str(decision)


# ── every way an agent could decide alone is refused ────────────────────────


async def test_a_comment_written_through_an_agent_is_not_a_decision(_wire: None) -> None:
    w = await _world()
    source = await _inert_note(w, "source")
    atom = await _proposal(w, source)
    by_agent = await _comment(w, source, f"approve {atom.id}", by_agent=True)
    assert await _refused(w, atom, by_agent) == "author_not_a_person"


async def test_a_comment_on_a_note_that_is_not_a_source_is_refused(_wire: None) -> None:
    w = await _world()
    source = await _inert_note(w, "source")
    elsewhere = await _inert_note(w, "unrelated")
    atom = await _proposal(w, source)
    decision = await _comment(w, elsewhere, f"approve {atom.id}")
    assert await _refused(w, atom, decision) == "not_on_a_source"


async def test_a_comment_older_than_the_proposal_is_refused(_wire: None) -> None:
    """A decision cannot predate the text it decides: the person had not read it."""
    w = await _world()
    source = await _inert_note(w, "source")
    early = await _comment(w, source, "approve 00000000")
    atom = await _proposal(w, source)
    await _edit(w, early, f"approve {atom.id}")
    assert await _refused(w, atom, early) == "before_proposal"


async def test_a_persons_comment_rewritten_through_an_agent_is_refused(_wire: None) -> None:
    """The author column stays the person's when an agent on their token
    edits the comment, so the writer is read from the activity log."""
    w = await _world()
    source = await _inert_note(w, "source")
    atom = await _proposal(w, source)
    decision = await _comment(w, source, "ci penso")
    await _edit(w, decision, f"approve {atom.id}", by_agent=True)
    assert await _refused(w, atom, decision) == "written_through_an_agent"


async def test_an_approval_cannot_be_spent_as_a_rejection(_wire: None) -> None:
    w = await _world()
    source = await _inert_note(w, "source")
    atom = await _proposal(w, source)
    decision = await _comment(w, source, f"approve {atom.id}")
    assert await _refused(w, atom, decision, verb="reject") == "node_not_named"


async def test_a_negated_line_names_nothing(_wire: None) -> None:
    w = await _world()
    source = await _inert_note(w, "source")
    atom = await _proposal(w, source)
    decision = await _comment(w, source, f"do not approve {atom.id}")
    assert await _refused(w, atom, decision) == "node_not_named"


async def test_a_comment_that_says_both_is_refused(_wire: None) -> None:
    w = await _world()
    source = await _inert_note(w, "source")
    atom = await _proposal(w, source)
    decision = await _comment(w, source, f"approve {atom.id}\nreject {atom.id.hex[:8]}")
    assert await _refused(w, atom, decision) == "contradictory"


async def test_a_stale_version_changes_nothing(_wire: None) -> None:
    w = await _world()
    source = await _inert_note(w, "source")
    atom = await _proposal(w, source)
    decision = await _comment(w, source, f"approve {atom.id}")
    atom.version -= 1
    with pytest.raises(ConflictError):
        await _approve(w, atom, decision)
    assert (await _state(w, atom.id))[:2] == ("proposed", False)


async def test_a_name_shared_with_another_node_of_the_source_is_refused(
    _wire: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One decision is spent on one node. Two random ids practically never
    share an 8-character prefix, so the name the comment yields is forced to
    one both nodes match; the sibling is an earlier, rejected distillation
    of the same source, which still counts: a person who wrote that prefix
    may have meant either."""
    w = await _world()
    source = await _inert_note(w, "source")
    first = await _proposal(w, source)
    async with tenant_session(str(w.org), str(w.user)) as s:
        await review.reject_node(s, org_id=w.org, actor_id=w.user, note_id=first.id)
    atom = await _proposal(w, source)
    decision = await _comment(w, source, f"approve {atom.id}")

    real = anno.named_prefixes

    def shared(body: str, verb: str = "accept") -> list[str]:
        return [""] if verb == "approve" else real(body, verb)

    monkeypatch.setattr(anno, "named_prefixes", shared)
    assert await _refused(w, atom, decision) == "ambiguous_id"


# ── the same decision, carried out through the MCP tool ─────────────────────


async def test_the_mcp_tool_names_the_assistant_behind_the_token_as_executor(
    _wire: None,
) -> None:
    """The tool is generated from the registry operation (ADR-0068), which
    resolves the executor from the calling agent token on either surface.
    Driven through the real MCP path: the published principal, the tenant
    session it opens, the argument model, the operation."""
    from mycelium_core.services import agent_tokens as at_svc
    from mycelium_mcp.server import _PRINCIPAL
    from mycelium_mcp.tools_garden import TOOLS

    w = await _world()
    source = await _inert_note(w, "source")
    atom = await _proposal(w, source)
    decision = await _comment(w, source, f"approve {atom.id.hex[:8]}")
    async with w.person() as s:
        assistant = (
            await s.execute(select(AiAssistant.id).where(AiAssistant.org_id == w.org))
        ).scalar_one()
        minted = await at_svc.mint(
            s, org_id=w.org, actor_id=w.user, name="agent", assistant_id=assistant
        )

    reset = _PRINCIPAL.set((w.user, w.org, minted.token.id))
    try:
        out = await TOOLS["garden_review_approve_on_decision"](
            token="",
            org_id="",
            note_id=str(atom.id),
            expected_version=atom.version,
            decision_comment_id=str(decision),
        )
    finally:
        _PRINCIPAL.reset(reset)

    assert out["review_state"] == "approved"
    assert out["rejected"] is False
    assert out["decision_comment_id"] == str(decision)
    async with tenant_session(str(w.org), str(w.user)) as s:
        row = (
            await s.execute(
                select(ActivityLog).where(
                    ActivityLog.entity_id == atom.id,
                    ActivityLog.action == "garden_review:approve",
                )
            )
        ).scalar_one()
    assert row.diff is not None
    assert row.diff["executed_by_identity_id"] == str(w.agent)
    assert row.diff["decision_comment_id"] == str(decision)
