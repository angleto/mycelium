"""Local adjacency + bounded best-first traversal (Fase 1 of the
search-informed graph, task 561c6aca).

``graph.compute_note_edge_weights`` materialises the WHOLE org weave in
one pass -- correct for analytics, but its cost grows with the org. This
module is the localised inverse: ``local_edges`` answers "the weighted
edges of note X" with a handful of per-node queries (DB work
O(neighbours of X), independent of org size), and ``bounded_neighborhood``
walks the weave best-first from a seed under explicit budgets, so the
cost of assembling a distillation/reading neighbourhood no longer scales
with the graph (plan §2: best-first thresholded, the pre-APPR phase).

Weight parity is a hard contract, pinned by test: for any pair the
soft-OR of the same four evidence sources (typed links, shared-generic-tag
Adamic-Adar, co-activity, search-informed edge usage) must equal the full builder's weight -- the
helpers (``_KIND_WEIGHT``, ``_softor``, ``_adamic_adar_pair``,
``_coactivity_weight``, ``_usage_weight``) are imported from ``graph``, never duplicated.
``note_edge_usage`` (materialised by Fase 2's ``refresh_edge_usage``) is
the 4th soft-OR input, folded exactly like co-activity.

Notes only: the unified include_tasks weave stays on the full builders
until a bounded surface needs it.
"""

from __future__ import annotations

import heapq
import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.membership import Role
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.models.note import Note
from mycelium_core.models.note_coactivity import NoteCoactivity
from mycelium_core.models.note_edge_usage import NoteEdgeUsage
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.services.graph import (
    _KIND_WEIGHT,
    _adamic_adar_pair,
    _coactivity_weight,
    _softor,
    _usage_weight,
)
from mycelium_core.services.note_effective import (
    effective_note_clause,
    ineffective_note_ids,
    note_is_effective,
)
from mycelium_core.services.rbac import require_role

# Best-first defaults. ``gamma`` mirrors the walk-continuation mass of the
# existing PPR damping (0.85); ``tau`` at 0.05 with typical edge weights
# (0.45-0.85) exhausts within ~4-6 hops of a strong path, so the budgets
# below are the belt on top of the threshold's own convergence.
DEFAULT_GAMMA = 0.85
DEFAULT_TAU = 0.05
DEFAULT_NODE_BUDGET = 24
DEFAULT_CHAR_BUDGET = 40_000


async def local_edges(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
) -> dict[uuid.UUID, float]:
    """Weighted edges touching ``note_id``: ``{neighbour_id: weight}`` with
    the exact soft-OR the full builder would give the pair (parity pinned in
    test_graph_local). Four bounded queries (links, co-activity, edge usage,
    co-tag) + a tag-degree lookup; nothing scans the org.

    Ineffective neighbours are dropped (a proposal awaiting review or a
    note in the bin is not a graph node) and so are their tags in the
    rarity denominator -- the same shared predicate the full builder
    applies, which is what makes the parity structural instead of two
    filters that have to be kept in step by hand.
    """
    contribs: dict[uuid.UUID, list[float]] = defaultdict(list)

    # 1) Typed links touching X, deduped per (neighbour, kind) so a future
    #    duplicate-by-direction row can't double-count (mirror of the full
    #    builder's seen_kind fold).
    link_rows = (
        await session.execute(
            select(
                NoteNoteLink.parent_note_id, NoteNoteLink.child_note_id, NoteNoteLink.kind
            ).where(
                NoteNoteLink.org_id == org_id,
                or_(
                    NoteNoteLink.parent_note_id == note_id,
                    NoteNoteLink.child_note_id == note_id,
                ),
            )
        )
    ).all()
    seen_kind: set[tuple[uuid.UUID, str]] = set()
    for parent_id, child_id, kind in link_rows:
        nb = child_id if parent_id == note_id else parent_id
        if nb == note_id or (nb, kind) in seen_kind:
            continue
        seen_kind.add((nb, kind))
        w = _KIND_WEIGHT.get(kind, 0.0)
        if w > 0:
            contribs[nb].append(w)

    # 2) Shared-generic-tag Adamic-Adar. The AA score only reads the pair's
    #    shared tags, and shared ⊆ X's tags -- so the tag-degree denominator
    #    and the co-tagged candidates are both scoped to X's own tags: two
    #    queries bounded by X's tag degree, never the org corpus.
    x_tags = {
        r[0]
        for r in (
            await session.execute(
                select(NoteTag.tag_id)
                .join(Tag, Tag.id == NoteTag.tag_id)
                .where(
                    NoteTag.org_id == org_id,
                    NoteTag.note_id == note_id,
                    Tag.kind == TagKind.generic,
                )
            )
        ).all()
    }
    if x_tags:
        # Degree of each shared tag over the org's EFFECTIVE notes -- the same
        # rarity denominator ``_generic_tag_degrees`` computes for the full
        # builder on the NOTES-ONLY weave, which is the parity contract of
        # this module (task f8402e7f; the unified ``include_tasks`` denominator
        # folds ``task_tags`` in and stays on the full builders). The join is
        # what keeps the two sides in step: a note the full builder does not
        # count must not be counted here either, or the same pair gets two
        # different weights.
        deg_rows = (
            await session.execute(
                select(NoteTag.tag_id, func.count())
                .join(Note, Note.id == NoteTag.note_id)
                .where(
                    NoteTag.org_id == org_id,
                    NoteTag.tag_id.in_(x_tags),
                    effective_note_clause(),
                )
                .group_by(NoteTag.tag_id)
            )
        ).all()
        tag_deg = {tag_id: int(n) for tag_id, n in deg_rows}
        co_rows = (
            await session.execute(
                select(NoteTag.note_id, NoteTag.tag_id).where(
                    NoteTag.org_id == org_id,
                    NoteTag.tag_id.in_(x_tags),
                    NoteTag.note_id != note_id,
                )
            )
        ).all()
        shared_by_nb: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        for nb_id, tag_id in co_rows:
            shared_by_nb[nb_id].add(tag_id)
        for nb_id, shared in shared_by_nb.items():
            # AA over the intersection only: passing the shared set for both
            # sides is identical to passing the full tag sets (the function
            # reads ``a_tags & b_tags``).
            w_tag = _adamic_adar_pair(shared, shared, tag_deg)
            if w_tag > 0:
                contribs[nb_id].append(w_tag)

    # 3) Co-activity (pair-keyed, canonical a<=b; query both slots).
    coact_rows = (
        await session.execute(
            select(
                NoteCoactivity.note_a_id, NoteCoactivity.note_b_id, NoteCoactivity.session_count
            ).where(
                NoteCoactivity.org_id == org_id,
                or_(
                    NoteCoactivity.note_a_id == note_id,
                    NoteCoactivity.note_b_id == note_id,
                ),
            )
        )
    ).all()
    for a_id, b_id, count in coact_rows:
        nb = b_id if a_id == note_id else a_id
        if nb == note_id:
            continue
        w_coact = _coactivity_weight(count)
        if w_coact > 0:
            contribs[nb].append(w_coact)

    # 4) Search-informed edge usage (Fase 2, pair-keyed canonical a<=b;
    #    query both slots, mirror of co-activity). Empty table == absent
    #    == neutral; parity with the full builder pinned in test.
    usage_rows = (
        await session.execute(
            select(
                NoteEdgeUsage.note_a_id, NoteEdgeUsage.note_b_id, NoteEdgeUsage.decay_score
            ).where(
                NoteEdgeUsage.org_id == org_id,
                or_(
                    NoteEdgeUsage.note_a_id == note_id,
                    NoteEdgeUsage.note_b_id == note_id,
                ),
            )
        )
    ).all()
    for a_id, b_id, decay_score in usage_rows:
        nb = b_id if a_id == note_id else a_id
        if nb == note_id:
            continue
        w_usage = _usage_weight(decay_score)
        if w_usage > 0:
            contribs[nb].append(w_usage)

    if not contribs:
        return {}
    # An ineffective endpoint -- un-approved proposal or trashed -- is not a
    # graph node, so it is not an edge endpoint either (task f8402e7f): one
    # bounded filter query over the candidate set plus X, never over the org.
    ineffective = await ineffective_note_ids(
        session, org_id=org_id, among=[note_id, *contribs.keys()]
    )
    if note_id in ineffective:
        # X itself is not a node: the full builder emits no pair touching it,
        # and parity with the full builder is this module's hard contract.
        return {}
    return {
        nb: w
        for nb, values in contribs.items()
        if nb not in ineffective and (w := _softor(values)) > 0
    }


@dataclass(frozen=True)
class BoundedNode:
    """One reached note: best path weight and hop distance from the seed."""

    note_id: uuid.UUID
    weight: float
    hop: int


@dataclass(frozen=True)
class BoundedEdge:
    """A tree edge actually traversed to first reach ``dst``."""

    src: uuid.UUID
    dst: uuid.UUID
    weight: float


@dataclass(frozen=True)
class BoundedNeighborhood:
    """Best-first neighbourhood of a seed under explicit budgets.

    ``nodes`` excludes the seed (the caller has it) and is in pop order
    (descending best-path weight, note-id tie-break). ``stopped_by`` says
    which guard ended the walk: ``node_budget`` / ``char_budget`` or
    ``exhausted`` (frontier empty -- including everything pruned by tau).
    """

    seed_id: uuid.UUID
    nodes: list[BoundedNode]
    edges: list[BoundedEdge]
    chars: int
    stopped_by: str


async def _note_chars(session: AsyncSession, note_id: uuid.UUID) -> int:
    """Indexed text size of a note (sum over its part blobs) -- the same
    text a distiller would assemble; notes with no indexed part count 0."""
    total = (
        await session.execute(
            select(func.coalesce(func.sum(func.length(func.coalesce(MemoryBlob.text, ""))), 0))
            .select_from(NotePartIndexPointer)
            .join(MemoryBlob, MemoryBlob.id == NotePartIndexPointer.blob_id)
            .where(NotePartIndexPointer.note_id == note_id)
        )
    ).scalar_one()
    return int(total)


async def bounded_neighborhood(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    seed_note_id: uuid.UUID,
    node_budget: int = DEFAULT_NODE_BUDGET,
    char_budget: int = DEFAULT_CHAR_BUDGET,
    tau: float = DEFAULT_TAU,
    gamma: float = DEFAULT_GAMMA,
) -> BoundedNeighborhood:
    """Best-first thresholded traversal from ``seed_note_id`` (plan §2,
    Fase 1). Path weight = product of edge weights x ``gamma`` per hop;
    the frontier is a max-heap, so expansion follows the strongest paths
    first. Stops at ``node_budget`` returned nodes, at ``char_budget`` of
    assembled indexed text (seed included; the node that would overflow is
    NOT returned), or when the frontier is exhausted -- and every push is
    gated on ``weight >= tau``, so termination does not depend on the
    budgets (the weight decays by at least ``gamma`` per hop).

    Deterministic: ties break on the note id string. DB work is
    O(returned nodes) calls to ``local_edges`` -- independent of org size.
    """
    await require_role(session, org_id, actor_id, Role.member)
    node_budget = max(1, node_budget)
    seed = await session.get(Note, seed_note_id)
    # An ineffective seed is not openable anywhere else either (``get_note``
    # raises the same NotFoundError for a trashed or un-approved note), so the
    # walk refuses to start rather than building a working set around a node
    # the rest of the system denies (task f8402e7f).
    if (
        seed is None
        or seed.org_id != org_id
        or not note_is_effective(review_state=seed.review_state, deleted_at=seed.deleted_at)
    ):
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)

    chars = await _note_chars(session, seed_note_id)
    if chars > char_budget:
        return BoundedNeighborhood(
            seed_id=seed_note_id, nodes=[], edges=[], chars=0, stopped_by="char_budget"
        )

    # Heap entries: (-weight, note_id_str, note_id, hop, parent, edge_w).
    heap: list[tuple[float, str, uuid.UUID, int, uuid.UUID | None, float]] = [
        (-1.0, str(seed_note_id), seed_note_id, 0, None, 0.0)
    ]
    visited: set[uuid.UUID] = set()
    nodes: list[BoundedNode] = []
    edges: list[BoundedEdge] = []
    stopped_by = "exhausted"
    while heap:
        neg_w, _, nid, hop, parent, edge_w = heapq.heappop(heap)
        if nid in visited:
            continue
        w = -neg_w
        if nid != seed_note_id:
            n_chars = await _note_chars(session, nid)
            if chars + n_chars > char_budget:
                stopped_by = "char_budget"
                break
            chars += n_chars
            nodes.append(BoundedNode(note_id=nid, weight=w, hop=hop))
            if parent is not None:
                edges.append(BoundedEdge(src=parent, dst=nid, weight=edge_w))
        visited.add(nid)
        if len(nodes) >= node_budget:
            stopped_by = "node_budget"
            break
        for nb, ew in sorted(
            (await local_edges(session, org_id=org_id, note_id=nid)).items(),
            key=lambda kv: str(kv[0]),
        ):
            if nb in visited:
                continue
            nw = w * ew * gamma
            if nw >= tau:
                heapq.heappush(heap, (-nw, str(nb), nb, hop + 1, nid, ew))
    return BoundedNeighborhood(
        seed_id=seed_note_id, nodes=nodes, edges=edges, chars=chars, stopped_by=stopped_by
    )


__all__ = [
    "BoundedEdge",
    "BoundedNeighborhood",
    "BoundedNode",
    "bounded_neighborhood",
    "local_edges",
]
