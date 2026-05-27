"""Garden analytics router (tasks 4467acb4 + 8c0a8f08, Phase 1).

One read-only endpoint, ``GET /garden/graph``, that exposes the
workspace's note-link graph as weighted undirected edges plus a
PageRank centrality map. Both are computed on demand (Phase 2 will
materialise; ADR-0031 traces the staging).

Member-level (``tenant_ctx``): every member can see the structure of
their own workspace; the data never leaks across tenants because the
underlying service queries are RLS-scoped.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import GardenGraphEdge, GardenGraphOut
from flow_core.services import graph as svc

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
