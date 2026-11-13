"""A source note becomes humus only when a person approves what was
generated from it.

Generated text is unverified: the models that write summaries have not been
validated on fidelity, so nothing they produce may change how the note they
read is treated until a person has looked at the result. Before this, the
source's ``humus_flag`` flipped the moment the distillation was CREATED --
while the atom itself was still a hidden proposal -- and neither approving
nor rejecting the atom touched it again. A rejected summary therefore left
its source demoted from ordinary retrieval behind an atom nobody could see.

Asserted here:

- every synthesis (distillation, pattern, season) is born ``proposed``,
  whoever asked for it and whichever model wrote it;
- creating a distillation leaves the source untouched;
- approving it marks the source humus, but only while the source is still
  inert, and records who did it;
- rejecting it, or retiring it through ``restore_source``, takes the mark
  back off, unless another effective distillation of the same source still
  justifies it;
- a pattern or season approval never marks its sources.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from _fake_ai import FakeLLM
from _fake_embedder import FakeEmbedder
from sqlalchemy import select, text

from mycelium_core.ai_providers import set_llm_override
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.models.classification_feedback import ClassificationFeedback
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.services import billing
from mycelium_core.services import decomposition as decomp
from mycelium_core.services import garden_review as review
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="HUMUS")
    return r.org_id, r.user_id


@pytest.fixture
def _wire() -> Iterator[None]:
    set_llm_override(FakeLLM)
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_llm_override(None)
        set_embedder_override(None)


async def _seed_billing(s: object, org: uuid.UUID, user: uuid.UUID) -> None:
    await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(1000))  # type: ignore[arg-type]
    await billing.upsert_rate_card(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        model_id="fake-llm",
        provider="local",
        values={
            "credits_per_input": Decimal("0.001"),
            "credits_per_output": Decimal("0.001"),
        },
    )


async def _inert_source(s: object, org: uuid.UUID, user: uuid.UUID, *, title: str) -> Note:
    """An archived note aged past the quiet window: the only kind of note a
    distillation may mark humus."""
    source = await nt.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text="a finished thought, archived and aged, worth distilling into atoms",
    )
    source.is_archived = True
    await s.flush()  # type: ignore[attr-defined]
    await s.execute(  # type: ignore[attr-defined]
        text("UPDATE notes SET created_at = :t, updated_at = :t WHERE id = :id"),
        {"t": dt.datetime.now(dt.UTC) - dt.timedelta(days=30), "id": str(source.id)},
    )
    await s.refresh(source)  # type: ignore[attr-defined]
    return source


async def _flag(s: object, note_id: uuid.UUID) -> bool:
    """DB truth, past the identity map."""
    return bool(
        (
            await s.execute(  # type: ignore[attr-defined]
                text("SELECT humus_flag FROM notes WHERE id = :id"), {"id": str(note_id)}
            )
        ).scalar_one()
    )


async def _atom(s: object, note_id: uuid.UUID) -> Note:
    return (await s.execute(select(Note).where(Note.id == note_id))).scalar_one()  # type: ignore[attr-defined]


async def _audit(s: object, note_id: uuid.UUID, action: str) -> list[ActivityLog]:
    return list(
        (
            await s.execute(  # type: ignore[attr-defined]
                select(ActivityLog).where(
                    ActivityLog.entity == "note",
                    ActivityLog.entity_id == note_id,
                    ActivityLog.action == action,
                )
            )
        )
        .scalars()
        .all()
    )


# ── born proposed, whoever asked ────────────────────────────────────────────


async def test_user_requested_distillation_is_born_proposed(_wire: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await _inert_source(s, org, user, title="asked for")
        res = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        assert (await _atom(s, res.distilled_note_id)).review_state == "proposed"


async def test_pattern_and_season_are_born_proposed(_wire: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        ids = []
        for t in ("a", "b"):
            n = await nt.create_note(
                s, org_id=org, actor_id=user, kind=NoteKind.text, title=t, text=f"compost {t}"
            )
            n.is_archived = True
            ids.append(n.id)
        await s.flush()
        pat = await decomp.extract_cluster_pattern(
            s, org_id=org, actor_id=user, source_note_ids=ids
        )
        assert (await _atom(s, pat.note_id)).review_state == "proposed"
        now = dt.datetime.now(dt.UTC)
        quarter = (now.month - 1) // 3 + 1
        season = await decomp.synthesize_season(
            s, org_id=org, actor_id=user, year=now.year, quarter=quarter
        )
        assert (await _atom(s, season.note_id)).review_state == "proposed"


# ── creation leaves the source alone ────────────────────────────────────────


async def test_creating_a_distillation_does_not_mark_the_source(_wire: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await _inert_source(s, org, user, title="inert")
        v0 = source.version
        await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        assert await _flag(s, source.id) is False
        await s.refresh(source)
        assert source.version == v0
        assert await _audit(s, source.id, "auto_humus") == []


# ── approval marks the source ───────────────────────────────────────────────


async def test_approving_a_distillation_marks_an_inert_source(_wire: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await _inert_source(s, org, user, title="inert")
        res = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        await review.approve_node(s, org_id=org, actor_id=user, note_id=res.distilled_note_id)
        assert await _flag(s, source.id) is True
        rows = await _audit(s, source.id, "garden_review:source_humus")
        assert len(rows) == 1
        assert rows[0].actor_id == user
        assert rows[0].diff is not None
        assert rows[0].diff["humus_flag"] == {"old": False, "new": True}
        assert rows[0].diff["distilled_note_id"] == str(res.distilled_note_id)
        fb = (
            await s.execute(
                select(ClassificationFeedback).where(
                    ClassificationFeedback.node_id == source.id,
                    ClassificationFeedback.suggestion_type == "humus",
                )
            )
        ).scalar_one()
        assert fb.action == "accept"
        assert fb.suggestion_value == {"humus_flag": True}


async def test_approval_leaves_a_source_that_came_back_to_life(_wire: None) -> None:
    """Between the proposal and the approval the person took the note up
    again: it is live, and approving the summary must not demote it."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await _inert_source(s, org, user, title="revived")
        res = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        await nt.archive_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=source.id,
            expected_version=source.version,
            archived=False,
        )
        await review.approve_node(s, org_id=org, actor_id=user, note_id=res.distilled_note_id)
        assert await _flag(s, source.id) is False
        assert await _audit(s, source.id, "garden_review:source_humus") == []


async def test_approving_a_pattern_never_marks_its_sources(_wire: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        a = await _inert_source(s, org, user, title="a")
        b = await _inert_source(s, org, user, title="b")
        pat = await decomp.extract_cluster_pattern(
            s, org_id=org, actor_id=user, source_note_ids=[a.id, b.id]
        )
        await review.approve_node(s, org_id=org, actor_id=user, note_id=pat.note_id)
        assert await _flag(s, a.id) is False
        assert await _flag(s, b.id) is False


# ── rejection and withdrawal take the mark back ─────────────────────────────


async def _legacy_marked(s: object, org: uuid.UUID, user: uuid.UUID) -> tuple[Note, uuid.UUID]:
    """The state the old code left behind: a proposed distillation whose
    source was already marked humus when the atom was created."""
    source = await _inert_source(s, org, user, title="legacy")
    res = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
    await s.execute(  # type: ignore[attr-defined]
        text("UPDATE notes SET humus_flag = true, version = version + 1 WHERE id = :id"),
        {"id": str(source.id)},
    )
    await s.refresh(source)  # type: ignore[attr-defined]
    return source, res.distilled_note_id


async def test_rejecting_a_distillation_clears_the_source_mark(_wire: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source, atom_id = await _legacy_marked(s, org, user)
        await review.reject_node(s, org_id=org, actor_id=user, note_id=atom_id)
        assert await _flag(s, source.id) is False
        rows = await _audit(s, source.id, "garden_review:source_humus")
        assert len(rows) == 1
        assert rows[0].diff is not None
        assert rows[0].diff["humus_flag"] == {"old": True, "new": False}


async def test_rejection_keeps_a_mark_another_effective_atom_justifies(_wire: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source, atom_id = await _legacy_marked(s, org, user)
        # A second distillation of the same source, already effective.
        other = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="older", text="lesson"
        )
        other.humus_kind = "distillation"
        other.humus_flag = True
        s.add(
            NoteNoteLink(
                org_id=org, parent_note_id=source.id, child_note_id=other.id, kind="hypha_of"
            )
        )
        await s.flush()
        await review.reject_node(s, org_id=org, actor_id=user, note_id=atom_id)
        assert await _flag(s, source.id) is True


async def test_restore_source_on_an_approved_atom_clears_the_mark(_wire: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await _inert_source(s, org, user, title="restored")
        res = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        await review.approve_node(s, org_id=org, actor_id=user, note_id=res.distilled_note_id)
        assert await _flag(s, source.id) is True
        await review.restore_source(
            s, org_id=org, actor_id=user, atom_note_id=res.distilled_note_id
        )
        assert await _flag(s, source.id) is False


async def test_the_source_mark_never_lands_on_a_version_it_did_not_read(_wire: None) -> None:
    """The mark is written only while the source is still at the version the
    reviewer's transaction read. A write that slipped in between (an edit, an
    unarchive) wins: the mark is skipped and leaves no trace."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await _inert_source(s, org, user, title="raced")
        res = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        atom = await _atom(s, res.distilled_note_id)
        await s.execute(
            text("UPDATE notes SET version = version + 1 WHERE id = :id"), {"id": str(source.id)}
        )
        # ``source`` still carries the version read before the concurrent write.
        await review._set_source_humus(
            s, org_id=org, actor_id=user, source=source, atom=atom, value=True
        )
        assert await _flag(s, source.id) is False
        assert await _audit(s, source.id, "garden_review:source_humus") == []


async def test_a_rejected_distillation_does_not_block_a_new_one(_wire: None) -> None:
    """Rejecting a summary is asking for a better one, not closing the
    question: the next request must write a new distillation, not hand back
    the one in the bin. The response also says the new one is waiting for
    review, so a client knows not to open it as a live note."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await _inert_source(s, org, user, title="second try")
        first = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        assert first.review_state == "proposed"
        await review.reject_node(s, org_id=org, actor_id=user, note_id=first.distilled_note_id)
        second = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        assert second.created is True
        assert second.distilled_note_id != first.distilled_note_id
        assert second.review_state == "proposed"
        # And a repeat request while it waits returns it, saying so.
        again = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        assert again.created is False
        assert again.distilled_note_id == second.distilled_note_id
        assert again.review_state == "proposed"
