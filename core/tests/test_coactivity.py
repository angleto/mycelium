"""Co-activity, the third note_edge_strength source (task f0a15247).

Covers the aggregation worker (services/coactivity.refresh_coactivity)
and the read-side plug into services/graph.compute_note_edge_weights:

- two notes touched in one session get a co-activity edge with weight > 0
  even with NO manual link and NO shared tag (the gap the source closes);
- sessions split on the time gap; repeated co-activity raises the count;
- system actors and non-touch actions (create/archive) never forge edges;
- soft-deleted notes are dropped; the per-org replace is idempotent;
- ABSENCE of co-activity is a byte-for-byte no-op vs the link+tag weave;
- the soft-OR still saturates when all three sources stack on one pair;
- the snapshot signature folds in co-activity so analytics don't go stale.

All against the real DB, mirroring test_graph_snapshot.py.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import select

from flow_core.db import admin_session, tenant_session
from flow_core.models.activity_log import ActivityLog
from flow_core.models.note import Note, NoteKind
from flow_core.models.note_coactivity import NoteCoactivity
from flow_core.models.note_part import NotePart
from flow_core.services import coactivity, note_links
from flow_core.services import graph as graph_svc
from flow_core.services import graph_snapshot as snap_svc
from flow_core.services import notes as notes_svc
from flow_core.services.auth import signup


async def _org_user() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="CA",
        )
    return a.org_id, a.user_id


async def _note(s: object, org: uuid.UUID, user: uuid.UUID, title: str) -> Note:
    return await notes_svc.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=f"body {title}",
    )


def _touch(
    s: object,
    *,
    org: uuid.UUID,
    note_id: uuid.UUID,
    ts: datetime.datetime,
    actor_id: uuid.UUID,
    actor_kind: str = "human_direct",
    action: str = "update",
    entity: str = "note",
) -> None:
    """Append a controlled touch event to the activity log. ``note_id`` is
    the entity id (a note id for entity='note', a part id for
    entity='note_part')."""
    s.add(  # type: ignore[attr-defined]
        ActivityLog(
            org_id=org,
            actor_id=actor_id,
            actor_kind=actor_kind,
            entity=entity,
            entity_id=note_id,
            action=action,
            ts=ts,
        )
    )


async def _coact_rows(s: object, org: uuid.UUID) -> dict[tuple[str, str], NoteCoactivity]:
    rows = (
        (await s.execute(select(NoteCoactivity).where(NoteCoactivity.org_id == org)))  # type: ignore[attr-defined]
        .scalars()
        .all()
    )
    return {(str(r.note_a_id), str(r.note_b_id)): r for r in rows}


def _key(a: Note, b: Note) -> tuple[str, str]:
    return (str(a.id), str(b.id)) if str(a.id) <= str(b.id) else (str(b.id), str(a.id))


async def test_one_session_makes_a_coactivity_edge_without_link_or_tag() -> None:
    """The core promise: two notes touched together get a weighted edge
    with no manual link and no shared tag connecting them."""
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    t0 = now - datetime.timedelta(days=1)
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        c = await _note(s, org, user, "c")  # never touched -> isolated
        _touch(s, org=org, note_id=a.id, ts=t0, actor_id=user)
        _touch(s, org=org, note_id=b.id, ts=t0 + datetime.timedelta(minutes=5), actor_id=user)
        await s.flush()

        n_pairs = await coactivity.refresh_coactivity(s, org_id=org, now=now)
        assert n_pairs == 1
        rows = await _coact_rows(s, org)
        assert set(rows) == {_key(a, b)}
        assert rows[_key(a, b)].session_count == 1

        weights = await _weave(s, org)
    # a-b carries the co-activity weight; c stays isolated.
    assert weights[_key(a, b)] == pytest.approx(graph_svc.coactivity_weight(1))
    assert _key(a, c) not in weights and _key(b, c) not in weights


def _pair_str(e: graph_svc.EdgeWeight) -> tuple[str, str]:
    return (str(e.src), str(e.dst)) if str(e.src) <= str(e.dst) else (str(e.dst), str(e.src))


async def _weave(s: object, org: uuid.UUID) -> dict[tuple[str, str], float]:
    """The org's edge weave keyed by canonical pair -> weight."""
    edges = await graph_svc.compute_note_edge_weights(s, org_id=org)  # type: ignore[arg-type]
    return {_pair_str(e): e.weight for e in edges}


async def test_sessions_split_on_the_gap_and_count_accumulates() -> None:
    """Touches inside the gap are one session; a longer gap starts a new
    one. The pair's session_count is the number of shared sessions."""
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    base = now - datetime.timedelta(days=2)
    gap = coactivity.COACTIVITY_SESSION_GAP
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        # Session 1: a, b within the gap.
        _touch(s, org=org, note_id=a.id, ts=base, actor_id=user)
        _touch(s, org=org, note_id=b.id, ts=base + datetime.timedelta(minutes=1), actor_id=user)
        # Session 2: a, b again after a > gap break.
        s2 = base + gap + datetime.timedelta(hours=1)
        _touch(s, org=org, note_id=a.id, ts=s2, actor_id=user)
        _touch(s, org=org, note_id=b.id, ts=s2 + datetime.timedelta(minutes=2), actor_id=user)
        await s.flush()

        await coactivity.refresh_coactivity(s, org_id=org, now=now)
        rows = await _coact_rows(s, org)
        assert rows[_key(a, b)].session_count == 2
        # last_coactive_at reflects the second (later) session.
        assert rows[_key(a, b)].last_coactive_at >= s2


async def test_system_actor_and_nontouch_actions_never_forge_edges() -> None:
    """A system sweep touching many notes in one tick, and create/archive
    events, must not manufacture co-activity (the spurious-clique guard)."""
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    t0 = now - datetime.timedelta(hours=3)
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        # system actor (a worker re-tiering both notes at once): excluded.
        _touch(s, org=org, note_id=a.id, ts=t0, actor_id=user, actor_kind="system")
        _touch(s, org=org, note_id=b.id, ts=t0, actor_id=user, actor_kind="system")
        # human, but non-touch actions (create / archive): excluded.
        _touch(s, org=org, note_id=a.id, ts=t0, actor_id=user, action="create")
        _touch(s, org=org, note_id=b.id, ts=t0, actor_id=user, action="archive")
        await s.flush()
        n_pairs = await coactivity.refresh_coactivity(s, org_id=org, now=now)
        rows = await _coact_rows(s, org)
    assert n_pairs == 0 and rows == {}


async def test_two_actors_only_pair_within_their_own_session() -> None:
    """Co-activity is per-actor: two people each touching a different
    second note in overlapping wall-clock time do not cross-pair."""
    org, owner = await _org_user()
    # A second identity in the same org.
    async with admin_session() as s:
        other = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="CA2",
        )
    user2 = other.user_id
    now = datetime.datetime.now(datetime.UTC)
    t0 = now - datetime.timedelta(hours=2)
    async with tenant_session(str(org), str(owner)) as s:
        a = await _note(s, org, owner, "a")
        b = await _note(s, org, owner, "b")
        c = await _note(s, org, owner, "c")
        # owner works a+b; user2 works a+c, same wall-clock window.
        _touch(s, org=org, note_id=a.id, ts=t0, actor_id=owner)
        _touch(s, org=org, note_id=b.id, ts=t0 + datetime.timedelta(minutes=2), actor_id=owner)
        _touch(s, org=org, note_id=a.id, ts=t0, actor_id=user2)
        _touch(s, org=org, note_id=c.id, ts=t0 + datetime.timedelta(minutes=2), actor_id=user2)
        await s.flush()
        await coactivity.refresh_coactivity(s, org_id=org, now=now)
        rows = await _coact_rows(s, org)
    # a-b (owner) and a-c (user2) but NOT b-c (different actors).
    assert set(rows) == {_key(a, b), _key(a, c)}


async def test_softdeleted_note_is_dropped_and_replace_is_idempotent() -> None:
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    t0 = now - datetime.timedelta(hours=5)
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        _touch(s, org=org, note_id=a.id, ts=t0, actor_id=user)
        _touch(s, org=org, note_id=b.id, ts=t0 + datetime.timedelta(minutes=3), actor_id=user)
        await s.flush()
        assert await coactivity.refresh_coactivity(s, org_id=org, now=now) == 1
        # Re-running over the same window yields the same single row.
        assert await coactivity.refresh_coactivity(s, org_id=org, now=now) == 1
        assert len(await _coact_rows(s, org)) == 1
        # Soft-delete b -> its pair must vanish on the next refresh.
        b.deleted_at = now
        await s.flush()
        assert await coactivity.refresh_coactivity(s, org_id=org, now=now) == 0
        assert await _coact_rows(s, org) == {}


async def test_absent_coactivity_is_a_noop_on_edge_weights() -> None:
    """With no co-activity materialised, the edge weave is identical to
    the link+tag-only result (the additive, retro-compatible guarantee)."""
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        await note_links.link_notes(
            s, org_id=org, actor_id=user, parent_note_id=a.id, child_note_id=b.id, kind="related"
        )
        before = await _weave(s, org)
        # An empty refresh writes nothing.
        assert await coactivity.refresh_coactivity(s, org_id=org) == 0
        after = await _weave(s, org)
    assert before == after
    assert before[_key(a, b)] == pytest.approx(graph_svc._kind_base_weight("related"))


async def test_softor_saturates_with_all_three_sources() -> None:
    """Co-activity stacks into the soft-OR next to the kind base and the
    shared-tag overlap; the combined weight stays in [0, 1] and exceeds
    any single source."""
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    t0 = now - datetime.timedelta(hours=1)
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        # Source 1: a related link.
        await note_links.link_notes(
            s, org_id=org, actor_id=user, parent_note_id=a.id, child_note_id=b.id, kind="related"
        )
        # Source 3: a co-activity session.
        _touch(s, org=org, note_id=a.id, ts=t0, actor_id=user)
        _touch(s, org=org, note_id=b.id, ts=t0 + datetime.timedelta(minutes=4), actor_id=user)
        await s.flush()
        await coactivity.refresh_coactivity(s, org_id=org, now=now)
        weave = await _weave(s, org)
    w = weave[_key(a, b)]
    w_link = graph_svc._kind_base_weight("related")
    w_coact = graph_svc.coactivity_weight(1)
    assert w == pytest.approx(graph_svc.softor([w_link, w_coact]))
    assert w_link < w <= 1.0


async def test_signature_folds_in_coactivity() -> None:
    """The snapshot signature must change when co-activity changes, or the
    materialised analytics would ignore the fresh edges."""
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    t0 = now - datetime.timedelta(hours=1)
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        sig0 = await snap_svc.graph_signature(s, org_id=org)
        _touch(s, org=org, note_id=a.id, ts=t0, actor_id=user)
        _touch(s, org=org, note_id=b.id, ts=t0 + datetime.timedelta(minutes=2), actor_id=user)
        await s.flush()
        await coactivity.refresh_coactivity(s, org_id=org, now=now)
        sig1 = await snap_svc.graph_signature(s, org_id=org)
        # A no-op re-materialise of the same window leaves the signature put.
        await coactivity.refresh_coactivity(s, org_id=org, now=now)
        sig2 = await snap_svc.graph_signature(s, org_id=org)
    assert sig0 != sig1
    assert sig1 == sig2


async def test_oversized_session_is_dropped_but_at_cap_is_kept() -> None:
    """A session touching > _MAX_SESSION_NOTES notes is a bulk op and must
    forge NO edges (the N²/2 fake-clique guard). A session exactly at the
    cap still produces its pairs."""
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    cap = coactivity._MAX_SESSION_NOTES
    async with tenant_session(str(org), str(user)) as s:
        # cap + 1 notes touched in one tight window -> dropped whole.
        big = [await _note(s, org, user, f"big{i}") for i in range(cap + 1)]
        base = now - datetime.timedelta(days=1)
        for i, nt in enumerate(big):
            _touch(
                s, org=org, note_id=nt.id, ts=base + datetime.timedelta(seconds=i), actor_id=user
            )
        await s.flush()
        assert await coactivity.refresh_coactivity(s, org_id=org, now=now) == 0

        # A second, separate session of exactly `cap` notes -> kept.
        ok = [await _note(s, org, user, f"ok{i}") for i in range(cap)]
        base2 = now - datetime.timedelta(hours=3)
        for i, nt in enumerate(ok):
            _touch(
                s, org=org, note_id=nt.id, ts=base2 + datetime.timedelta(seconds=i), actor_id=user
            )
        await s.flush()
        n_pairs = await coactivity.refresh_coactivity(s, org_id=org, now=now)
    # Only the at-cap session contributes: C(cap, 2) pairs, none from big.
    assert n_pairs == cap * (cap - 1) // 2


async def test_part_edits_contribute_coactivity_resolved_to_note() -> None:
    """Editing two notes' non-ord-0 parts (entity='note_part') makes the
    owning notes co-active — multi-part work is not invisible."""
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    t0 = now - datetime.timedelta(hours=2)
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        part_a = NotePart(org_id=org, note_id=a.id, ord=1, body="a.1")
        part_b = NotePart(org_id=org, note_id=b.id, ord=1, body="b.1")
        s.add_all([part_a, part_b])
        await s.flush()
        # Only part-level touches (entity='note_part'), no note-level rows.
        _touch(
            s,
            org=org,
            note_id=part_a.id,
            ts=t0,
            actor_id=user,
            entity="note_part",
            action="update",
        )
        _touch(
            s,
            org=org,
            note_id=part_b.id,
            ts=t0 + datetime.timedelta(minutes=3),
            actor_id=user,
            entity="note_part",
            action="append",
        )
        await s.flush()
        n_pairs = await coactivity.refresh_coactivity(s, org_id=org, now=now)
        rows = await _coact_rows(s, org)
    # The part touches resolve to notes a and b -> one co-activity edge.
    assert n_pairs == 1
    assert set(rows) == {_key(a, b)}
