"""Graph analytics over the workspace note graph (tasks 4467acb4 +
8c0a8f08, Phase 1).

Two product surfaces in one place because they share the same data
shape (note_note_link rows plus the note↔tag bipartite graph) and the
same query traversal:

- ``compute_note_edge_weights`` exposes the materialised v1 of the
  ``note_edge_strength`` model. The weight per pair ``(a, b)`` is a
  soft-OR of three evidence sources:
    - a per-kind base contribution (hypha_of > supersedes > contradicts
      > related), aggregated soft-OR across every typed link between the
      pair;
    - an Adamic-Adar style overlap of shared generic tags (a rare
      tag contributes more than a common one, ``1 / log(1 + deg(t))``),
      then squashed to [0, 1] via ``1 - 1 / (1 + sum)`` so multiple
      rare tags saturate gently instead of exploding;
    - co-activity (ADR-0031's ``w_coact``, Proposal A): how often the
      pair was touched in the same working session, read from the
      ``note_coactivity`` table the offline worker materialises
      (``services/coactivity``) and squashed the same saturating way.
      Absent (the worker never ran / the pair never co-occurred) it
      contributes nothing, so the function is a byte-for-byte no-op
      versus the two-source version on a garden with no co-activity.

- ``compute_pagerank`` runs a deterministic power iteration on the
  UNDIRECTED, weighted weave (the edges of ``compute_note_edge_weights``).
  Importance is emergent connectivity, not authorship: the stored link
  direction (genesis) is ignored on purpose, since a child idea can
  outrank the idea that generated it. Genesis stays on the directional
  kinds (``hypha_of`` derivation, ``supersedes`` / ``contradicts``) +
  timestamps, a separate axis. PPR + Leiden + betweenness are Phase 2.

Both helpers run on-demand in a single round-trip and avoid any
materialised view; the cost is bounded because the link / tag
graphs are small per workspace (typical garden < 1k notes, < 10k
edges). Materialising is a Phase-2 concern once we see latency or
volume.
"""

from __future__ import annotations

import datetime
import math
import random
import uuid
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.note import Note
from flow_core.models.note_coactivity import NoteCoactivity
from flow_core.models.note_link import NoteNoteLink, NoteTaskLink
from flow_core.models.note_tag import NoteTag
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task import Task
from flow_core.models.task_relation import TaskRelation
from flow_core.models.task_tag import TaskTag

# Per-kind base contribution. Mirrors the SPA's ``edgeWeightV1`` so the
# server-side authoritative weight reads identically when the client
# moves from the local computation to the API.
_KIND_WEIGHT: dict[str, float] = {
    "hypha_of": 0.85,
    "supersedes": 0.70,
    "contradicts": 0.65,
    "related": 0.45,
}

# Per-kind base contribution for note<->task links (ADR-0042 D1, the
# unified task graph). The four flow relations carry different structural
# strength: a derivation (``promoted_from`` / ``derived_from``) is a
# stronger tie than a loose ``subject`` / ``artifact`` association. Only
# read when ``include_tasks`` is on; tunable v1, same spirit as
# ``_KIND_WEIGHT``.
_NOTE_TASK_KIND_WEIGHT: dict[str, float] = {
    "promoted_from": 0.70,
    "derived_from": 0.65,
    "artifact": 0.55,
    "subject": 0.50,
}

# Task<->task ``related`` edge weight (``TaskRelation``). Reuses the note
# ``related`` base so a task relation reads at the same strength as a note
# relation in the unified weave.
_TASK_RELATION_WEIGHT: float = _KIND_WEIGHT["related"]

# Co-activity source scale (ADR-0031 ``w_coact``). The session count from
# ``note_coactivity`` is squashed via ``1 - 1 / (1 + scale * count)`` --
# the same saturating shape as the shared-tag overlap. At 0.4: one shared
# session ~0.29, two ~0.44, five ~0.67; the pair never reaches 1 on
# co-activity alone, so a manual link or shared tag still dominates.
# Tunable like ``_KIND_WEIGHT``; a per-workspace weighting profile is a
# future knob (ADR-0031 roadmap), not v1.
_COACTIVITY_SCALE = 0.4


def _coactivity_weight(session_count: int) -> float:
    """Saturating [0, 1] contribution of a pair's co-activity session
    count. Zero (no co-activity) is neutral in the soft-OR."""
    if session_count <= 0:
        return 0.0
    return 1.0 - 1.0 / (1.0 + _COACTIVITY_SCALE * session_count)


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


async def _node_ids(
    session: AsyncSession, *, org_id: uuid.UUID, include_tasks: bool
) -> list[uuid.UUID]:
    """The graph's node set. Notes only by default; notes + live tasks when
    ``include_tasks`` (ADR-0042 D1). With it off this runs the exact same
    ``select(Note.id)`` the centrality helpers used inline, so the order and
    the resulting analytics are byte-identical. Note and task ids never
    collide (distinct UUIDs), so a bare UUID stays a sufficient node key."""
    note_rows = (await session.execute(select(Note.id).where(Note.org_id == org_id))).all()
    nodes: list[uuid.UUID] = [r[0] for r in note_rows]
    if include_tasks:
        task_rows = (
            await session.execute(
                select(Task.id).where(Task.org_id == org_id, Task.deleted_at.is_(None))
            )
        ).all()
        nodes.extend(r[0] for r in task_rows)
    return nodes


async def _generic_tag_degrees(
    session: AsyncSession, *, org_id: uuid.UUID, include_tasks: bool = False
) -> dict[uuid.UUID, int]:
    """Count how many in-org NODES carry each generic tag. The Adamic-
    Adar denominator depends on the degree of each tag in the bipartite
    node↔tag graph; we restrict to ``kind='generic'`` because client
    and project tags are coarse buckets (every node in the workspace
    has one), so they contribute zero discriminative power and would
    flatten the score.

    ``include_tasks`` (ADR-0042 D1) extends the corpus over ``task_tags``
    too, so the rarity denominator reflects the unified foresta. Default
    false keeps the notes-only degree counts byte-identical."""
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
    if include_tasks:
        # Join tasks + exclude soft-deleted: a deleted task is not a graph
        # node (``_node_ids`` excludes it), so its tags must not inflate the
        # rarity denominator either.
        task_rows = (
            await session.execute(
                select(TaskTag.tag_id, Tag.kind)
                .join(Tag, Tag.id == TaskTag.tag_id)
                .join(Task, Task.id == TaskTag.task_id)
                .where(TaskTag.org_id == org_id, Task.deleted_at.is_(None))
            )
        ).all()
        for tag_id, kind in task_rows:
            if kind is TagKind.generic:
                deg[tag_id] += 1
    return deg


async def _note_generic_tags(
    session: AsyncSession, *, org_id: uuid.UUID, include_tasks: bool = False
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """``{node_id: {generic_tag_id, ...}}`` for every visible node in the
    org. Single batched query (one extra when ``include_tasks`` folds
    ``task_tags`` in, ADR-0042 D1). Keys are note ids by default, notes +
    tasks when unified; the two id spaces never collide."""
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
    if include_tasks:
        # Exclude soft-deleted tasks (not graph nodes, see ``_node_ids``).
        task_rows = (
            await session.execute(
                select(TaskTag.task_id, TaskTag.tag_id, Tag.kind)
                .join(Tag, Tag.id == TaskTag.tag_id)
                .join(Task, Task.id == TaskTag.task_id)
                .where(TaskTag.org_id == org_id, Task.deleted_at.is_(None))
            )
        ).all()
        for task_id, tag_id, kind in task_rows:
            if kind is TagKind.generic:
                out[task_id].add(tag_id)
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
    session: AsyncSession, *, org_id: uuid.UUID, include_tasks: bool = False
) -> list[EdgeWeight]:
    """Materialise the v1 ``note_edge_strength`` over the org's manual
    note↔note link graph. Returns one row per *undirected* pair (the
    typed kind is collapsed; ``A hypha_of B`` and ``B related A``
    fold into the same weighted edge). Cost: two batched SELECTs
    (links + tags), O(L + N·avgTags) Python aggregation.

    When ``include_tasks`` (ADR-0042 D1) the weave spans notes + tasks: the
    tag corpus folds in ``task_tags`` (so note↔task and task↔task co-tag
    edges surface), ``TaskRelation`` adds undirected task↔task ``related``
    edges, and ``NoteTaskLink`` adds note↔task edges with a per-kind
    weight. Task co-activity (the working-session source) is a separate
    additive follow-up; absent, the soft-OR leaves it neutral. With
    ``include_tasks`` off the result is byte-identical to the notes-only
    weave (the caller — a unified surface — passes the fleet flag in).
    """
    inc = include_tasks
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
    tag_deg = await _generic_tag_degrees(session, org_id=org_id, include_tasks=inc)
    note_tags = await _note_generic_tags(session, org_id=org_id, include_tasks=inc)
    note_ids: list[uuid.UUID] = sorted(note_tags.keys(), key=str)
    by_tag_to_notes: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for nid in note_ids:
        for t in note_tags[nid]:
            by_tag_to_notes[t].append(nid)
    # Enumerate co-tagged note pairs only (much smaller than O(N²)).
    for _tag_id, nids in by_tag_to_notes.items():
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

    # Co-activity (ADR-0031 ``w_coact``, task f0a15247): the third source.
    # One cheap SELECT of the worker-materialised pairwise session counts;
    # each becomes a saturating contribution in the same soft-OR. Pairs
    # with co-activity but no link/tag surface here too (independent
    # evidence), exactly like the tag-overlap pairs above. Empty table ->
    # nothing appended -> identical to the link+tag-only result.
    coact_rows = (
        await session.execute(
            select(
                NoteCoactivity.note_a_id,
                NoteCoactivity.note_b_id,
                NoteCoactivity.session_count,
            ).where(NoteCoactivity.org_id == org_id)
        )
    ).all()
    for note_a_id, note_b_id, session_count in coact_rows:
        w_coact = _coactivity_weight(session_count)
        if w_coact <= 0:
            continue
        # Rows are stored canonical, but fold through _pair_key anyway so a
        # mismatch can never split a pair into two undirected edges.
        by_pair[_pair_key(note_a_id, note_b_id)].append(w_coact)

    # Task edges (ADR-0042 D1), only when the weave is unified. Both tables
    # carry their own uniqueness (TaskRelation unique per pair, NoteTaskLink
    # unique per (note, task, kind)), so a plain append is enough -- no
    # cross-direction dedup like the note-link loop needs. Empty / not
    # included -> nothing appended -> the notes-only result is untouched.
    if inc:
        # Soft-deleted tasks are not graph nodes (``_node_ids`` excludes
        # them), so their lingering relation / note-link rows must not emit
        # edges -- otherwise ``compute_betweenness`` (which derives its node
        # set from the edges) would resurrect a deleted task as a phantom.
        live_task_ids = {
            r[0]
            for r in (
                await session.execute(
                    select(Task.id).where(Task.org_id == org_id, Task.deleted_at.is_(None))
                )
            ).all()
        }
        rel_rows = (
            await session.execute(
                select(TaskRelation.task_a_id, TaskRelation.task_b_id).where(
                    TaskRelation.org_id == org_id
                )
            )
        ).all()
        for task_a_id, task_b_id in rel_rows:
            if task_a_id in live_task_ids and task_b_id in live_task_ids:
                by_pair[_pair_key(task_a_id, task_b_id)].append(_TASK_RELATION_WEIGHT)
        ntl_rows = (
            await session.execute(
                select(
                    NoteTaskLink.note_id,
                    NoteTaskLink.task_id,
                    NoteTaskLink.kind,
                ).where(NoteTaskLink.org_id == org_id)
            )
        ).all()
        for note_id, task_id, kind in ntl_rows:
            if task_id not in live_task_ids:
                continue
            w_nt = _NOTE_TASK_KIND_WEIGHT.get(kind, 0.0)
            if w_nt > 0:
                by_pair[_pair_key(note_id, task_id)].append(w_nt)

    out: list[EdgeWeight] = []
    for (a, b), contribs in by_pair.items():
        w = _softor(contribs)
        if w <= 0:
            continue
        out.append(EdgeWeight(src=a, dst=b, weight=w))
    # Stable order: descending weight, tie-break by (src, dst) string.
    out.sort(key=lambda e: (-e.weight, str(e.src), str(e.dst)))
    return out


async def compute_tag_neighborhood_entropy(
    session: AsyncSession, *, org_id: uuid.UUID
) -> float | None:
    """Mean Shannon entropy (bits) of the generic-tag distribution in each
    note's neighbourhood -- the notes it is directly linked to -- over the
    notes whose neighbourhood carries at least one generic tag. ADR-0035's
    ``tag_entropy_local`` biodiversity sensor: higher = more varied (a
    forest, not a monoculture). None when no neighbourhood carries a
    generic tag yet.

    The neighbourhood is the manual note<->note link graph (undirected);
    tag-overlap edges are deliberately excluded -- we measure the variety
    a node is *linked to*, not its own tag similarity.
    """
    link_rows = (
        await session.execute(
            select(NoteNoteLink.parent_note_id, NoteNoteLink.child_note_id).where(
                NoteNoteLink.org_id == org_id
            )
        )
    ).all()
    if not link_rows:
        return None
    adj: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for parent_id, child_id in link_rows:
        adj[parent_id].add(child_id)
        adj[child_id].add(parent_id)
    note_tags = await _note_generic_tags(session, org_id=org_id)
    entropies: list[float] = []
    for neighbours in adj.values():
        counts: dict[uuid.UUID, int] = defaultdict(int)
        for nb in neighbours:
            for tag_id in note_tags.get(nb, set()):
                counts[tag_id] += 1
        total = sum(counts.values())
        if total == 0:
            continue
        entropies.append(-sum((c / total) * math.log2(c / total) for c in counts.values()))
    if not entropies:
        return None
    return round(sum(entropies) / len(entropies), 4)


async def compute_pagerank(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    damping: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-6,
    include_tasks: bool = False,
) -> dict[uuid.UUID, float]:
    """Deterministic power-iteration PageRank over the org's note weave,
    treated as an UNDIRECTED, weighted graph (the edges of
    ``compute_note_edge_weights``: per-kind base soft-OR'd with shared-
    tag overlap).

    Importance is emergent connectivity, not authorship: the stored link
    direction (``parent_note_id`` / ``child_note_id``, i.e. genesis) is
    deliberately ignored, since a child idea can outrank the idea that
    generated it. Each undirected weighted edge ``w(a, b)`` contributes
    symmetrically and a node spreads its rank to neighbours in
    proportion to edge weight. Provenance / genesis lives on the
    directional kinds (``supersedes``) and on timestamps; it is a
    separate axis and is not allowed to bias centrality here.

    Notes with no weighted edge ("dangling") redistribute their mass
    uniformly to keep the iteration on a stochastic matrix. Returns the
    probability mass per note, summing to 1; an empty workspace returns
    ``{}``.
    """
    inc = include_tasks
    nodes = await _node_ids(session, org_id=org_id, include_tasks=inc)
    n = len(nodes)
    if n == 0:
        return {}
    idx = {nid: i for i, nid in enumerate(nodes)}
    # Undirected weighted adjacency from the materialised edge weights.
    # Each edge is added in both directions; out-strength is its weight
    # sum, so rank spreads proportionally to how strongly two ideas are
    # woven together, regardless of who linked to whom.
    edges = await compute_note_edge_weights(session, org_id=org_id, include_tasks=inc)
    neighbours: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    out_strength = [0.0] * n
    for e in edges:
        i = idx.get(e.src)
        j = idx.get(e.dst)
        if i is None or j is None or i == j or e.weight <= 0:
            continue
        neighbours[i].append((j, e.weight))
        neighbours[j].append((i, e.weight))
        out_strength[i] += e.weight
        out_strength[j] += e.weight
    rank = [1.0 / n] * n
    teleport = (1.0 - damping) / n
    for _ in range(max_iter):
        nxt = [teleport] * n
        dangling_mass = 0.0
        for u in range(n):
            if out_strength[u] <= 0:
                dangling_mass += rank[u]
                continue
            base = damping * rank[u] / out_strength[u]
            for v, w in neighbours[u]:
                nxt[v] += base * w
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
    include_tasks: bool = False,
) -> dict[uuid.UUID, float]:
    """Personalised PageRank seeded at ``seed_ids`` (task 5bf31b63).

    Same iteration shape as ``compute_pagerank`` but the teleport
    distribution is concentrated on the seed set (uniform across the
    seeds, zero elsewhere). The link graph is traversed UNDIRECTED (the
    pollinator wanders the weave both ways; exploration, like
    importance, does not follow genesis direction). Returns the
    probability mass per note, summing to 1 across the workspace. Used
    by ``graph_walk`` in focused mode to rank the subgraph around the
    seed.
    """
    inc = include_tasks
    nodes = await _node_ids(session, org_id=org_id, include_tasks=inc)
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
        # Undirected: walk the filament both ways.
        out_neighbours[pi].append(ci)
        out_neighbours[ci].append(pi)
    if inc:
        # The task-side structural filaments the pollinator can wander
        # (ADR-0042 D1): task↔task relations and note↔task typed links.
        # Unweighted like the note links above (PPR adjacency is a plain
        # neighbour list; edge strength is PageRank's axis, not the walk's).
        rel_rows = (
            await session.execute(
                select(TaskRelation.task_a_id, TaskRelation.task_b_id).where(
                    TaskRelation.org_id == org_id
                )
            )
        ).all()
        ntl_rows = (
            await session.execute(
                select(NoteTaskLink.note_id, NoteTaskLink.task_id).where(
                    NoteTaskLink.org_id == org_id
                )
            )
        ).all()
        for a_id, b_id in [*rel_rows, *ntl_rows]:
            ai = idx.get(a_id)
            bi = idx.get(b_id)
            if ai is None or bi is None or ai == bi:
                continue
            out_neighbours[ai].append(bi)
            out_neighbours[bi].append(ai)
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


async def compute_betweenness(
    session: AsyncSession, *, org_id: uuid.UUID, include_tasks: bool = False
) -> dict[uuid.UUID, float]:
    """Betweenness centrality over the org's note weave (task d8664631,
    Phase 2 of 8c0a8f08): the cluster-bridge detector. A note that sits
    on many shortest paths between other notes is a bridge between
    glades, even when its PageRank is unremarkable.

    Brandes' algorithm over the UNDIRECTED edge set of
    ``compute_note_edge_weights``, traversed unweighted: bridges are a
    structural property of the weave's topology, and the weights
    already shape PageRank (their own axis). Pure Python, deterministic
    (sorted traversal order), O(V·E): too slow for the request path on
    a grown garden, which is exactly why the worker computes it offline
    into ``garden_graph_snapshot`` and the API serves the stored copy.

    Values are normalised to [0, 1] by ``(n-1)(n-2)`` (the undirected
    pair count times two, folding Brandes' double count), with ``n``
    the number of connected notes. Isolated notes are omitted (their
    betweenness is zero by definition).

    With ``include_tasks`` (ADR-0042 D1) the bridge set spans the unified
    weave: a task that sits on shortest paths between glades is a bridge
    too. Nodes are derived from the (now unified) edge set, so an isolated
    task is omitted exactly like an isolated note.
    """
    inc = include_tasks
    edges = await compute_note_edge_weights(session, org_id=org_id, include_tasks=inc)
    adj: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for e in edges:
        if e.src == e.dst or e.weight <= 0:
            continue
        adj[e.src].append(e.dst)
        adj[e.dst].append(e.src)
    nodes = sorted(adj.keys(), key=str)
    n = len(nodes)
    if n < 3:
        return dict.fromkeys(nodes, 0.0)
    bc: dict[uuid.UUID, float] = dict.fromkeys(nodes, 0.0)
    for s in nodes:
        # BFS phase: shortest-path counts (sigma) + predecessor DAG.
        stack: list[uuid.UUID] = []
        pred: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        sigma: dict[uuid.UUID, float] = defaultdict(float)
        sigma[s] = 1.0
        dist: dict[uuid.UUID, int] = {s: 0}
        queue: deque[uuid.UUID] = deque([s])
        while queue:
            v = queue.popleft()
            stack.append(v)
            for w in adj[v]:
                if w not in dist:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        # Accumulation phase: dependency back-propagation.
        delta: dict[uuid.UUID, float] = defaultdict(float)
        while stack:
            w = stack.pop()
            for v in pred[w]:
                delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                bc[w] += delta[w]
    norm = float((n - 1) * (n - 2))
    return {v: round(bc[v] / norm, 6) for v in nodes}


# Recency half-feel: a note keeps ~37% of its boost after this many
# days. Two weeks matches the garden's seasonal cadence (the maturity
# sweep's seed->growing window).
RECENCY_TAU_DAYS = 14.0


async def compute_recency(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> dict[uuid.UUID, float]:
    """Exponential freshness per note: ``exp(-age_days / tau)``, 1.0 at
    creation decaying toward 0. Deliberately a SEPARATE axis from
    centrality (task d8664631): folding it into PageRank would let
    novelty masquerade as importance and skew every centrality
    consumer (sensors included). Consumers that want to counter the
    cold start (a brand-new note has no links, hence no centrality)
    combine the two explicitly. Pure function of ``created_at``: cheap
    to compute live, nothing to materialise."""
    now = now or datetime.datetime.now(datetime.UTC)
    rows = (
        await session.execute(
            select(Note.id, Note.created_at).where(Note.org_id == org_id, Note.deleted_at.is_(None))
        )
    ).all()
    out: dict[uuid.UUID, float] = {}
    for nid, created in rows:
        age_days = max(0.0, (now - created).total_seconds() / 86400.0)
        out[nid] = round(math.exp(-age_days / RECENCY_TAU_DAYS), 4)
    return out


async def compute_leiden_clusters(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    seed: int = 0,
    include_tasks: bool = False,
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

    inc = include_tasks
    nodes = await _node_ids(session, org_id=org_id, include_tasks=inc)
    if not nodes:
        return ClusterResult(clusters={}, modularity=None)
    idx = {nid: i for i, nid in enumerate(nodes)}

    edges = await compute_note_edge_weights(session, org_id=org_id, include_tasks=inc)
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


# Free-wander humus bias (ADR-0034): a humus neighbour's pick weight is
# multiplied by ``1 + _HUMUS_WANDER_BOOST * (pagerank / max_pagerank)`` so
# the wander drifts toward high-centrality long-fermented atoms. Fixed,
# not learned. Live neighbours keep factor 1.0.
_HUMUS_WANDER_BOOST = 2.0
# Hard cap: humus stays <= this fraction of the walk (ADR-0034).
_HUMUS_WANDER_CAP = 0.5


async def humus_note_ids(session: AsyncSession, *, org_id: uuid.UUID) -> set[uuid.UUID]:
    """The org's humus notes (``humus_flag`` set by the decomposition
    pipeline, ADR-0039). Read-side predicate for the walk bias and the
    walk-step provenance marker; uses the partial index ix_notes_humus_flag."""
    rows = await session.execute(
        select(Note.id).where(
            Note.org_id == org_id,
            Note.humus_flag.is_(True),
            # ADR-0043 D2/D1: a humus note still awaiting human review
            # (``review_state='proposed'``) is withheld from the free-wander
            # bias until approved; NULL/'approved' pass via IS DISTINCT FROM.
            Note.review_state.is_distinct_from("proposed"),
        )
    )
    return {r[0] for r in rows}


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
    # Seedable, non-cryptographic PRNG: the walk must be reproducible for
    # tests/telemetry given a seed, so stdlib ``random`` (not ``secrets``)
    # is the correct choice here.
    rng = random.Random(seed_rng) if seed_rng is not None else random.Random()  # noqa: S311
    # Pull the weighted edge list once.
    edges = await compute_note_edge_weights(session, org_id=org_id)
    # Undirected adjacency: {node_id: [(neighbour, weight)]}
    adj: dict[uuid.UUID, list[tuple[uuid.UUID, float]]] = defaultdict(list)
    for e in edges:
        adj[e.src].append((e.dst, e.weight))
        adj[e.dst].append((e.src, e.weight))
    if seed_id not in adj or not adj[seed_id]:
        return [seed_id]
    # Humus bias inputs (ADR-0034): the flagged-note set + PageRank for the
    # centrality weighting. Both are cheap on a <1k-note weave; skip the
    # PageRank pass entirely when the workspace has no humus.
    humus_ids = await humus_note_ids(session, org_id=org_id)
    pagerank = await compute_pagerank(session, org_id=org_id) if humus_ids else {}
    max_pr = max(pagerank.values(), default=0.0) or 1.0
    walk: list[uuid.UUID] = [seed_id]
    humus_count = 1 if seed_id in humus_ids else 0
    prev: uuid.UUID | None = None
    cur: uuid.UUID = seed_id
    for _ in range(max(0, budget - 1)):
        candidates = adj.get(cur)
        if not candidates:
            break
        if prev is None:
            # First step: plain weighted pick.
            ids = [n for n, _ in candidates]
            weights = [w for _, w in candidates]
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
        # Humus bias + hard cap (ADR-0034): boost high-centrality humus
        # neighbours (PageRank * humus_flag), but suppress humus once it
        # reaches the cap so it stays a minority of the walk. If the cap
        # empties the choice (every neighbour is capped humus) fall back to
        # the un-biased weights so the walk never dead-ends.
        at_cap = humus_count >= _HUMUS_WANDER_CAP * len(walk)
        biased = []
        for cand, w in zip(ids, weights, strict=True):
            if cand in humus_ids and not at_cap:
                biased.append(w * (1.0 + _HUMUS_WANDER_BOOST * (pagerank.get(cand, 0.0) / max_pr)))
            elif cand in humus_ids:
                biased.append(0.0)
            else:
                biased.append(w)
        if sum(biased) <= 0.0:
            biased = weights
        nxt = _weighted_pick(rng, ids, biased)
        if nxt is None:
            break
        walk.append(nxt)
        if nxt in humus_ids:
            humus_count += 1
        prev = cur
        cur = nxt
    return walk


def _weighted_pick(
    rng: random.Random, ids: list[uuid.UUID], weights: list[float]
) -> uuid.UUID | None:
    total = sum(w for w in weights if w > 0)
    if total <= 0 or not ids:
        return None
    r = rng.random() * total
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


def coactivity_weight(session_count: int) -> float:
    """Public re-export of the co-activity squash so tests / future MCP
    tools can pin the policy without importing the private helper."""
    return _coactivity_weight(session_count)


def softor(values: Iterable[float]) -> float:
    """Public ``softor`` re-export. Same semantics as the SPA's
    ``softOr`` (task 7e99c724) so a unit test can pin the formula on
    both sides."""
    return _softor(values)


__all__ = [
    "RECENCY_TAU_DAYS",
    "EdgeWeight",
    "adamic_adar_pair",
    "biased_random_walk",
    "coactivity_weight",
    "compute_betweenness",
    "compute_note_edge_weights",
    "compute_pagerank",
    "compute_personalized_pagerank",
    "compute_recency",
    "humus_note_ids",
    "softor",
]
