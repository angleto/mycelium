"""Graph analytics over the workspace note graph (tasks 4467acb4 +
8c0a8f08, Phase 1).

Two product surfaces in one place because they share the same data
shape (note_note_link rows plus the note↔tag bipartite graph) and the
same query traversal:

- ``compute_note_edge_weights`` exposes the materialised v1 of the
  ``note_edge_strength`` model. The weight per pair ``(a, b)`` is a
  soft-OR of two evidence sources:
    - a per-kind base contribution (atom_of > supersedes > replies_to
      > references), aggregated soft-OR across every typed manual
      link between the pair;
    - an Adamic-Adar style overlap of shared generic tags (a rare
      tag contributes more than a common one, ``1 / log(1 + deg(t))``),
      then squashed to [0, 1] via ``1 - 1 / (1 + sum)`` so multiple
      rare tags saturate gently instead of exploding.
  The third source documented in ADR-0031 (co-activity via Proposal
  A) is deferred to Phase 2 (the activity log carries the shape but
  no aggregation worker exists yet).

- ``compute_pagerank`` runs a deterministic power iteration on the
  same manual-link graph. PageRank global (no personalisation, no
  topic teleportation) is Phase 1 of the larger centrality stack;
  PPR seeded + Leiden + betweenness are Phase 2 (see ADR-0031).

Both helpers run on-demand in a single round-trip and avoid any
materialised view; the cost is bounded because the link / tag
graphs are small per workspace (typical garden < 1k notes, < 10k
edges). Materialising is a Phase-2 concern once we see latency or
volume.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.note import Note
from flow_core.models.note_link import NoteNoteLink
from flow_core.models.note_tag import NoteTag
from flow_core.models.tag import Tag, TagKind

# Per-kind base contribution. Mirrors the SPA's ``edgeWeightV1`` so the
# server-side authoritative weight reads identically when the client
# moves from the local computation to the API.
_KIND_WEIGHT: dict[str, float] = {
    "atom_of": 0.85,
    "supersedes": 0.70,
    "replies_to": 0.60,
    "references": 0.40,
}


@dataclass(frozen=True)
class EdgeWeight:
    """One undirected pair of notes carrying the aggregated weight."""

    src: uuid.UUID
    dst: uuid.UUID
    weight: float


def _softor(values: Iterable[float]) -> float:
    """Saturating OR over [0, 1] weights. Two evidence sources never
    push past 1; a missing source is neutral (contributes ``1 - 0``).
    """
    acc = 1.0
    for v in values:
        if v <= 0:
            continue
        if v >= 1:
            return 1.0
        acc *= 1.0 - v
    return 1.0 - acc


def _pair_key(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Canonical undirected pair key (sort by str so two directed
    edges A→B and B→A fold into the same row)."""
    return (a, b) if str(a) <= str(b) else (b, a)


async def _generic_tag_degrees(
    session: AsyncSession, *, org_id: uuid.UUID
) -> dict[uuid.UUID, int]:
    """Count how many in-org notes carry each generic tag. The Adamic-
    Adar denominator depends on the degree of each tag in the bipartite
    note↔tag graph; we restrict to ``kind='generic'`` because client
    and project tags are coarse buckets (every note in the workspace
    has one), so they contribute zero discriminative power and would
    flatten the score."""
    rows = (
        await session.execute(
            select(NoteTag.tag_id, Tag.kind)
            .join(Tag, Tag.id == NoteTag.tag_id)
            .where(NoteTag.org_id == org_id)
        )
    ).all()
    deg: dict[uuid.UUID, int] = defaultdict(int)
    for tag_id, kind in rows:
        if kind is TagKind.generic:
            deg[tag_id] += 1
    return deg


async def _note_generic_tags(
    session: AsyncSession, *, org_id: uuid.UUID
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """``{note_id: {generic_tag_id, ...}}`` for every visible note in
    the org. Single batched query."""
    rows = (
        await session.execute(
            select(NoteTag.note_id, NoteTag.tag_id, Tag.kind)
            .join(Tag, Tag.id == NoteTag.tag_id)
            .where(NoteTag.org_id == org_id)
        )
    ).all()
    out: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for note_id, tag_id, kind in rows:
        if kind is TagKind.generic:
            out[note_id].add(tag_id)
    return out


def _adamic_adar_pair(
    a_tags: set[uuid.UUID],
    b_tags: set[uuid.UUID],
    tag_degrees: dict[uuid.UUID, int],
) -> float:
    """Sum of ``1 / log(1 + deg(t))`` over the tags ``a`` and ``b``
    share, squashed into [0, 1] via ``1 - 1 / (1 + sum)``. A pair
    with one shared rare tag (deg=2) lands around 0.5; a pair with
    five common shared tags converges toward 1 without ever exceeding
    it."""
    if not a_tags or not b_tags:
        return 0.0
    s = 0.0
    for t in a_tags & b_tags:
        d = tag_degrees.get(t, 0)
        # log(1 + deg) protects against deg=0 (which can't happen if
        # we found a shared tag, but stay defensive) and avoids the
        # log(1) singularity when only one note has the tag.
        s += 1.0 / math.log(2.0 + d)
    if s <= 0:
        return 0.0
    return 1.0 - 1.0 / (1.0 + s)


async def compute_note_edge_weights(
    session: AsyncSession, *, org_id: uuid.UUID
) -> list[EdgeWeight]:
    """Materialise the v1 ``note_edge_strength`` over the org's manual
    note↔note link graph. Returns one row per *undirected* pair (the
    typed kind is collapsed; ``A atom_of B`` and ``B references A``
    fold into the same weighted edge). Cost: two batched SELECTs
    (links + tags), O(L + N·avgTags) Python aggregation.
    """
    link_rows = (
        await session.execute(
            select(
                NoteNoteLink.parent_note_id,
                NoteNoteLink.child_note_id,
                NoteNoteLink.kind,
            ).where(NoteNoteLink.org_id == org_id)
        )
    ).all()
    # Group per-kind contributions per pair, soft-OR'd inside the pair.
    by_pair: dict[tuple[uuid.UUID, uuid.UUID], list[float]] = defaultdict(list)
    seen_kind: dict[tuple[uuid.UUID, uuid.UUID, str], bool] = {}
    for parent_id, child_id, kind in link_rows:
        pk = _pair_key(parent_id, child_id)
        # Dedupe (parent, child, kind) so duplicate-by-direction rows
        # (a future B→A added on top of A→B) don't double-count: the
        # weight cap is "this kind exists between the two notes", not
        # "how many directed copies".
        seen_key = (pk[0], pk[1], kind)
        if seen_key in seen_kind:
            continue
        seen_kind[seen_key] = True
        w = _KIND_WEIGHT.get(kind, 0.0)
        if w > 0:
            by_pair[pk].append(w)

    # Adamic-Adar over shared generic tags. Pairs that have ZERO manual
    # link still surface here because tag overlap is independent
    # evidence the v2 design wants to expose. The SPA can filter by
    # ``weight >= threshold`` to keep the visual layer clean.
    tag_deg = await _generic_tag_degrees(session, org_id=org_id)
    note_tags = await _note_generic_tags(session, org_id=org_id)
    note_ids: list[uuid.UUID] = sorted(note_tags.keys(), key=str)
    by_tag_to_notes: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for nid in note_ids:
        for t in note_tags[nid]:
            by_tag_to_notes[t].append(nid)
    # Enumerate co-tagged note pairs only (much smaller than O(N²)).
    for tag_id, nids in by_tag_to_notes.items():
        if len(nids) < 2:
            continue
        for i in range(len(nids)):
            for j in range(i + 1, len(nids)):
                pk = _pair_key(nids[i], nids[j])
                # Compute AA once per pair, not per shared tag. We need
                # the full intersection -- accumulate the contribution
                # lazily by remembering we've seen this pair.
                if any(k[:2] == pk and k[2] == "__tag" for k in seen_kind):
                    continue
                seen_kind[(pk[0], pk[1], "__tag")] = True
                w_tag = _adamic_adar_pair(
                    note_tags[pk[0]], note_tags[pk[1]], tag_deg
                )
                if w_tag > 0:
                    by_pair[pk].append(w_tag)

    out: list[EdgeWeight] = []
    for (a, b), contribs in by_pair.items():
        w = _softor(contribs)
        if w <= 0:
            continue
        out.append(EdgeWeight(src=a, dst=b, weight=w))
    # Stable order: descending weight, tie-break by (src, dst) string.
    out.sort(key=lambda e: (-e.weight, str(e.src), str(e.dst)))
    return out


async def compute_pagerank(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    damping: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-6,
) -> dict[uuid.UUID, float]:
    """Deterministic power-iteration PageRank over the org's manual
    note↔note link graph (directed; ``parent_note_id`` is the source
    of authority that flows toward ``child_note_id``).

    Notes with zero out-degree ("dangling") redistribute their mass
    uniformly across all nodes to keep the power iteration converging
    on a stochastic matrix (classic PageRank fix). Returns the
    probability mass per note, summing to 1 across the workspace; an
    empty workspace returns ``{}``.
    """
    note_rows = (
        await session.execute(select(Note.id).where(Note.org_id == org_id))
    ).all()
    nodes: list[uuid.UUID] = [r[0] for r in note_rows]
    n = len(nodes)
    if n == 0:
        return {}
    idx = {nid: i for i, nid in enumerate(nodes)}
    out_neighbours: list[list[int]] = [[] for _ in range(n)]
    link_rows = (
        await session.execute(
            select(NoteNoteLink.parent_note_id, NoteNoteLink.child_note_id).where(
                NoteNoteLink.org_id == org_id
            )
        )
    ).all()
    for parent_id, child_id in link_rows:
        if parent_id == child_id:
            continue
        pi = idx.get(parent_id)
        ci = idx.get(child_id)
        if pi is None or ci is None:
            continue
        out_neighbours[pi].append(ci)
    rank = [1.0 / n] * n
    teleport = (1.0 - damping) / n
    for _ in range(max_iter):
        nxt = [teleport] * n
        dangling_mass = 0.0
        for u in range(n):
            outs = out_neighbours[u]
            if not outs:
                dangling_mass += rank[u]
                continue
            share = damping * rank[u] / len(outs)
            for v in outs:
                nxt[v] += share
        if dangling_mass > 0:
            extra = damping * dangling_mass / n
            for u in range(n):
                nxt[u] += extra
        # L1 distance for convergence; the per-step distribution stays
        # normalised so this is also the policy distance.
        delta = 0.0
        for u in range(n):
            delta += abs(nxt[u] - rank[u])
        rank = nxt
        if delta < tol:
            break
    return {nodes[i]: rank[i] for i in range(n)}


def _kind_base_weight(kind: str) -> float:
    """Public re-export so callers (tests, future MCP tools) can poke
    the policy without importing the private dict."""
    return _KIND_WEIGHT.get(kind, 0.0)


def adamic_adar_pair(
    a_tags: set[uuid.UUID],
    b_tags: set[uuid.UUID],
    tag_degrees: dict[uuid.UUID, int],
) -> float:
    """Public face of the internal helper (mirrors the cast(...) pattern
    used by other services for test-only exposure)."""
    return _adamic_adar_pair(a_tags, b_tags, tag_degrees)


def softor(values: Iterable[float]) -> float:
    """Public ``softor`` re-export. Same semantics as the SPA's
    ``softOr`` (task 7e99c724) so a unit test can pin the formula on
    both sides."""
    return _softor(values)


__all__ = [
    "EdgeWeight",
    "adamic_adar_pair",
    "compute_note_edge_weights",
    "compute_pagerank",
    "softor",
]
