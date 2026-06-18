"""Decomposition pipeline (task 4a718dc4).

Asserts the Phase 1 invariants:
- a non-trivial note can be distilled exactly once;
- the distillation note carries ``humus_kind='distillation'`` and
  ``humus_flag=True``;
- a NoteNoteLink (``hypha_of``) joins the source to the distillation
  so the graph can navigate the lineage;
- re-running on an already-distilled source is a no-op (returns the
  cached distillation id).
"""

from __future__ import annotations

import datetime as dt
import sys
import uuid
from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _fake_ai import FakeLLM  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from flow_core.ai_providers import LLMResult, set_llm_override  # noqa: E402
from flow_core.db import admin_session, tenant_session  # noqa: E402
from flow_core.errors import DomainError  # noqa: E402
from flow_core.models.activity_log import ActivityLog  # noqa: E402
from flow_core.models.billing import CostBasis, UsageRecord  # noqa: E402
from flow_core.models.classification_feedback import ClassificationFeedback  # noqa: E402
from flow_core.models.note import Note, NoteKind  # noqa: E402
from flow_core.models.note_link import NoteNoteLink  # noqa: E402
from flow_core.services import billing  # noqa: E402
from flow_core.services import decomposition as decomp  # noqa: E402
from flow_core.services import notes as nt  # noqa: E402
from flow_core.services.auth import signup  # noqa: E402


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="DECOMP")
    return r.org_id, r.user_id


@pytest.fixture
def _wire_llm() -> Iterator[None]:
    set_llm_override(FakeLLM)
    try:
        yield
    finally:
        set_llm_override(None)


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


async def test_distill_note_creates_humus_atom(_wire_llm: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="Long thought",
            text=(
                "Decided to stop building Y because the failure mode only "
                "shows up after weeks; the week-2 workaround masked a config "
                "drift between staging and prod."
            ),
        )
        # The anti-mutation invariant (task 8a26c000) marks the SOURCE as
        # humus only when it is inert; archive it and age it past the quiet
        # window so the distillation's humus side-effect applies to it.
        source.is_archived = True
        await s.flush()
        await s.execute(
            text("UPDATE notes SET updated_at = :t WHERE id = :id"),
            {"t": dt.datetime.now(dt.UTC) - dt.timedelta(days=30), "id": str(source.id)},
        )
        await s.refresh(source)  # the raw UPDATE bypasses the identity map
        res = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        distilled = (
            await s.execute(select(Note).where(Note.id == res.distilled_note_id))
        ).scalar_one()
        assert distilled.humus_kind == "distillation"
        assert distilled.humus_flag is True
        await s.refresh(source)
        assert source.humus_flag is True


async def test_distill_does_not_mark_a_live_source_humus(_wire_llm: None) -> None:
    """Anti-mutation invariant (task 8a26c000): distilling a LIVE source
    (fresh / recently edited) still creates the derived distillation node,
    but must NOT mark the live source as humus."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="live",
            text="a non-trivial body the LLM will distill on the first pass",
        )
        res = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        distilled = (
            await s.execute(select(Note).where(Note.id == res.distilled_note_id))
        ).scalar_one()
        assert distilled.humus_flag is True
        await s.refresh(source)
        assert source.humus_flag is False
        link = (
            await s.execute(
                select(NoteNoteLink).where(
                    NoteNoteLink.parent_note_id == source.id,
                    NoteNoteLink.child_note_id == distilled.id,
                )
            )
        ).scalar_one()
        assert link.kind == "hypha_of"


async def test_distill_note_meters_through_the_seam(_wire_llm: None) -> None:
    """WS-C3 (privacy/billing fix): distillation must charge through the
    per-org MeteredLLM seam, not a bare get_llm() that never bills. With a
    rate card seeded, the first distillation produces exactly one UsageRecord
    under ``op='distill'`` on the REAL model id, and the balance drops."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        before = await billing.balance(s, org_id=org)
        source = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="meter me",
            text="a non-trivial finished thought worth distilling into reusable atoms",
        )
        res = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        rec = (
            await s.execute(
                select(UsageRecord).where(UsageRecord.operation_id == f"distill:{org}:{source.id}")
            )
        ).scalar_one()
        assert rec.op == "distill"
        assert rec.basis is CostBasis.local
        assert rec.model_id == res.model_id  # the REAL model, not the 'cached' sentinel
        assert await billing.balance(s, org_id=org) < before


async def _inert_source(s: object, org: uuid.UUID, user: uuid.UUID, *, title: str) -> Note:
    """Create a note and make it inert (archived + aged past the quiet
    window) so distilling it flips the source's ``humus_flag``."""
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
        text("UPDATE notes SET updated_at = :t WHERE id = :id"),
        {"t": dt.datetime.now(dt.UTC) - dt.timedelta(days=30), "id": str(source.id)},
    )
    await s.refresh(source)  # type: ignore[attr-defined]
    return source


async def test_distill_traces_humus_flip_on_inert_source(_wire_llm: None) -> None:
    """WS-F2 (§12 'every mutation is tracked'): the autonomous ``humus_flag``
    flip on an inert source must leave a trace -- an ``auto_humus`` audit row
    AND an append-only ``classification_feedback`` row (action 'auto')."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await _inert_source(s, org, user, title="inert thought")
        res = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        await s.refresh(source)
        assert source.humus_flag is True
        # Audit trail.
        log_rows = (
            (
                await s.execute(
                    select(ActivityLog).where(
                        ActivityLog.entity == "note",
                        ActivityLog.entity_id == source.id,
                        ActivityLog.action == "auto_humus",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(log_rows) == 1
        assert log_rows[0].diff is not None
        assert log_rows[0].diff["humus_flag"] == {"old": False, "new": True}
        # Learning-loop feedback projection.
        fb = (
            await s.execute(
                select(ClassificationFeedback).where(
                    ClassificationFeedback.node_id == source.id,
                    ClassificationFeedback.suggestion_type == "humus",
                )
            )
        ).scalar_one()
        assert fb.action == "auto"
        assert fb.suggestion_value == {"humus_flag": True}
        assert fb.signals_snapshot["trigger"] == "distill"
        assert fb.signals_snapshot["distill_model_id"] == res.model_id


async def test_distill_does_not_trace_humus_for_live_source(_wire_llm: None) -> None:
    """A LIVE source is never flipped to humus, so it must leave NO trace:
    no ``auto_humus`` audit row and no humus feedback row."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="live",
            text="a non-trivial body the LLM will distill on the first pass",
        )
        await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        await s.refresh(source)
        assert source.humus_flag is False
        log_rows = (
            (
                await s.execute(
                    select(ActivityLog).where(
                        ActivityLog.entity_id == source.id,
                        ActivityLog.action == "auto_humus",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert log_rows == []
        fb_rows = (
            (
                await s.execute(
                    select(ClassificationFeedback).where(
                        ClassificationFeedback.node_id == source.id,
                        ClassificationFeedback.suggestion_type == "humus",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert fb_rows == []


async def test_distill_note_is_idempotent(_wire_llm: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        source = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="t",
            text="some non-trivial body the LLM will distill on the first pass",
        )
        r1 = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        r2 = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        assert r1.distilled_note_id == r2.distilled_note_id
        rows = (
            (
                await s.execute(
                    select(NoteNoteLink).where(
                        NoteNoteLink.parent_note_id == source.id,
                        NoteNoteLink.kind == "hypha_of",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1


async def test_flip_source_to_humus_is_version_guarded() -> None:
    """WS-F3: the guarded flip only fires when the version matches AND the
    flag is still False, and it bumps the version. A stale version or an
    already-set flag is a skip (returns False), never an exception."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="v", text="body"
        )
        v0 = note.version
        # Matching version + flag False -> flips and bumps the version.
        assert await decomp._flip_source_to_humus(s, note_id=note.id, expected_version=v0) is True
        await s.refresh(note)
        assert note.humus_flag is True
        assert note.version == v0 + 1
        # Stale version -> skip.
        assert await decomp._flip_source_to_humus(s, note_id=note.id, expected_version=v0) is False
        # Current version but flag already set -> skip (double-distill no-op).
        assert (
            await decomp._flip_source_to_humus(s, note_id=note.id, expected_version=note.version)
            is False
        )


class _RacingDistillLLM:
    """A fake LLM that, mid-distillation, bumps the source note's version --
    exactly the WS-F3 race window between snapshotting the version (before the
    call) and the guarded humus flip (after). distill_note must have captured
    the pre-call version, so the post-call flip finds the version moved and
    skips. Driven on the live session so the test is deterministic; the
    version-guard mechanism itself is proven independently against a real
    version change in ``test_flip_source_to_humus_is_version_guarded``."""

    model_id = "racing-llm"
    session: object
    note_id: uuid.UUID

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        del system
        await self.session.execute(  # type: ignore[attr-defined]
            text("UPDATE notes SET version = version + 1 WHERE id = :id"),
            {"id": str(self.note_id)},
        )
        last = messages[-1][1] if messages else ""
        return LLMResult(text=f"echo: {last}", tokens_in=1, tokens_out=1, model_id=self.model_id)


async def test_distill_skips_humus_flip_when_source_changes_mid_call() -> None:
    """WS-F3 end-to-end: if the inert source's version moves during the LLM
    call, the humus flip is skipped (check-and-skip) -- the source is NOT
    marked humus and leaves no trace -- but the distillation still stands.
    Guards against a regression that captures the version after the call."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        source = await _inert_source(s, org, user, title="raced")
        _RacingDistillLLM.session = s
        _RacingDistillLLM.note_id = source.id
        set_llm_override(_RacingDistillLLM)
        try:
            res = await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        finally:
            set_llm_override(None)
        # The distillation was still created.
        distilled = (
            await s.execute(select(Note).where(Note.id == res.distilled_note_id))
        ).scalar_one()
        assert distilled.humus_kind == "distillation"
        # The source was NOT flipped to humus (the race lost the version guard).
        # Read DB truth (the source ORM object would be the identity-mapped one).
        db_flag = (
            await s.execute(
                text("SELECT humus_flag FROM notes WHERE id = :id"), {"id": str(source.id)}
            )
        ).scalar_one()
        assert db_flag is False
        # And it left no trace, because nothing mutated.
        log_rows = (
            (
                await s.execute(
                    select(ActivityLog).where(
                        ActivityLog.entity_id == source.id,
                        ActivityLog.action == "auto_humus",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert log_rows == []
        fb_rows = (
            (
                await s.execute(
                    select(ClassificationFeedback).where(
                        ClassificationFeedback.node_id == source.id,
                        ClassificationFeedback.suggestion_type == "humus",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert fb_rows == []


# ── Phase 2: pattern extraction + season synthesis (e87daff4) ──────────


async def _archived(s: object, org: uuid.UUID, user: uuid.UUID, *, title: str, body: str) -> Note:
    n = await nt.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=body,
    )
    n.is_archived = True
    await s.flush()  # type: ignore[attr-defined]
    return n


async def test_extract_cluster_pattern_creates_linked_humus(_wire_llm: None) -> None:
    """A pattern note is synthesised over >=2 archived sources: humus_kind
    'pattern', flag set, a stable signature, and a hypha_of link from EVERY
    source -- and the sources themselves are never mutated. Re-running is
    idempotent (the existing note is returned)."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        a = await _archived(s, org, user, title="a", body="gardening notes on soil")
        b = await _archived(s, org, user, title="b", body="more soil and compost ideas")
        c = await _archived(s, org, user, title="c", body="compost turns into humus")
        res = await decomp.extract_cluster_pattern(
            s, org_id=org, actor_id=user, source_note_ids=[a.id, b.id, c.id]
        )
        assert res.created is True
        note = (await s.execute(select(Note).where(Note.id == res.note_id))).scalar_one()
        assert note.humus_kind == "pattern"
        assert note.humus_flag is True
        assert note.humus_signature
        links = (
            (
                await s.execute(
                    select(NoteNoteLink).where(
                        NoteNoteLink.child_note_id == note.id, NoteNoteLink.kind == "hypha_of"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {lk.parent_note_id for lk in links} == {a.id, b.id, c.id}
        # Sources are read-only inputs (§12): never flipped to humus.
        for src in (a, b, c):
            await s.refresh(src)
            assert src.humus_flag is False
        # Idempotent: same source set -> same note, no new synthesis.
        again = await decomp.extract_cluster_pattern(
            s, org_id=org, actor_id=user, source_note_ids=[c.id, a.id, b.id]
        )
        assert again.created is False
        assert again.note_id == res.note_id


async def test_extract_cluster_pattern_needs_two_archived_sources(_wire_llm: None) -> None:
    """One archived source (or live sources) -> no pattern to extract."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        only = await _archived(s, org, user, title="solo", body="a single archived note")
        # A live (non-archived) note must not count toward the pattern.
        live = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="live", text="still working"
        )
        with pytest.raises(DomainError):
            await decomp.extract_cluster_pattern(
                s, org_id=org, actor_id=user, source_note_ids=[only.id, live.id]
            )


async def test_synthesize_season_creates_humus_idempotent(_wire_llm: None) -> None:
    """The season synthesis gathers the quarter's archived notes into one
    'season' humus note, keyed (idempotent) on year+quarter."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        a = await _archived(s, org, user, title="winter a", body="a thought from the quarter")
        b = await _archived(s, org, user, title="winter b", body="another from the quarter")
        # Backdate both into 2025 Q1.
        await s.execute(
            text("UPDATE notes SET created_at = :t WHERE id IN (:a, :b)"),
            {"t": dt.datetime(2025, 2, 1, tzinfo=dt.UTC), "a": str(a.id), "b": str(b.id)},
        )
        res = await decomp.synthesize_season(s, org_id=org, actor_id=user, year=2025, quarter=1)
        assert res.created is True
        note = (await s.execute(select(Note).where(Note.id == res.note_id))).scalar_one()
        assert note.humus_kind == "season"
        assert note.humus_signature == "2025Q1"
        again = await decomp.synthesize_season(s, org_id=org, actor_id=user, year=2025, quarter=1)
        assert again.created is False
        assert again.note_id == res.note_id
        # A quarter with nothing archived -> nothing to synthesise.
        with pytest.raises(DomainError):
            await decomp.synthesize_season(s, org_id=org, actor_id=user, year=2024, quarter=3)
