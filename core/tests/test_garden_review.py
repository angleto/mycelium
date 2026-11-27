"""Human-gated review state for AUTONOMOUSLY-generated nodes (ADR-0043,
task e87daff4).

Asserts the gate's invariants end-to-end:

- every summary the garden generates is born ``review_state='proposed'``,
  whoever asked for it, and is INVISIBLE on every
  retrieval/listing surface (the walk, search, list_notes, get_note, the
  @-lookup picker, the free-wander humus set) until a human approves it;
- the producing model is on the artifact (``origin_model_id``), not only in
  the transient MCP response;
- approve -> effective (re-enters every surface) + a bus ``commit`` event +
  an audit row; reject -> soft-delete (never pollutes) + a bus ``reject``
  event carrying ``origin_model_id`` + an audit row, and the humus signature
  reopens for regeneration.

What approving or rejecting a distillation does to its SOURCE is asserted in
``test_humus_on_approval.py``.
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
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import ConflictError, DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.models.event_outbox import EventOutbox
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.models.note_link import NoteNoteLink
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


async def _make_proposed_pattern(s: object, org: uuid.UUID, user: uuid.UUID) -> decomp.HumusResult:
    """Three archived sources -> one ``pattern`` humus synthesis."""
    a = await _archived(s, org, user, title="a", body="soil compost forecast synthesis alpha")
    b = await _archived(s, org, user, title="b", body="soil compost forecast synthesis beta")
    c = await _archived(s, org, user, title="c", body="soil compost forecast synthesis gamma")
    return await decomp.extract_cluster_pattern(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        source_note_ids=[a.id, b.id, c.id],
    )


# ── D1/D3: born proposed, model on the artifact ────────────────────────────


async def test_generation_is_born_proposed_with_model(_wire: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        res = await _make_proposed_pattern(s, org, user)
        note = (await s.execute(select(Note).where(Note.id == res.note_id))).scalar_one()
        # Withheld from retrieval (proposed), humus, model stamped.
        assert note.review_state == "proposed"
        assert note.humus_flag is True
        assert note.humus_kind == "pattern"
        assert note.origin_model_id == res.model_id == "fake-llm"


# ── D2: invisible on every listing surface until approved ──────────────────


async def test_proposed_note_hidden_from_listings_then_visible(_wire: None) -> None:
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
        assert (await nt.get_note(s, org_id=org, note_id=nid, include_gated=True)).id == nid
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


async def test_proposed_humus_absent_from_walk_then_present(_wire: None) -> None:
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


async def test_approve_emits_commit_event_and_audit(_wire: None) -> None:
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


async def test_decline_keeps_the_node_emits_event_and_reopens(_wire: None) -> None:
    """Declining (ADR-0075) keeps the node, gated: out of the inbox and of
    every ordinary read, never in the trash, listed among the declined and
    readable through the review surface. The bus event and the audit row
    are those a rejection always left."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        res = await _make_proposed_pattern(s, org, user)
        nid = res.note_id
    async with tenant_session(str(org), str(user)) as s:
        note = await review.decline_node(
            s, org_id=org, actor_id=user, note_id=nid, reason="weak summary"
        )
        assert note.review_state == "declined" and note.deleted_at is None
        assert note.review_decided_at is not None
        assert nid not in {p.note_id for p in await review.list_pending(s, org_id=org)}
        assert [r.note_id for r in await review.list_rejected(s, org_id=org)] == [nid]
        with pytest.raises(NotFoundError):
            await nt.get_note(s, org_id=org, note_id=nid)
        with pytest.raises(NotFoundError):
            await nt.get_note(s, org_id=org, note_id=nid, include_deleted=True)
        reading = await review.read_node(s, org_id=org, actor_id=user, note_id=nid)
        assert reading.review_state == "declined" and reading.text.body
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
    # A pattern over new sources is a new synthesis, declined one or not.
    async with tenant_session(str(org), str(user)) as s:
        again = await _make_proposed_pattern(s, org, user)
        assert again.created is True
        assert again.note_id != nid


# ── idempotency + error surface ────────────────────────────────────────────


async def test_approve_and_reject_are_idempotent(_wire: None) -> None:
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


async def test_review_of_non_proposed_note_is_404(_wire: None) -> None:
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
            await review.decline_node(s, org_id=org, actor_id=user, note_id=live.id)
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
        await review.decline_node(
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


async def test_the_declined_list_shows_what_was_declined_and_reopen_undoes_it() -> None:
    """A declined node is listed by its state, most recently declined
    first, and the ordinary trash is not part of it. The undo is
    ``reopen_node``, echoing the version the list serves; the old undo, the
    ordinary restore, answers 404 on a proposal like every ordinary door."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note_id = await _proposed(s, org, user, model="model-z")
        assert {p.note_id for p in await review.list_pending(s, org_id=org)} == {note_id}
        assert await review.list_rejected(s, org_id=org) == []

        # An ordinary trashed note is NOT a declined proposal.
        plain = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="plain", text="ordinary"
        )
        await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=plain.id,
            expected_version=plain.version,
        )
        older = await _proposed(s, org, user, model="model-z")
        await review.decline_node(s, org_id=org, actor_id=user, note_id=older)
        await review.decline_node(s, org_id=org, actor_id=user, note_id=note_id)
        assert await review.list_pending(s, org_id=org) == []
        binned = await review.list_rejected(s, org_id=org)
        # Most recently declined first, and the plain trashed note is absent.
        assert [r.note_id for r in binned] == [note_id, older]
        assert plain.id not in {r.note_id for r in binned}
        assert binned[0].origin_model_id == "model-z"
        assert binned[0].preview  # the body is readable to decide on the undo
        assert binned[0].rejected_at >= binned[0].created_at

        with pytest.raises(NotFoundError):
            await nt.restore_note(
                s, org_id=org, actor_id=user, note_id=note_id, expected_version=binned[0].version
            )
        # The undo: reopen, echoing the pin the list serves.
        await review.reopen_node(
            s,
            org_id=org,
            actor_id=user,
            note_id=note_id,
            expected_version=binned[0].version,
        )
        assert {r.note_id for r in await review.list_rejected(s, org_id=org)} == {older}
        assert {p.note_id for p in await review.list_pending(s, org_id=org)} == {note_id}


async def test_a_rejection_taken_back_stops_counting_against_the_model() -> None:
    """A reject the human undid is not a reject. Without the compensating
    event the sequence decline -> reopen -> approve would charge the model a
    decision that was reversed AND credit it with the approval, a worse
    reading of reliability than either outcome alone."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note_id = await _proposed(s, org, user, model="model-undo")
        await review.decline_node(s, org_id=org, actor_id=user, note_id=note_id)
        by = {m.model_id: m for m in await review.accept_ratio_by_model(s, org_id=org)}
        assert (by["model-undo"].approved, by["model-undo"].rejected) == (0, 1)

        version = (await review.list_rejected(s, org_id=org))[0].version
        await review.reopen_node(
            s, org_id=org, actor_id=user, note_id=note_id, expected_version=version
        )
        by = {m.model_id: m for m in await review.accept_ratio_by_model(s, org_id=org)}
        assert (by["model-undo"].approved, by["model-undo"].rejected) == (0, 0)

        # Second look, this time a yes: one decision, not one of each.
        note = (await s.execute(select(Note).where(Note.id == note_id))).scalar_one()
        await review.approve_node(
            s, org_id=org, actor_id=user, note_id=note_id, expected_version=note.version
        )
        by = {m.model_id: m for m in await review.accept_ratio_by_model(s, org_id=org)}
        assert (by["model-undo"].approved, by["model-undo"].rejected, by["model-undo"].ratio) == (
            1,
            0,
            1.0,
        )


async def test_approving_a_declined_node_is_one_decision_not_two() -> None:
    """Approving straight from the declined list (ADR-0075) takes the
    decline back first, so the model ends with one approval and no
    rejection, as after decline -> reopen -> approve."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note_id = await _proposed(s, org, user, model="model-direct")
        declined = await review.decline_node(s, org_id=org, actor_id=user, note_id=note_id)
        approved = await review.approve_node(
            s, org_id=org, actor_id=user, note_id=note_id, expected_version=declined.version
        )
        assert approved.review_state == "approved" and approved.review_decided_at is not None
        by = {m.model_id: m for m in await review.accept_ratio_by_model(s, org_id=org)}
        assert (by["model-direct"].approved, by["model-direct"].rejected) == (1, 0)
        assert await review.list_rejected(s, org_id=org) == []


async def test_reopen_and_remove_act_only_on_a_declined_node() -> None:
    """Reopen is a no-op on a pending node and a state conflict on an
    approved one; removal is a state conflict on anything but a declined
    node, so nothing is thrown away unread; both refuse a stale version."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        pending = await _proposed(s, org, user, model="m")
        v = (await s.get(Note, pending)).version  # type: ignore[union-attr]
        same = await review.reopen_node(
            s, org_id=org, actor_id=user, note_id=pending, expected_version=v
        )
        assert same.review_state == "proposed" and same.version == v
        with pytest.raises(ConflictError) as unread:
            await review.remove_declined(
                s, org_id=org, actor_id=user, note_id=pending, expected_version=v
            )
        assert unread.value.code is MessageCode.REVIEW_STATE_CONFLICT
        approved = await review.approve_node(s, org_id=org, actor_id=user, note_id=pending)
        with pytest.raises(ConflictError) as effective:
            await review.reopen_node(
                s, org_id=org, actor_id=user, note_id=pending, expected_version=approved.version
            )
        assert effective.value.code is MessageCode.REVIEW_STATE_CONFLICT

        other = await _proposed(s, org, user, model="m")
        d = await review.decline_node(s, org_id=org, actor_id=user, note_id=other)
        with pytest.raises(ConflictError):
            await review.remove_declined(
                s, org_id=org, actor_id=user, note_id=other, expected_version=d.version - 1
            )
        with pytest.raises(ConflictError):
            await review.reopen_node(
                s, org_id=org, actor_id=user, note_id=other, expected_version=d.version - 1
            )
        await review.remove_declined(
            s, org_id=org, actor_id=user, note_id=other, expected_version=d.version
        )
        assert await s.scalar(select(Note.id).where(Note.id == other)) is None
        with pytest.raises(NotFoundError):
            await review.read_node(s, org_id=org, actor_id=user, note_id=other)


async def test_an_ordinary_note_is_not_readable_as_a_proposal() -> None:
    """The review reading serves generated notes only: an ordinary note, or
    an id that does not exist, is 404 there."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        plain = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="plain", text="ordinary"
        )
        for nid in (plain.id, uuid.uuid4()):
            with pytest.raises(NotFoundError):
                await review.read_node(s, org_id=org, actor_id=user, note_id=nid)


async def test_a_second_rejection_after_an_undo_counts_again() -> None:
    """The decision keys carry the version for this: a node rejected, taken
    back and rejected again has been rejected twice as far as the model is
    concerned, and the 24h idempotency window must not swallow the second
    verdict as if it were a retry."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note_id = await _proposed(s, org, user, model="model-twice")
        await review.decline_node(s, org_id=org, actor_id=user, note_id=note_id)
        version = (await review.list_rejected(s, org_id=org))[0].version
        await review.reopen_node(
            s, org_id=org, actor_id=user, note_id=note_id, expected_version=version
        )
        await review.decline_node(s, org_id=org, actor_id=user, note_id=note_id)
        by = {m.model_id: m for m in await review.accept_ratio_by_model(s, org_id=org)}
        # reject(1) - unreject(1) + reject(1): the second verdict stands.
        assert (by["model-twice"].approved, by["model-twice"].rejected) == (0, 1)


async def test_undoing_an_unattributed_withdrawal_stays_unattributed() -> None:
    """``restore_source`` withdraws without charging the model, so taking
    THAT back must not credit one either: an undo that named a model would
    subtract a rejection the model never got, cancelling a real one from
    another node."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        # A real rejection for the model, on its own node.
        real = await _proposed(s, org, user, model="model-mix")
        await review.decline_node(s, org_id=org, actor_id=user, note_id=real)

        # A lens of the same model, withdrawn by restore_source.
        source = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="src", text="the source"
        )
        await nt.archive_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=source.id,
            archived=True,
            expected_version=source.version,
        )
        atom_id = await _proposed(s, org, user, model="model-mix")
        atom = (await s.execute(select(Note).where(Note.id == atom_id))).scalar_one()
        atom.humus_flag = True
        s.add(
            NoteNoteLink(
                org_id=org, parent_note_id=source.id, child_note_id=atom_id, kind="hypha_of"
            )
        )
        await s.flush()
        await review.approve_node(s, org_id=org, actor_id=user, note_id=atom_id)
        await review.restore_source(s, org_id=org, actor_id=user, atom_note_id=atom_id)
        by = {m.model_id: m for m in await review.accept_ratio_by_model(s, org_id=org)}
        assert (by["model-mix"].approved, by["model-mix"].rejected) == (1, 1)

        # Undo the WITHDRAWAL: the real rejection on the other node stands.
        binned = {r.note_id: r for r in await review.list_rejected(s, org_id=org)}
        await review.reopen_node(
            s,
            org_id=org,
            actor_id=user,
            note_id=atom_id,
            expected_version=binned[atom_id].version,
        )
        by = {m.model_id: m for m in await review.accept_ratio_by_model(s, org_id=org)}
        assert (by["model-mix"].approved, by["model-mix"].rejected) == (1, 1)


async def test_retiring_a_lens_is_not_a_verdict_on_its_model() -> None:
    """``restore_source`` retires an approved atom to bring its sources back
    up. That is a choice about which layer to look at, so it stays in the
    audit trail and out of the model's accept ratio."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        source = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="src", text="the source"
        )
        await nt.archive_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=source.id,
            archived=True,
            expected_version=source.version,
        )
        atom_id = await _proposed(s, org, user, model="model-lens")
        atom = (await s.execute(select(Note).where(Note.id == atom_id))).scalar_one()
        atom.humus_flag = True
        s.add(
            NoteNoteLink(
                org_id=org, parent_note_id=source.id, child_note_id=atom_id, kind="hypha_of"
            )
        )
        await s.flush()
        await review.approve_node(s, org_id=org, actor_id=user, note_id=atom_id)
        by = {m.model_id: m for m in await review.accept_ratio_by_model(s, org_id=org)}
        assert (by["model-lens"].approved, by["model-lens"].rejected) == (1, 0)

        res = await review.restore_source(s, org_id=org, actor_id=user, atom_note_id=atom_id)
        assert res.atom_retired and res.restored_source_ids == [source.id]
        by = {m.model_id: m for m in await review.accept_ratio_by_model(s, org_id=org)}
        assert (by["model-lens"].approved, by["model-lens"].rejected) == (1, 0)
        # The withdrawal is still on the record, just not attributed.
        withdrawn = (
            (
                await s.execute(
                    select(EventOutbox).where(
                        EventOutbox.node_id == atom_id,
                        EventOutbox.payload["review"].astext == "reject",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(withdrawn) == 1
        assert withdrawn[0].payload["origin_model_id"] is None


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


# ── TOCTOU guard: expected_version on approve/reject (task 2e36e732) ────────


async def test_approve_with_stale_version_conflicts(_wire: None) -> None:
    """The reviewer reads v, the note changes (any edit bumps the counter),
    the approve pinned to v must fail with ``stale_version``: the human never
    blesses content they have not seen."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        nid = (await _make_proposed_pattern(s, org, user)).note_id
    async with tenant_session(str(org), str(user)) as s:
        note = await s.get(Note, nid)
        assert note is not None
        seen = note.version
        # Any interposed edit (an agent touching distilled_text, a rename...)
        # bumps the counter; the ORM bump stands in for every such path.
        note.version += 1
        await s.flush()
        with pytest.raises(ConflictError):
            await review.approve_node(
                s, org_id=org, actor_id=user, note_id=nid, expected_version=seen
            )
        # The node is untouched: still proposed, approvable with a FRESH view.
        fresh = await review.approve_node(
            s, org_id=org, actor_id=user, note_id=nid, expected_version=note.version
        )
        assert fresh.review_state == "approved"


async def test_approve_bumps_version_and_second_pinned_approve_is_stale(_wire: None) -> None:
    """Concurrent double-approve semantics pinned: with the same read version,
    exactly one transition wins (and bumps the counter); the second pinned
    approve gets ``stale_version``. An UNPINNED retry stays idempotent."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        nid = (await _make_proposed_pattern(s, org, user)).note_id
    async with tenant_session(str(org), str(user)) as s:
        note = await s.get(Note, nid)
        assert note is not None
        seen = note.version
        first = await review.approve_node(
            s, org_id=org, actor_id=user, note_id=nid, expected_version=seen
        )
        assert first.review_state == "approved"
        assert first.version == seen + 1
        with pytest.raises(ConflictError):
            await review.approve_node(
                s, org_id=org, actor_id=user, note_id=nid, expected_version=seen
            )
        # Legacy/None path: idempotent no-op, unchanged behaviour.
        again = await review.approve_node(s, org_id=org, actor_id=user, note_id=nid)
        assert again.review_state == "approved"
        assert again.version == seen + 1


async def test_reject_with_stale_version_conflicts(_wire: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        nid = (await _make_proposed_pattern(s, org, user)).note_id
    async with tenant_session(str(org), str(user)) as s:
        note = await s.get(Note, nid)
        assert note is not None
        seen = note.version
        note.version += 1
        await s.flush()
        with pytest.raises(ConflictError):
            await review.decline_node(
                s, org_id=org, actor_id=user, note_id=nid, expected_version=seen
            )
        rejected = await review.decline_node(
            s, org_id=org, actor_id=user, note_id=nid, expected_version=note.version
        )
        assert rejected.review_state == "declined" and rejected.deleted_at is None
        assert rejected.version == seen + 2  # interposed bump + decline bump


async def test_list_pending_serves_the_version_pin(_wire: None) -> None:
    """The inbox row carries the version the reviewer must echo back."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        nid = (await _make_proposed_pattern(s, org, user)).note_id
    async with tenant_session(str(org), str(user)) as s:
        note = await s.get(Note, nid)
        assert note is not None
        rows = await review.list_pending(s, org_id=org)
        mine = next(r for r in rows if r.note_id == nid)
        assert mine.version == note.version


# ── ADR-0076: checks and revisions by an agent ─────────────────────────────


def _finding(status: str) -> review.Finding:
    from mycelium_core.models.proposal_check import ClaimStatus

    return review.Finding(claim="a claim", status=ClaimStatus(status), evidence="", comment="")


async def test_a_check_must_agree_with_its_findings_and_decides_nothing() -> None:
    """``supported`` needs every finding supported, ``issues`` at least one
    that is not, and an empty check is refused. A check moves no state."""
    from mycelium_core.models.proposal_check import CheckVerdict

    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        nid = await _proposed(s, org, user, model="m")
        v = (await s.get(Note, nid)).version  # type: ignore[union-attr]
        for verdict, findings in (
            (CheckVerdict.supported, [_finding("supported"), _finding("unclear")]),
            (CheckVerdict.issues, [_finding("supported")]),
            (CheckVerdict.issues, []),
        ):
            with pytest.raises(DomainError):
                await review.submit_check(
                    s,
                    org_id=org,
                    actor_id=user,
                    note_id=nid,
                    expected_version=v,
                    verdict=verdict,
                    summary="s",
                    findings=findings,
                    checker_model_id=None,
                    checker_identity_id=None,
                )
        with pytest.raises(ConflictError):
            await review.submit_check(
                s,
                org_id=org,
                actor_id=user,
                note_id=nid,
                expected_version=v - 1,
                verdict=CheckVerdict.supported,
                summary="s",
                findings=[_finding("supported")],
                checker_model_id=None,
                checker_identity_id=None,
            )
        check = await review.submit_check(
            s,
            org_id=org,
            actor_id=user,
            note_id=nid,
            expected_version=v,
            verdict=CheckVerdict.supported,
            summary="all grounded",
            findings=[_finding("supported")],
            checker_model_id="m-check",
            checker_identity_id=None,
        )
        # A person's check is attributed to the person's own identity.
        assert check.checker_identity_id is not None
        note = await s.get(Note, nid)
        assert note is not None and note.review_state == "proposed" and note.version == v
        reading = await review.read_node(s, org_id=org, actor_id=user, note_id=nid)
        assert [(c.verdict, c.stale) for c in reading.checks] == [("supported", False)]


async def test_revising_a_declined_note_leaves_it_declined_and_an_approved_one_is_refused() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        nid = await _proposed(s, org, user, model="m")
        declined = await review.decline_node(s, org_id=org, actor_id=user, note_id=nid)
        rev = await review.revise_node(
            s,
            org_id=org,
            actor_id=user,
            note_id=nid,
            expected_version=declined.version,
            text="better",
            origin_model_id="strong",
        )
        assert rev.review_state == "proposed" and rev.revises_note_id == nid
        original = await s.get(Note, nid)
        assert original is not None and original.review_state == "declined"
        # The person's decline counted once; the revision added nothing.
        by = {m.model_id: m for m in await review.accept_ratio_by_model(s, org_id=org)}
        assert (by["m"].approved, by["m"].rejected) == (0, 1)

        approved = await review.approve_node(s, org_id=org, actor_id=user, note_id=rev.id)
        with pytest.raises(ConflictError) as refused:
            await review.revise_node(
                s,
                org_id=org,
                actor_id=user,
                note_id=rev.id,
                expected_version=approved.version,
                text="again",
                origin_model_id="strong",
            )
        assert refused.value.code is MessageCode.REVIEW_STATE_CONFLICT
        assert refused.value.params["state"] == "approved"
        with pytest.raises(DomainError):
            await review.revise_node(
                s,
                org_id=org,
                actor_id=user,
                note_id=nid,
                expected_version=original.version,
                text="   ",
                origin_model_id="strong",
            )


async def test_an_agent_writes_a_pattern_with_its_own_model(_wire: None) -> None:
    """``synthesized_text`` skips the internal model, as ``distilled_text``
    does for a distillation, and records ``external:`` provenance."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        ids = [
            (await _archived(s, org, user, title=t, body=f"soil compost {t}")).id
            for t in ("x", "y")
        ]
        res = await decomp.extract_cluster_pattern(
            s,
            org_id=org,
            actor_id=user,
            source_note_ids=ids,
            synthesized_text="Two notes about soil, and what recurs in both.",
            origin_model_id="claude-test",
        )
        assert res.created and res.model_id == "external:claude-test"
        reading = await review.read_node(s, org_id=org, actor_id=user, note_id=res.note_id)
        assert reading.text.body == "Two notes about soil, and what recurs in both."
        assert reading.review_state == "proposed"
