"""Human-gated review state for AUTONOMOUSLY-generated nodes (ADR-0043,
task e87daff4).

Asserts the gate's invariants end-to-end:

- a summary the garden generates AUTONOMOUSLY (``autonomous=True``) under the
  review gate is born ``review_state='proposed'`` and is INVISIBLE on every
  retrieval/listing surface (the walk, search, list_notes, get_note, the
  @-lookup picker, the free-wander humus set) until a human approves it;
- the producing model is on the artifact (``origin_model_id``), not only in
  the transient MCP response;
- approve -> effective (re-enters every surface) + a bus ``commit`` event +
  an audit row; reject -> soft-delete (never pollutes) + a bus ``reject``
  event carrying ``origin_model_id`` + an audit row, and the humus signature
  reopens for regeneration;
- with the gate OFF, or for a USER-initiated generation (the default
  ``autonomous=False``), the note is effective immediately -- byte-identical
  to pre-ADR-0043 behaviour.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from _fake_ai import FakeLLM
from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from mycelium_core.ai_providers import set_llm_override
from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import NotFoundError
from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.models.event_outbox import EventOutbox
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.services import billing, graph, lookup, memory
from mycelium_core.services import decomposition as decomp
from mycelium_core.services import garden_review as review
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup
from mycelium_core.services.task_search import _note_filter_meta


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="REVIEW")
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


@pytest.fixture
def _gate_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "garden_review_gate_enabled", True)
    yield


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


async def _make_proposed_pattern(
    s: object, org: uuid.UUID, user: uuid.UUID, *, autonomous: bool = True
) -> decomp.HumusResult:
    """Three archived sources -> one ``pattern`` humus synthesis, generated as
    the autonomous sweep would (``autonomous=True``)."""
    a = await _archived(s, org, user, title="a", body="soil compost forecast synthesis alpha")
    b = await _archived(s, org, user, title="b", body="soil compost forecast synthesis beta")
    c = await _archived(s, org, user, title="c", body="soil compost forecast synthesis gamma")
    return await decomp.extract_cluster_pattern(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        source_note_ids=[a.id, b.id, c.id],
        autonomous=autonomous,
    )


# ── D1/D3: born proposed, model on the artifact ────────────────────────────


async def test_autonomous_generation_is_born_proposed_with_model(
    _wire: None, _gate_on: None
) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        res = await _make_proposed_pattern(s, org, user)
        note = (await s.execute(select(Note).where(Note.id == res.note_id))).scalar_one()
        # Withheld from retrieval (proposed), humus as today, model stamped.
        assert note.review_state == "proposed"
        assert note.humus_flag is True
        assert note.humus_kind == "pattern"
        assert note.origin_model_id == res.model_id == "fake-llm"


async def test_user_initiated_is_effective_and_stamps_model(_wire: None, _gate_on: None) -> None:
    """Even with the gate ON, a USER-initiated synthesis (autonomous=False, the
    default) is effective immediately -- the gate keys on the trigger, not on
    'an LLM wrote it'. The model is still stamped (transparency)."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        res = await _make_proposed_pattern(s, org, user, autonomous=False)
        note = (await s.execute(select(Note).where(Note.id == res.note_id))).scalar_one()
        assert note.review_state is None
        assert note.origin_model_id == "fake-llm"


async def test_gate_off_autonomous_is_effective(_wire: None) -> None:
    """Flag OFF: even an autonomous generation is effective (no 'proposed'
    note is ever created) -- byte-identical to pre-ADR-0043 behaviour."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        res = await _make_proposed_pattern(s, org, user, autonomous=True)
        note = (await s.execute(select(Note).where(Note.id == res.note_id))).scalar_one()
        assert note.review_state is None
        assert note.origin_model_id == "fake-llm"  # transparency is unconditional


# ── D2: invisible on every listing surface until approved ──────────────────


async def test_proposed_note_hidden_from_listings_then_visible(_wire: None, _gate_on: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        res = await _make_proposed_pattern(s, org, user)
        nid = res.note_id

        # list_notes excludes it.
        listed = await nt.list_notes(s, org_id=org)
        assert nid not in {n.id for n in listed}
        # get_note 404s by default; the inbox bypass loads it.
        with pytest.raises(NotFoundError):
            await nt.get_note(s, org_id=org, note_id=nid)
        assert (await nt.get_note(s, org_id=org, note_id=nid, include_proposed=True)).id == nid
        # The @-lookup picker excludes it.
        matches = await lookup.resolve_prefix(s, prefix=str(nid)[:8], kinds=("note",))
        assert nid not in {m.id for m in matches}
        # The free-wander humus node set excludes it.
        assert nid not in await graph.humus_note_ids(s, org_id=org)
        # The unified-search note filter drops it even if its blob ranked.
        meta = await _note_filter_meta(
            s, note_ids=[nid], include_archived=True, include_deleted=False
        )
        assert nid not in meta
        # It IS in the review inbox, carrying the producing model.
        pending = await review.list_pending(s, org_id=org)
        row = next(p for p in pending if p.note_id == nid)
        assert row.origin_model_id == "fake-llm"
        assert row.humus_kind == "pattern"

    # Approve, then assert it re-enters every surface.
    async with tenant_session(str(org), str(user)) as s:
        await review.approve_node(s, org_id=org, actor_id=user, note_id=nid)
    async with tenant_session(str(org), str(user)) as s:
        assert nid in {n.id for n in await nt.list_notes(s, org_id=org, include_archived=True)}
        assert (await nt.get_note(s, org_id=org, note_id=nid)).id == nid
        assert nid in await graph.humus_note_ids(s, org_id=org)
        meta = await _note_filter_meta(
            s, note_ids=[nid], include_archived=True, include_deleted=False
        )
        assert nid in meta
        assert nid not in {p.note_id for p in await review.list_pending(s, org_id=org)}


async def test_proposed_humus_absent_from_walk_then_present(_wire: None, _gate_on: None) -> None:
    """The retrieval walk (memory.retrieve): the only humus note is the
    proposal, so no hit carries provenance 'humus' until it is approved."""
    org, user = await _org()
    query = "soil compost forecast synthesis"
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        res = await _make_proposed_pattern(s, org, user)
        nid = res.note_id
    # Indexed at teardown. Before approval: gated out of the humus source.
    async with tenant_session(str(org), str(user)) as s:
        hits = await memory.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query=query,
            operation_id=f"op-{uuid.uuid4().hex}",
            limit=10,
        )
        assert all(h.provenance != "humus" for h in hits)
    async with tenant_session(str(org), str(user)) as s:
        await review.approve_node(s, org_id=org, actor_id=user, note_id=nid)
    # After approval: the humus source surfaces it.
    async with tenant_session(str(org), str(user)) as s:
        hits = await memory.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query=query,
            operation_id=f"op-{uuid.uuid4().hex}",
            limit=10,
        )
        assert any(h.provenance == "humus" for h in hits)


# ── D3: approve / reject audited + on the bus ──────────────────────────────


async def test_approve_emits_commit_event_and_audit(_wire: None, _gate_on: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        nid = (await _make_proposed_pattern(s, org, user)).note_id
    async with tenant_session(str(org), str(user)) as s:
        note = await review.approve_node(s, org_id=org, actor_id=user, note_id=nid)
        assert note.review_state == "approved"
        ev = (
            await s.execute(
                select(EventOutbox).where(EventOutbox.node_id == nid, EventOutbox.kind == "commit")
            )
        ).scalar_one()
        assert ev.node_kind == "note"
        assert ev.payload["origin_model_id"] == "fake-llm"
        assert ev.applied_state == "committed"
        log = (
            await s.execute(
                select(ActivityLog).where(
                    ActivityLog.entity_id == nid,
                    ActivityLog.action == "garden_review:approve",
                )
            )
        ).scalar_one()
        assert log.diff["review_state"] == {"old": "proposed", "new": "approved"}


async def test_reject_soft_deletes_emits_event_and_reopens(_wire: None, _gate_on: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        res = await _make_proposed_pattern(s, org, user)
        nid = res.note_id
    async with tenant_session(str(org), str(user)) as s:
        note = await review.reject_node(
            s, org_id=org, actor_id=user, note_id=nid, reason="weak summary"
        )
        # Soft-deleted: never pollutes; gone from the inbox and listings.
        assert note.deleted_at is not None
        assert nid not in {p.note_id for p in await review.list_pending(s, org_id=org)}
        with pytest.raises(NotFoundError):
            await nt.get_note(s, org_id=org, note_id=nid, include_proposed=True)
        # Bus reject event carries the model (for the D4 accept-ratio later).
        ev = (
            await s.execute(
                select(EventOutbox).where(EventOutbox.node_id == nid, EventOutbox.kind == "reject")
            )
        ).scalar_one()
        assert ev.payload["origin_model_id"] == "fake-llm"
        assert ev.payload["reason"] == "weak summary"
        assert ev.applied_state == "rejected"
        log = (
            await s.execute(
                select(ActivityLog).where(
                    ActivityLog.entity_id == nid,
                    ActivityLog.action == "garden_review:reject",
                )
            )
        ).scalar_one()
        assert log is not None
    # The soft-delete reopens the humus signature: a re-run regenerates.
    async with tenant_session(str(org), str(user)) as s:
        again = await _make_proposed_pattern(s, org, user)
        assert again.created is True
        assert again.note_id != nid


# ── idempotency + error surface ────────────────────────────────────────────


async def test_approve_and_reject_are_idempotent(_wire: None, _gate_on: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        nid = (await _make_proposed_pattern(s, org, user)).note_id
    async with tenant_session(str(org), str(user)) as s:
        await review.approve_node(s, org_id=org, actor_id=user, note_id=nid)
        # Re-approve: a no-op, still approved (no second commit row).
        again = await review.approve_node(s, org_id=org, actor_id=user, note_id=nid)
        assert again.review_state == "approved"
        commits = (
            (
                await s.execute(
                    select(EventOutbox).where(
                        EventOutbox.node_id == nid, EventOutbox.kind == "commit"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(commits) == 1


async def test_review_of_non_proposed_note_is_404(_wire: None, _gate_on: None) -> None:
    """A plain (never-proposed) note is not a pending proposal: approving or
    rejecting it 404s, so the review surface can't touch ordinary notes."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        live = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="live", text="ordinary"
        )
        with pytest.raises(NotFoundError):
            await review.approve_node(s, org_id=org, actor_id=user, note_id=live.id)
        with pytest.raises(NotFoundError):
            await review.reject_node(s, org_id=org, actor_id=user, note_id=live.id)
        with pytest.raises(NotFoundError):
            await review.approve_node(s, org_id=org, actor_id=user, note_id=uuid.uuid4())


# ── D4: per-model accept ratio (earned-autonomy telemetry, 27f7726e) ───────


async def _proposed(s: object, org: uuid.UUID, user: uuid.UUID, *, model: str) -> uuid.UUID:
    """A 'proposed' node stamped with a given ``origin_model_id`` -- the gate
    column the review events carry (no decomposition needed to test the
    ratio)."""
    note = await nt.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title="p",
        text="a proposed body",
    )
    note.origin_model_id = model
    note.review_state = "proposed"
    await s.flush()  # type: ignore[attr-defined]
    return note.id


async def test_accept_ratio_by_model_counts_approve_and_reject() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        # model-x: 1 approve + 1 reject -> 0.5 ; model-y: 1 approve -> 1.0
        await review.approve_node(
            s, org_id=org, actor_id=user, note_id=await _proposed(s, org, user, model="model-x")
        )
        await review.reject_node(
            s, org_id=org, actor_id=user, note_id=await _proposed(s, org, user, model="model-x")
        )
        await review.approve_node(
            s, org_id=org, actor_id=user, note_id=await _proposed(s, org, user, model="model-y")
        )
        ratios = await review.accept_ratio_by_model(s, org_id=org)
        by = {m.model_id: m for m in ratios}
        assert (by["model-x"].approved, by["model-x"].rejected, by["model-x"].ratio) == (1, 1, 0.5)
        assert (by["model-y"].approved, by["model-y"].rejected, by["model-y"].ratio) == (1, 0, 1.0)
        # most-decided model first (model-x has 2 decisions, model-y has 1).
        assert ratios[0].model_id == "model-x"
        assert await review.accept_ratio_overall(s, org_id=org) == round(2 / 3, 4)


async def test_accept_ratio_is_none_without_reviews() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        assert await review.accept_ratio_overall(s, org_id=org) is None
        assert await review.accept_ratio_by_model(s, org_id=org) == []


async def test_garden_health_surfaces_the_accept_ratio_sensor() -> None:
    from mycelium_core.services import garden_health

    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        # No reviews yet -> the sensor reads "no signal", not 0%.
        h0 = await garden_health.compute_health(s, org_id=org)
        assert h0.autonomous_accept_ratio.value is None
        assert h0.autonomous_accept_ratio.reason
        # One approval -> ratio 1.0, with the reference floor attached.
        await review.approve_node(
            s, org_id=org, actor_id=user, note_id=await _proposed(s, org, user, model="m")
        )
        h1 = await garden_health.compute_health(s, org_id=org)
        assert h1.autonomous_accept_ratio.value == 1.0
        assert h1.autonomous_accept_ratio.floor == garden_health.AUTONOMOUS_ACCEPT_RATIO_FLOOR
