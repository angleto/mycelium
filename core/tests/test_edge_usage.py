"""Fase 2 of the search-informed graph (task 561c6aca): the
``refresh_edge_usage`` aggregation and its two consumers.

- pairing rule: ranking-adjacent + top-anchored (O(m), never the k²/2
  clique), note-granularity dedup (a multi-blob note never pairs with
  itself), oversized-trace guard;
- direction tallies relative to the canonical pair order;
- decay + retention: window-edge traces weigh less, aged-out traces are
  deleted; probe rows are never consumed;
- full per-org replace, idempotent for a fixed ``now``;
- the fourth soft-OR input: a populated ``note_edge_usage`` feeds
  ``compute_note_edge_weights`` AND ``graph_local.local_edges`` with the
  same weight (parity), while the empty-table no-op stays pinned in
  test_retrieval_trace;
- the ``link_direct`` candidate: proposed only above the traffic +
  asymmetry gates, never for structurally-linked pairs.

All against the real DB, mirroring test_coactivity.py.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import func, select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.models.note import NoteKind
from mycelium_core.models.note_edge_usage import NoteEdgeUsage
from mycelium_core.models.note_part import NotePart
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.models.retrieval_trace import RetrievalTrace
from mycelium_core.services import candidates, edge_usage, graph, graph_local
from mycelium_core.services import note_links as nl
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup
from mycelium_core.services.graph import _pair_key, _usage_weight

NOW = datetime.datetime(2026, 7, 1, 12, 0, tzinfo=datetime.UTC)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org(name: str = "EUSE") -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name=name)
    return r.org_id, r.user_id


async def _indexed_note(
    s: object, org: uuid.UUID, user: uuid.UUID, title: str, *, blobs: int = 1
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """A note with ``blobs`` indexed part blobs (create_note in this
    direct-service context does not index parts; the aggregation resolves
    blob->note via NotePartIndexPointer, so the pointers are seeded
    explicitly, same helper shape as test_graph_local)."""
    n = await nt.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=f"body of {title}",
    )
    blob_ids: list[uuid.UUID] = []
    for i in range(blobs):
        part = NotePart(org_id=org, note_id=n.id, ord=i + 1, body=f"{title} part {i}")
        s.add(part)  # type: ignore[attr-defined]
        await s.flush()  # type: ignore[attr-defined]
        blob = MemoryBlob(org_id=org, namespace="note", text=f"{title} chunk {i}")
        s.add(blob)  # type: ignore[attr-defined]
        await s.flush()  # type: ignore[attr-defined]
        s.add(  # type: ignore[attr-defined]
            NotePartIndexPointer(
                part_id=part.id, note_id=n.id, org_id=org, blob_id=blob.id, content_hash="x"
            )
        )
        await s.flush()  # type: ignore[attr-defined]
        blob_ids.append(blob.id)
    return n.id, blob_ids


def _trace(
    org: uuid.UUID,
    blob_ids: list[uuid.UUID],
    created_at: datetime.datetime,
    *,
    probe: bool = False,
) -> RetrievalTrace:
    return RetrievalTrace(
        org_id=org,
        items=[{"blob_id": str(b), "rank": i + 1} for i, b in enumerate(blob_ids)],
        is_probe=probe,
        created_at=created_at,
    )


async def _usage_rows(
    s: object, org: uuid.UUID
) -> dict[tuple[uuid.UUID, uuid.UUID], NoteEdgeUsage]:
    rows = (
        (await s.execute(select(NoteEdgeUsage).where(NoteEdgeUsage.org_id == org)))  # type: ignore[attr-defined]
        .scalars()
        .all()
    )
    return {(r.note_a_id, r.note_b_id): r for r in rows}


async def test_pairing_is_adjacent_plus_top_anchored() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        na, ba = await _indexed_note(s, org, user, "A")
        nb, bb = await _indexed_note(s, org, user, "B")
        nc, bc = await _indexed_note(s, org, user, "C")
        nd, bd = await _indexed_note(s, org, user, "D")
        s.add(_trace(org, [ba[0], bb[0], bc[0], bd[0]], NOW))
        await s.flush()
        n_pairs = await edge_usage.refresh_edge_usage(s, org_id=org, now=NOW)
        # ranks A>B>C>D: adjacent (A,B),(B,C),(C,D) + anchored (A,C),(A,D)
        # -- 2m-3 = 5 pairs, NOT the 6-pair clique (no (B,D)).
        assert n_pairs == 5
        rows = await _usage_rows(s, org)
        expected = {
            _pair_key(na, nb),
            _pair_key(nb, nc),
            _pair_key(nc, nd),
            _pair_key(na, nc),
            _pair_key(na, nd),
        }
        assert set(rows) == expected
        assert _pair_key(nb, nd) not in rows
        for _pk, row in rows.items():
            assert row.traversal_count == 1
            # One fresh trace: full decay mass.
            assert abs(row.decay_score - 1.0) < 1e-9
            assert row.forward_count + row.backward_count == 1
            assert row.last_traversed_at == NOW


async def test_multiblob_note_dedupes_and_never_self_pairs() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        na, ba = await _indexed_note(s, org, user, "A", blobs=2)
        nb, bb = await _indexed_note(s, org, user, "B")
        s.add(_trace(org, [ba[0], ba[1], bb[0]], NOW))
        await s.flush()
        assert await edge_usage.refresh_edge_usage(s, org_id=org, now=NOW) == 1
        rows = await _usage_rows(s, org)
        (row,) = rows.values()
        assert {row.note_a_id, row.note_b_id} == {na, nb}
        assert row.traversal_count == 1


async def test_oversized_trace_is_dropped_whole() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        blob_ids: list[uuid.UUID] = []
        for i in range(26):  # > _MAX_TRACE_NOTES
            _nid, bids = await _indexed_note(s, org, user, f"N{i}")
            blob_ids.append(bids[0])
        s.add(_trace(org, blob_ids, NOW))
        await s.flush()
        assert await edge_usage.refresh_edge_usage(s, org_id=org, now=NOW) == 0


async def test_direction_tallies_follow_rank_order() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        na, ba = await _indexed_note(s, org, user, "A")
        nb, bb = await _indexed_note(s, org, user, "B")
        s.add(_trace(org, [ba[0], bb[0]], NOW - datetime.timedelta(hours=2)))
        s.add(_trace(org, [ba[0], bb[0]], NOW - datetime.timedelta(hours=1)))
        s.add(_trace(org, [bb[0], ba[0]], NOW))
        await s.flush()
        assert await edge_usage.refresh_edge_usage(s, org_id=org, now=NOW) == 1
        rows = await _usage_rows(s, org)
        (row,) = rows.values()
        assert row.traversal_count == 3
        pk = _pair_key(na, nb)
        # A ranked above B twice, B above A once; forward = canonical-a
        # above canonical-b.
        a_above, b_above = (2, 1) if pk[0] == na else (1, 2)
        assert (row.forward_count, row.backward_count) == (a_above, b_above)
        assert row.last_traversed_at == NOW


async def test_decay_window_and_retention() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        _na, ba = await _indexed_note(s, org, user, "A")
        _nb, bb = await _indexed_note(s, org, user, "B")
        s.add(_trace(org, [ba[0], bb[0]], NOW))
        s.add(_trace(org, [ba[0], bb[0]], NOW - datetime.timedelta(days=30)))
        s.add(_trace(org, [ba[0], bb[0]], NOW - datetime.timedelta(days=100)))  # aged out
        await s.flush()
        assert await edge_usage.refresh_edge_usage(s, org_id=org, now=NOW) == 1
        rows = await _usage_rows(s, org)
        (row,) = rows.values()
        # Fresh (1.0) + one-half-life-old (0.5); the 100-day trace is
        # outside the 90-day window.
        assert row.traversal_count == 2
        assert abs(row.decay_score - 1.5) < 1e-9
        # Retention: the aged trace is gone, the two in-window rows stay
        # (the source is re-folded on every run, not consumed).
        n_traces = (
            await s.execute(
                select(func.count()).select_from(RetrievalTrace).where(RetrievalTrace.org_id == org)
            )
        ).scalar_one()
        assert n_traces == 2


async def test_probe_traces_are_never_consumed() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        _na, ba = await _indexed_note(s, org, user, "A")
        _nb, bb = await _indexed_note(s, org, user, "B")
        s.add(_trace(org, [ba[0], bb[0]], NOW, probe=True))
        await s.flush()
        assert await edge_usage.refresh_edge_usage(s, org_id=org, now=NOW) == 0
        # The in-window probe row itself is kept (only aged rows are
        # retention-deleted), it just never becomes demand.
        n_traces = (
            await s.execute(
                select(func.count()).select_from(RetrievalTrace).where(RetrievalTrace.org_id == org)
            )
        ).scalar_one()
        assert n_traces == 1


async def test_replace_is_idempotent_and_ages_out() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        _na, ba = await _indexed_note(s, org, user, "A")
        _nb, bb = await _indexed_note(s, org, user, "B")
        s.add(_trace(org, [ba[0], bb[0]], NOW))
        await s.flush()
        assert await edge_usage.refresh_edge_usage(s, org_id=org, now=NOW) == 1
        assert await edge_usage.refresh_edge_usage(s, org_id=org, now=NOW) == 1
        assert len(await _usage_rows(s, org)) == 1
        # The projection empties when the window moves past the trace.
        later = NOW + datetime.timedelta(days=91)
        assert await edge_usage.refresh_edge_usage(s, org_id=org, now=later) == 0
        assert len(await _usage_rows(s, org)) == 0


async def test_usage_feeds_edge_weights_and_local_parity() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        na, ba = await _indexed_note(s, org, user, "A")
        nb, bb = await _indexed_note(s, org, user, "B")
        s.add(_trace(org, [ba[0], bb[0]], NOW))
        await s.flush()
        await edge_usage.refresh_edge_usage(s, org_id=org, now=NOW)
        rows = await _usage_rows(s, org)
        (row,) = rows.values()
        expected_w = _usage_weight(row.decay_score)
        assert expected_w > 0
        # Full builder: the pair surfaces on search demand alone (no
        # link, no shared tag, no co-activity) -- independent evidence,
        # exactly like the co-activity source.
        weave = await graph.compute_note_edge_weights(s, org_id=org)
        by_pair = {_pair_key(e.src, e.dst): e.weight for e in weave}
        assert abs(by_pair[_pair_key(na, nb)] - expected_w) < 1e-12
        # Local provider parity (the Fase 1 contract, now with the 4th
        # input active).
        local = await graph_local.local_edges(s, org_id=org, note_id=na)
        assert abs(local[nb] - expected_w) < 1e-12


async def test_link_direct_gates_and_direction() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        na, ba = await _indexed_note(s, org, user, "Entry")
        nb, bb = await _indexed_note(s, org, user, "Sub")
        # 5 one-way traversals: passes both gates (traffic >= 5,
        # dominant 5 >= 4 * max(0, 1)).
        for h in range(5):
            s.add(_trace(org, [ba[0], bb[0]], NOW - datetime.timedelta(hours=h)))
        await s.flush()
        await edge_usage.refresh_edge_usage(s, org_id=org, now=NOW)
        out = await candidates.list_distillation_candidates(s, org_id=org, kind="link_direct")
        (edge,) = out["edges"]
        assert edge["op"] == "direct"
        assert edge["link_kind"] == "hypha_of"
        # The consistently higher-ranked note is the proposed parent.
        assert edge["src_note_id"] == str(na)
        assert edge["dst_note_id"] == str(nb)


async def test_link_direct_stays_silent_below_gates_or_when_linked() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        _na, ba = await _indexed_note(s, org, user, "A")
        _nb, bb = await _indexed_note(s, org, user, "B")
        nc, bc = await _indexed_note(s, org, user, "C")
        nd, bd = await _indexed_note(s, org, user, "D")
        # A-B: enough traffic but mixed direction (5 fwd / 2 bwd:
        # dominant 5 < 4 * 2).
        for h in range(5):
            s.add(_trace(org, [ba[0], bb[0]], NOW - datetime.timedelta(hours=h)))
        for h in range(2):
            s.add(_trace(org, [bb[0], ba[0]], NOW - datetime.timedelta(hours=10 + h)))
        # C-D: one-way but already hypha_of-linked -> not a target.
        for h in range(5):
            s.add(_trace(org, [bc[0], bd[0]], NOW - datetime.timedelta(hours=h)))
        await nl.link_notes(
            s, org_id=org, actor_id=user, parent_note_id=nc, child_note_id=nd, kind="hypha_of"
        )
        await s.flush()
        await edge_usage.refresh_edge_usage(s, org_id=org, now=NOW)
        out = await candidates.list_distillation_candidates(s, org_id=org, kind="link_direct")
        assert out["edges"] == []
