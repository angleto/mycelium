"""Garden analytics router (tasks 4467acb4 + 8c0a8f08 + 5bf31b63).

Read-only endpoints over the workspace note-link graph:

- ``GET /garden/graph``: weighted edges + global PageRank.
- ``GET /garden/walk``: two graph-walk modes used to feed the
  LLM walk and the mindmap UI:
    * ``focused``: personalised PageRank seeded at one node; the
      caller renders the top-K as the node's "neighbourhood of
      attention".
    * ``free_wander``: Node2Vec-style second-order random walk
      with the (p, q) bias the caller can tune. The walk path is
      the trajectory rendered on the mindmap as the "pollinator
      trail".

Member-level (``tenant_ctx``): every member can see the structure of
their own workspace; the data never leaks across tenants because the
underlying service queries are RLS-scoped.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    GardenGraphEdge,
    GardenGraphOut,
    GardenLinkSuggestion,
    GardenLinkSuggestionsOut,
    GardenWalkOut,
    GardenWalkStep,
)
from flow_core.services import graph as svc
from flow_core.services import link_prediction as linkpred_svc

router = APIRouter(prefix="/garden", tags=["garden"])


@router.get("/graph", response_model=GardenGraphOut)
async def garden_graph(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> GardenGraphOut:
    """Materialise the workspace note-graph in one round-trip:

    - ``edges``: undirected ``(src, dst, weight)`` rows with the v1
      ``note_edge_strength`` aggregation (soft-OR of per-kind base
      contributions + Adamic-Adar tag overlap). Co-activity from
      Proposal A is Phase 2.
    - ``centrality``: ``{note_id: pagerank}`` over the manual
      directed link graph (damping=0.85, power iteration). The map
      sums to 1.0 across the workspace.

    Both computed on demand (no cache). Bounded cost: O(L) for the
    edges, O(iter · L) for PageRank; the typical garden's link count
    stays well under 10k so each call resolves comfortably under a
    second.
    """
    edges = await svc.compute_note_edge_weights(ctx.session, org_id=ctx.org_id)
    centrality = await svc.compute_pagerank(ctx.session, org_id=ctx.org_id)
    return GardenGraphOut(
        edges=[GardenGraphEdge(src=e.src, dst=e.dst, weight=e.weight) for e in edges],
        centrality=centrality,
    )


@router.get("/walk", response_model=GardenWalkOut)
async def garden_walk(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    seed: Annotated[uuid.UUID, Query(description="Note id to seed the walk on")],
    mode: Annotated[
        Literal["focused", "free_wander"],
        Query(description="focused = PPR seeded; free_wander = Node2Vec walk"),
    ] = "focused",
    budget: Annotated[int, Query(ge=1, le=200)] = 24,
    p: Annotated[float, Query(gt=0.0, le=10.0)] = 1.0,
    q: Annotated[float, Query(gt=0.0, le=10.0)] = 1.0,
    seed_rng: Annotated[int | None, Query(ge=0)] = None,
) -> GardenWalkOut:
    """Two graph walks rooted at ``seed``.

    ``focused`` runs personalised PageRank teleporting on ``seed`` and
    returns the top ``budget`` nodes by induced mass. Use when the
    user wants "neighbourhood of attention".

    ``free_wander`` runs a Node2Vec second-order biased random walk
    of length ``budget`` from ``seed`` with parameters ``p`` (return
    bias) and ``q`` (in-out bias). Use for cross-domain exploration
    in the mindmap pollinator-trail animation.
    """
    if mode == "focused":
        ranks = await svc.compute_personalized_pagerank(
            ctx.session, org_id=ctx.org_id, seed_ids=[seed]
        )
        # Drop the seed from the result (the caller already has it)
        # and return top-K by mass, ascending order = walk index.
        ordered = sorted(
            ((nid, m) for nid, m in ranks.items() if nid != seed and m > 0),
            key=lambda kv: -kv[1],
        )[:budget]
        steps = [
            GardenWalkStep(note_id=nid, step=i + 1, weight=mass)
            for i, (nid, mass) in enumerate(ordered)
        ]
        return GardenWalkOut(seed=seed, mode=mode, steps=steps)
    # free_wander
    path = await svc.biased_random_walk(
        ctx.session,
        org_id=ctx.org_id,
        seed_id=seed,
        budget=budget,
        p=p,
        q=q,
        seed_rng=seed_rng,
    )
    steps = [
        GardenWalkStep(note_id=nid, step=i, weight=1.0 / max(1, i))
        for i, nid in enumerate(path)
        if nid != seed or i == 0
    ]
    return GardenWalkOut(seed=seed, mode=mode, steps=steps)


@router.get("/link-suggestions/{note_id}", response_model=GardenLinkSuggestionsOut)
async def garden_link_suggestions(
    note_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    k: Annotated[int, Query(ge=1, le=20)] = 5,
) -> GardenLinkSuggestionsOut:
    """Top-K candidate notes to link from ``note_id`` (task c7d0bb4c).

    The score mixes Adamic-Adar tag overlap and PPR-induced mass;
    already-linked pairs are excluded. The SPA renders them as
    'suggested links' chips; nothing is created without explicit
    user confirmation."""
    rows = await linkpred_svc.suggest_links_for_note(
        ctx.session, org_id=ctx.org_id, note_id=note_id, k=k
    )
    return GardenLinkSuggestionsOut(
        source_note_id=note_id,
        suggestions=[
            GardenLinkSuggestion(
                note_id=r.note_id,
                score=r.score,
                rationale=r.rationale,
                signals=r.signals,
            )
            for r in rows
        ],
    )
