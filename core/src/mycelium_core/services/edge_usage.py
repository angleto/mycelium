"""Search-informed edge-usage aggregation: the fourth
``note_edge_strength`` source (Fase 2 of the search-informed graph,
task 561c6aca).

Fase 0 laid the substrate: ``RetrievalTraceStage`` appends one
content-free ``retrieval_trace`` row per non-probe search (the served
top-m as ``{"blob_id", "rank"}`` items in rank order). This module is
the offline half: it folds the window's traces into the pair-keyed
``note_edge_usage`` counters that (a) ``graph.compute_note_edge_weights``
reads as its fourth soft-OR input and (b) ``candidates`` mines for
``link_direct`` proposals. Mirrors ``coactivity.refresh_coactivity``
(the third source) in shape: offline in the garden sweep, full per-org
replace, a pure projection of the rolling window.

Pairing rule (the O(m) contract from the design): after resolving
blob→note and deduping to NOTE granularity (first == best rank wins;
risk #1 — the graph is per-note, traces are per-blob, so a multi-part
note served as several chunks must not pair with itself), the deduped
rank-ordered note sequence ``n1..nm`` emits

- **ranking-adjacent** pairs ``(n_i, n_i+1)`` for i in 1..m-1, and
- **top-anchored** pairs ``(n1, n_j)`` for j in 3..m (j=2 is already
  adjacent),

i.e. ``2m-3`` pairs per trace — linear in the served list, never the
k²/2 clique (a clique would forge co-traversal between tail results
that were merely served together). Adjacency captures "ranked next to
each other for this query"; the top anchor captures "co-served with the
winner", the hit the searcher most plausibly used.

Direction: within a trace the earlier rank is the entry point, so a
pair contributes one directional tally *earlier→later*. Stored relative
to the canonical ``a<=b`` orientation (``graph._pair_key``):
``forward_count`` += 1 when canonical-a ranked above canonical-b,
``backward_count`` += 1 otherwise. Direction stays OUT of the
undirected weight (PageRank/betweenness/Leiden assume undirected) and
only feeds the ``link_direct`` candidate heuristic.

Retention: unlike the activity log (a shared append-only audit stream
that ``refresh_coactivity`` reads but never touches), ``retrieval_trace``
exists solely for this aggregation, so the refresh also DELETES rows
older than the window — they can never contribute again and the table
stays bounded by the org's search rate. Rows inside the window are kept
and re-folded on every run (full replace ⇒ ageing-out pairs disappear,
idempotent for a fixed ``now``). ``decay_score`` is the recency-weighted
magnitude ``Σ 0.5^(age_days/half_life)`` over the window's traversals
(the ``memory.recompute_tier`` form), so one fresh co-retrieval scores
1.0 and a window-edge one ~0.13.

Probe traffic (the eval harness) writes no trace by construction
(Fase 0); the aggregation still filters ``is_probe`` so the flagged-not-
skipped future the column reserves cannot leak measurement into demand.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.note import Note
from mycelium_core.models.note_edge_usage import NoteEdgeUsage
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.models.retrieval_trace import RetrievalTrace
from mycelium_core.services.graph import _pair_key

# Rolling lookback, same season framing as COACTIVITY_WINDOW_DAYS: the
# live weave is shaped by the recent past, not ancient searches.
EDGE_USAGE_WINDOW_DAYS = 90
# Half-life of a traversal's contribution to ``decay_score`` (the
# ``recompute_tier`` default): a month-old co-retrieval counts half.
EDGE_USAGE_HALF_LIFE_DAYS = 30.0
# A trace resolving to more distinct notes than this is not a focused
# search result being worked with (some future stage re-ordering or a
# giant limit): dropped whole, mirroring coactivity's bulk-session
# guard. The stage caps top_m at 16, so this only guards drift.
_MAX_TRACE_NOTES = 25


@dataclass
class _PairAcc:
    """Mutable per-pair fold state for one refresh pass."""

    last: datetime.datetime
    count: int = 0
    forward: int = 0
    backward: int = 0
    decay: float = 0.0


async def _blob_to_note(
    session: AsyncSession, *, org_id: uuid.UUID, blob_ids: set[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Resolve served blob ids to their owning note via the index
    pointers. Blobs with no pointer (task/plain-memory blobs) drop out:
    the graph is per-note. Erased blobs simply don't resolve."""
    if not blob_ids:
        return {}
    rows = (
        await session.execute(
            select(NotePartIndexPointer.blob_id, NotePartIndexPointer.note_id).where(
                NotePartIndexPointer.org_id == org_id,
                NotePartIndexPointer.blob_id.in_(blob_ids),
            )
        )
    ).all()
    return {blob_id: note_id for blob_id, note_id in rows}


def _trace_pairs(
    note_seq: list[uuid.UUID],
) -> list[tuple[tuple[uuid.UUID, uuid.UUID], bool]]:
    """The trace's pair emissions: ``(canonical_pair, is_forward)`` per
    the module-docstring rule (adjacent + top-anchored). ``is_forward``
    is True when the earlier-ranked note is the canonical ``a``."""
    out: list[tuple[tuple[uuid.UUID, uuid.UUID], bool]] = []

    def emit(u: uuid.UUID, v: uuid.UUID) -> None:
        pk = _pair_key(u, v)
        out.append((pk, pk[0] == u))

    for i in range(len(note_seq) - 1):
        emit(note_seq[i], note_seq[i + 1])
    for j in range(2, len(note_seq)):
        emit(note_seq[0], note_seq[j])
    return out


async def refresh_edge_usage(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> int:
    """Fold the org's windowed retrieval traces into ``note_edge_usage``
    and replace the stored rows; delete traces older than the window
    (see the module docstring for the pairing/direction/retention
    contract). Returns the number of pair rows written. Idempotent for a
    fixed ``now``.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    since = now - datetime.timedelta(days=EDGE_USAGE_WINDOW_DAYS)

    # Aged-out traces can never contribute again: retention delete first
    # (the (org_id, created_at) index serves both this and the read).
    await session.execute(
        delete(RetrievalTrace).where(
            RetrievalTrace.org_id == org_id, RetrievalTrace.created_at < since
        )
    )

    traces = (
        await session.execute(
            select(RetrievalTrace.items, RetrievalTrace.created_at)
            .where(
                RetrievalTrace.org_id == org_id,
                RetrievalTrace.created_at >= since,
                RetrievalTrace.is_probe.is_(False),
            )
            .order_by(RetrievalTrace.created_at)
        )
    ).all()

    # Resolve every served blob to its note in one batched lookup.
    all_blob_ids: set[uuid.UUID] = set()
    parsed: list[tuple[list[uuid.UUID], datetime.datetime]] = []
    for items, created_at in traces:
        blob_ids = [uuid.UUID(it["blob_id"]) for it in items]
        all_blob_ids.update(blob_ids)
        parsed.append((blob_ids, created_at))
    note_of = await _blob_to_note(session, org_id=org_id, blob_ids=all_blob_ids)

    acc: dict[tuple[uuid.UUID, uuid.UUID], _PairAcc] = {}
    for blob_ids, created_at in parsed:
        # Note-granularity dedup preserving rank order: best rank wins.
        seq: list[uuid.UUID] = []
        seen: set[uuid.UUID] = set()
        for bid in blob_ids:
            nid = note_of.get(bid)
            if nid is not None and nid not in seen:
                seen.add(nid)
                seq.append(nid)
        if len(seq) < 2 or len(seq) > _MAX_TRACE_NOTES:
            continue
        age_days = max((now - created_at).total_seconds(), 0.0) / 86400.0
        decay = 0.5 ** (age_days / EDGE_USAGE_HALF_LIFE_DAYS)
        # The rule emits each pair at most once per trace (adjacency and
        # top-anchoring are disjoint: j starts at 3 and the sequence is
        # deduped). The per-trace set makes count/decay robust to a
        # future rule edit that overlaps them; direction tallies every
        # emission (identical under at-most-once).
        seen_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
        for pk, is_forward in _trace_pairs(seq):
            row = acc.setdefault(pk, _PairAcc(last=created_at))
            if pk not in seen_pairs:
                seen_pairs.add(pk)
                row.count += 1
                row.decay += decay
            if is_forward:
                row.forward += 1
            else:
                row.backward += 1
            if created_at > row.last:
                row.last = created_at

    # Drop pairs touching a soft-deleted / vanished note before write.
    if acc:
        note_ids = {n for pk in acc for n in pk}
        live = {
            r[0]
            for r in (
                await session.execute(
                    select(Note.id).where(
                        Note.org_id == org_id,
                        Note.id.in_(note_ids),
                        Note.deleted_at.is_(None),
                    )
                )
            ).all()
        }
        acc = {pk: v for pk, v in acc.items() if pk[0] in live and pk[1] in live}

    # Full per-org replace: the table is a projection of the window.
    await session.execute(delete(NoteEdgeUsage).where(NoteEdgeUsage.org_id == org_id))
    if acc:
        session.add_all(
            NoteEdgeUsage(
                org_id=org_id,
                note_a_id=pk[0],
                note_b_id=pk[1],
                traversal_count=row.count,
                forward_count=row.forward,
                backward_count=row.backward,
                last_traversed_at=row.last,
                decay_score=round(row.decay, 9),
                computed_at=now,
            )
            for pk, row in acc.items()
        )
        await session.flush()
    return len(acc)


__all__ = [
    "EDGE_USAGE_HALF_LIFE_DAYS",
    "EDGE_USAGE_WINDOW_DAYS",
    "refresh_edge_usage",
]
