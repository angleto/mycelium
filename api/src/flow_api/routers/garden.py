"""Garden analytics router (tasks 4467acb4 + 8c0a8f08 + 5bf31b63).

Read-only endpoints over the workspace note-link graph:

- ``GET /garden/graph``: weighted edges + global PageRank.
- ``GET /garden/clusters``: Leiden communities + global modularity.
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

import datetime
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    GardenApplyIn,
    GardenApplyOut,
    GardenClassifyOut,
    GardenClustersOut,
    GardenClusterSuggestionOut,
    GardenGraphEdge,
    GardenGraphOut,
    GardenHealthMetricOut,
    GardenHealthOut,
    GardenHealthSnapshotOut,
    GardenLinkCandidateOut,
    GardenLinkSuggestion,
    GardenLinkSuggestionsOut,
    GardenMaturitySuggestionOut,
    GardenTagSuggestionOut,
    GardenWalkOut,
    GardenWalkStep,
)
from flow_core.services import garden_classify as classify_svc
from flow_core.services import garden_health as health_svc
from flow_core.services import graph as svc
from flow_core.services import graph_snapshot as graph_snapshot_svc
from flow_core.services import link_prediction as linkpred_svc

router = APIRouter(prefix="/garden", tags=["garden"])


@router.get("/health", response_model=GardenHealthOut)
async def garden_health(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> GardenHealthOut:
    """Structural symbiosis sensors (ADR-0035): the current live readings
    plus the recent daily snapshots (newest first) for the sparkline.
    "Show, never judge" -- values + floors, never a verdict."""
    health = await health_svc.compute_health(ctx.session, org_id=ctx.org_id)
    snaps = await health_svc.recent_snapshots(ctx.session, org_id=ctx.org_id, days=30)
    return GardenHealthOut(
        generated_at=datetime.datetime.now(datetime.UTC),
        metrics={k: GardenHealthMetricOut(**v) for k, v in health.as_dict().items()},
        trend=[
            GardenHealthSnapshotOut(
                day=s.day,
                metrics={k: GardenHealthMetricOut(**v) for k, v in s.metrics.items()},
            )
            for s in snaps
        ],
    )


@router.get("/health/timeseries", response_model=list[GardenHealthSnapshotOut])
async def garden_health_timeseries(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    days: Annotated[int, Query(ge=1, le=365)] = 90,
) -> list[GardenHealthSnapshotOut]:
    """Daily garden-health snapshots over the last ``days`` (newest first),
    for the per-metric drill-down chart (task b820d223). Reads the persisted
    ``garden_health_daily`` rows only, no live recompute -- a longer,
    cheaper window than the 30-day trend bundled into ``GET /garden/health``."""
    snaps = await health_svc.recent_snapshots(ctx.session, org_id=ctx.org_id, days=days)
    return [
        GardenHealthSnapshotOut(
            day=s.day,
            metrics={k: GardenHealthMetricOut(**v) for k, v in s.metrics.items()},
        )
        for s in snaps
    ]


@router.get("/graph", response_model=GardenGraphOut)
async def garden_graph(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> GardenGraphOut:
    """Materialise the workspace note-graph in one round-trip:

    - ``edges``: undirected ``(src, dst, weight)`` rows with the v1
      ``note_edge_strength`` aggregation (soft-OR of per-kind base
      contributions + Adamic-Adar tag overlap). Co-activity from
      Proposal A is Phase 2.
    - ``centrality``: ``{note_id: pagerank}`` over the manual
      directed link graph (damping=0.85, power iteration). The map
      sums to 1.0 across the workspace.
    - ``recency``: separate freshness axis per note (``exp(-age/tau)``),
      live, for cold-start compensation (task d8664631).
    - ``betweenness``: cluster-bridge centrality from the worker's
      materialised snapshot (O(V·E) Brandes is offline-only). Empty
      until the first snapshot; ``analytics_computed_at`` labels its
      age so the client can tell "no bridges" from "not computed yet".

    Edges / PageRank / recency are computed on demand (no cache):
    bounded cost (O(L), O(iter · L), O(N)) and the post-edit reload
    must see fresh weights. Serving them snapshot-first is the planned
    flip once latency/volume demand it (the snapshot already stores
    them).
    """
    edges = await svc.compute_note_edge_weights(ctx.session, org_id=ctx.org_id)
    centrality = await svc.compute_pagerank(ctx.session, org_id=ctx.org_id)
    recency = await svc.compute_recency(ctx.session, org_id=ctx.org_id)
    snap = await graph_snapshot_svc.get_graph_snapshot(ctx.session, org_id=ctx.org_id)
    betweenness: dict[uuid.UUID, float] = {}
    if snap is not None:
        betweenness = {uuid.UUID(k): float(v) for k, v in snap.betweenness.items()}
    return GardenGraphOut(
        edges=[GardenGraphEdge(src=e.src, dst=e.dst, weight=e.weight) for e in edges],
        centrality=centrality,
        betweenness=betweenness,
        recency=recency,
        analytics_computed_at=snap.computed_at if snap is not None else None,
    )


@router.get("/clusters", response_model=GardenClustersOut)
async def garden_clusters(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> GardenClustersOut:
    """Leiden communities over the weighted note graph (task 8c0a8f08).

    Separate from ``/graph`` on purpose: clustering is heavier than the
    edge/PageRank pass and the SPA only needs it when the user toggles
    cluster-colouring on, so it is not paid on every mindmap load.
    Returns ``{note_id: community_index}`` plus the global modularity
    (ADR-0035 structure thermometer). When the optional ``clustering``
    extra (python-igraph + leidenalg) is absent the result is an empty
    map with ``modularity=null`` — the mindmap simply renders no cluster
    colours rather than erroring.
    """
    res = await svc.compute_leiden_clusters(ctx.session, org_id=ctx.org_id)
    return GardenClustersOut(
        clusters=res.clusters,
        modularity=res.modularity,
        count=len(set(res.clusters.values())),
    )


@router.get("/classify/{node_id}", response_model=GardenClassifyOut)
async def garden_classify(
    node_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    kinds: Annotated[
        str | None,
        Query(description="CSV subset of tags,links,maturity,cluster (default: all)"),
    ] = None,
) -> GardenClassifyOut:
    """The proposal engine (ADR-0032): a structured enrichment proposal
    ``{tags, links, maturity, cluster}`` for a note, each with confidence
    + rationale, plus ``signals_used`` for transparency. **Read-only** —
    nothing is mutated; the user (or an agent) applies a suggestion via
    ``POST /garden/apply``. v1 classifies notes (404 otherwise). Unknown
    ``kinds`` tokens are dropped; an all-unknown set falls back to all."""
    wanted: frozenset[str] | None = None
    if kinds is not None:
        requested = {k.strip() for k in kinds.split(",") if k.strip()}
        wanted = frozenset(requested & classify_svc.ALL_KINDS) or classify_svc.ALL_KINDS
    res = await classify_svc.classify_node(
        ctx.session, org_id=ctx.org_id, node_id=node_id, kinds=wanted
    )
    return GardenClassifyOut(
        node_id=res.node_id,
        node_kind=res.node_kind,
        tags=[
            GardenTagSuggestionOut(tag_id=t.tag_id, confidence=t.confidence, rationale=t.rationale)
            for t in res.tags
        ],
        links=[
            GardenLinkCandidateOut(
                target_id=lc.target_id,
                link_kind=lc.link_kind,
                confidence=lc.confidence,
                rationale=lc.rationale,
            )
            for lc in res.links
        ],
        maturity=(
            GardenMaturitySuggestionOut(
                value=res.maturity.value,
                confidence=res.maturity.confidence,
                rationale=res.maturity.rationale,
                auto_apply=res.maturity.auto_apply,
            )
            if res.maturity is not None
            else None
        ),
        cluster=(
            GardenClusterSuggestionOut(
                leiden_id=res.cluster.leiden_id,
                modularity=res.cluster.modularity,
                confidence=res.cluster.confidence,
            )
            if res.cluster is not None
            else None
        ),
        signals_used=res.signals_used,
        model_version=res.model_version,
        generated_at=res.generated_at,
    )


@router.post("/apply", response_model=GardenApplyOut)
async def garden_apply(
    body: GardenApplyIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> GardenApplyOut:
    """Apply or decline a ``garden_classify`` suggestion (ADR-0032 /
    ADR-0037). ``accept``/``override`` perform the mutation via the
    existing idempotent services; ``reject``/``ignore`` mutate nothing.
    Either way an append-only ``classification_feedback`` event is
    written — the audit trail behind the learning loop and rollback."""
    feedback = await classify_svc.apply_suggestion(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        node_id=body.node_id,
        suggestion_type=body.suggestion_type,
        suggestion_value=body.suggestion_value,
        action=body.action,
        override_value=body.override_value,
        model_version=body.model_version or classify_svc.MODEL_VERSION,
        signals_snapshot=body.signals_snapshot,
    )
    return GardenApplyOut(
        feedback_id=feedback.id,
        node_id=body.node_id,
        suggestion_type=body.suggestion_type,
        action=body.action,
        applied=body.action in ("accept", "override"),
    )


@router.get("/walk", response_model=GardenWalkOut)
async def garden_walk(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
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
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
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
