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


@dataclass(frozen=True)
class ClusterResult:
    """Leiden community assignment over the weighted note graph.

    ``clusters`` maps each note to its community index (0-based, dense).
    ``modularity`` is the global modularity of the partition (ADR-0035's
    ``leiden_modularity`` sensor), or ``None`` when clustering could not
    run (the optional ``clustering`` extra is not installed) — the
    caller degrades gracefully rather than failing the request.
    """

    clusters: dict[uuid.UUID, int]
    modularity: float | None


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


async def _generic_tag_degrees(session: AsyncSession, *, org_id: uuid.UUID) -> dict[uuid.UUID, int]:
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
                w_tag = _adamic_adar_pair(note_tags[pk[0]], note_tags[pk[1]], tag_deg)
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
    note_rows = (await session.execute(select(Note.id).where(Note.org_id == org_id))).all()
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


async def compute_personalized_pagerank(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    seed_ids: list[uuid.UUID],
    damping: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-6,
) -> dict[uuid.UUID, float]:
    """Personalised PageRank seeded at ``seed_ids`` (task 5bf31b63).

    Same iteration shape as ``compute_pagerank`` but the teleport
    distribution is concentrated on the seed set (uniform across the
    seeds, zero elsewhere). Returns the probability mass per note,
    summing to 1 across the workspace. Used by ``graph_walk`` in
    focused mode to rank the subgraph induced by the seed's typed
    neighbours.
    """
    note_rows = (await session.execute(select(Note.id).where(Note.org_id == org_id))).all()
    nodes: list[uuid.UUID] = [r[0] for r in note_rows]
    n = len(nodes)
    if n == 0:
        return {}
    idx = {nid: i for i, nid in enumerate(nodes)}
    valid_seeds = [idx[s] for s in seed_ids if s in idx]
    if not valid_seeds:
        return {nid: 0.0 for nid in nodes}
    seed_set = set(valid_seeds)
    teleport_dist = [0.0] * n
    for s in valid_seeds:
        teleport_dist[s] = 1.0 / len(valid_seeds)
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
    rank = list(teleport_dist)
    for _ in range(max_iter):
        nxt = [(1.0 - damping) * teleport_dist[u] for u in range(n)]
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
            # Dangling mass redistributes via the teleport distribution
            # (the personalised variant of the classic fix), so dangling
            # walks restart at the seeds rather than uniformly.
            for u in range(n):
                nxt[u] += damping * dangling_mass * teleport_dist[u]
        delta = 0.0
        for u in range(n):
            delta += abs(nxt[u] - rank[u])
        rank = nxt
        if delta < tol:
            break
    # Sanity: emit the seed mass clamp non-negative.
    del seed_set
    return {nodes[i]: rank[i] for i in range(n)}


async def compute_leiden_clusters(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    seed: int = 0,
) -> ClusterResult:
    """Partition the workspace note graph into communities with the
    Leiden algorithm (task 8c0a8f08, ADR-0031 v2). Runs over the same
    undirected weighted graph as ``compute_note_edge_weights`` (per-kind
    base + Adamic-Adar tag overlap), so visually-clustered notes and
    co-tagged notes fall together. Leiden over Louvain because it
    guarantees well-connected communities (no internally-disconnected
    clusters).

    Returns a dense 0-based community index per note plus the partition's
    global modularity (the "is this garden structured or magma?"
    thermometer of ADR-0035). Notes with no qualifying edge become
    singleton communities. Deterministic given ``seed``.

    ``python-igraph`` + ``leidenalg`` are an optional extra
    (``clustering``); when absent the function returns an empty mapping
    and ``modularity=None`` so the endpoint degrades to "no colours"
    instead of 500-ing. Bounded cost: the garden graph is small per
    workspace (< 1k notes); materialisation/caching is a later concern,
    same as the other graph helpers here.
    """
    try:
        import igraph
        import leidenalg
    except ImportError:
        return ClusterResult(clusters={}, modularity=None)

    note_rows = (await session.execute(select(Note.id).where(Note.org_id == org_id))).all()
    nodes: list[uuid.UUID] = [r[0] for r in note_rows]
    if not nodes:
        return ClusterResult(clusters={}, modularity=None)
    idx = {nid: i for i, nid in enumerate(nodes)}

    edges = await compute_note_edge_weights(session, org_id=org_id)
    ig_edges: list[tuple[int, int]] = []
    weights: list[float] = []
    for e in edges:
        si = idx.get(e.src)
        di = idx.get(e.dst)
        if si is None or di is None or si == di:
            continue
        ig_edges.append((si, di))
        weights.append(e.weight)

    g = igraph.Graph(n=len(nodes), edges=ig_edges, directed=False)
    weight_arg = None
    if weights:
        g.es["weight"] = weights
        weight_arg = "weight"
    # ModularityVertexPartition: the canonical objective, so the score we
    # report is exactly the modularity ADR-0035 tracks. A tunable
    # resolution (RBConfigurationVertexPartition) is the future knob for
    # the anti-monoculture work (ADR-0033) but not needed for v1.
    partition = leidenalg.find_partition(
        g,
        leidenalg.ModularityVertexPartition,
        weights=weight_arg,
        seed=seed,
    )
    membership: list[int] = list(partition.membership)
    clusters = {nodes[i]: int(membership[i]) for i in range(len(nodes))}
    modularity = float(g.modularity(membership, weights=weight_arg))
    return ClusterResult(clusters=clusters, modularity=modularity)


async def biased_random_walk(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    seed_id: uuid.UUID,
    budget: int = 32,
    p: float = 1.0,
    q: float = 1.0,
    seed_rng: int | None = None,
) -> list[uuid.UUID]:
    """Node2Vec-style second-order random walk (task 5bf31b63 / ADR-0034).

    The walk visits up to ``budget`` nodes starting at ``seed_id``.
    The second-order bias is parameterised by:
    - ``p`` (return parameter): high p discourages immediate backtrack.
    - ``q`` (in-out parameter): high q biases toward staying close
      (BFS-like, structural equivalence); low q lets the walk wander
      farther (DFS-like, community discovery).

    The graph is treated as *undirected* for the walk: every typed
    link contributes both directions. Edge weight comes from the
    materialised ``note_edge_strength`` (computed inline; cheap on
    workspaces with <10k edges). When a node has no neighbours the
    walk terminates early.
    """
    import random

    rng = random.Random(seed_rng) if seed_rng is not None else random.Random()
    # Pull the weighted edge list once.
    edges = await compute_note_edge_weights(session, org_id=org_id)
    # Undirected adjacency: {node_id: [(neighbour, weight)]}
    adj: dict[uuid.UUID, list[tuple[uuid.UUID, float]]] = defaultdict(list)
    for e in edges:
        adj[e.src].append((e.dst, e.weight))
        adj[e.dst].append((e.src, e.weight))
    if seed_id not in adj or not adj[seed_id]:
        return [seed_id]
    walk: list[uuid.UUID] = [seed_id]
    prev: uuid.UUID | None = None
    cur: uuid.UUID = seed_id
    for _ in range(max(0, budget - 1)):
        candidates = adj.get(cur)
        if not candidates:
            break
        if prev is None:
            # First step: plain weighted pick.
            weights = [w for _, w in candidates]
            nxt = _weighted_pick(rng, [n for n, _ in candidates], weights)
        else:
            # Second-order bias: distance(prev -> candidate) is 0 if
            # candidate is prev itself (return), 1 if candidate is a
            # neighbour of prev (BFS), 2 otherwise (DFS).
            prev_nbrs = {n for n, _ in adj.get(prev, [])}
            weights = []
            ids = []
            for cand, w in candidates:
                if cand == prev:
                    factor = 1.0 / max(p, 1e-9)
                elif cand in prev_nbrs:
                    factor = 1.0
                else:
                    factor = 1.0 / max(q, 1e-9)
                weights.append(w * factor)
                ids.append(cand)
            nxt = _weighted_pick(rng, ids, weights)
        if nxt is None:
            break
        walk.append(nxt)
        prev = cur
        cur = nxt
    return walk


def _weighted_pick(rng: object, ids: list[uuid.UUID], weights: list[float]) -> uuid.UUID | None:
    total = sum(w for w in weights if w > 0)
    if total <= 0 or not ids:
        return None
    # rng is typed as object to keep the import inside biased_random_walk
    # local; cast via getattr for the .random() call.
    r = rng.random() * total  # type: ignore[no-any-return]
    acc = 0.0
    for i, w in zip(ids, weights, strict=True):
        if w <= 0:
            continue
        acc += w
        if r <= acc:
            return i
    return ids[-1]


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
    "biased_random_walk",
    "compute_note_edge_weights",
    "compute_pagerank",
    "compute_personalized_pagerank",
    "softor",
]
