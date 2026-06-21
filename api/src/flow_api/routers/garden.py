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
    GardenEventOut,
    GardenFeatureDeltaOut,
    GardenGraphEdge,
    GardenGraphOut,
    GardenHealthEventOut,
    GardenHealthMetricOut,
    GardenHealthOut,
    GardenHealthSnapshotOut,
    GardenLearningRollbackIn,
    GardenLearningRollbackOut,
    GardenLearningTelemetryOut,
    GardenLinkCandidateOut,
    GardenLinkSuggestion,
    GardenLinkSuggestionsOut,
    GardenMaturitySuggestionOut,
    GardenRejectHotspotOut,
    GardenReviewActionIn,
    GardenReviewActionOut,
    GardenReviewPendingItem,
    GardenTagSuggestionOut,
    GardenWalkOut,
    GardenWalkStep,
)
from flow_core.config import get_settings
from flow_core.models.precomputed_suggestion import PrecomputedSuggestion
from flow_core.services import event_bus
from flow_core.services import garden_classify as classify_svc
from flow_core.services import garden_health as health_svc
from flow_core.services import garden_learning as learning_svc
from flow_core.services import garden_review as review_svc
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


@router.get("/health/events", response_model=list[GardenHealthEventOut])
async def garden_health_events(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    days: Annotated[int, Query(ge=1, le=365)] = 90,
) -> list[GardenHealthEventOut]:
    """The "what changed" timeline (ADR-0035 §84, task d0bada67): discrete
    events that plausibly explain a shift in the sensors -- a classifier
    bump or a bulk corpus edit -- so a reading is interpreted, not
    guessed. Newest first. Derived live from the existing audit +
    feedback streams (no separate event store), workspace-scoped by RLS.
    "Show, never judge": facts, never a verdict."""
    events = await health_svc.recent_events(ctx.session, org_id=ctx.org_id, days=days)
    return [GardenHealthEventOut(at=e.at, kind=e.kind, detail=e.detail) for e in events]


@router.get("/audit", response_model=list[GardenEventOut])
async def garden_audit(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    days: Annotated[int | None, Query(ge=1, le=365)] = None,
) -> list[GardenEventOut]:
    """The workspace event stream (ADR-0036 audit panel): the coordinated
    read/propose/commit/reject/snapshot events, newest first, RLS-scoped.
    ``days`` bounds the window (replay-from-cursor for subscribers).
    "Show, never judge": the verbatim events, never a verdict."""
    since = (
        None
        if days is None
        else datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
    )
    events = await event_bus.recent_events(ctx.session, org_id=ctx.org_id, limit=limit, since=since)
    return [
        GardenEventOut(
            id=e.id,
            actor_id=e.actor_id,
            actor_kind=e.actor_kind,
            kind=e.kind,
            node_kind=e.node_kind,
            node_id=e.node_id,
            parent_event_id=e.parent_event_id,
            payload=e.payload,
            ts=e.ts,
            applied_at=e.applied_at,
            applied_state=e.applied_state,
        )
        for e in events
    ]


@router.get("/graph", response_model=GardenGraphOut)
async def garden_graph(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> GardenGraphOut:
    """Materialise the workspace note-graph in one round-trip:

    - ``edges``: undirected ``(src, dst, weight)`` rows with the
      ``note_edge_strength`` aggregation (soft-OR of per-kind base
      contributions + Adamic-Adar tag overlap + co-activity from the
      worker-materialised ``note_coactivity``, task f0a15247).
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
    # Unified surface (ADR-0042 D1): the mindmap spans notes + tasks when the
    # fleet opts in. Recency stays a note axis (tasks have no maturity clock).
    include_tasks = get_settings().garden_unified_task_graph_enabled
    edges = await svc.compute_note_edge_weights(
        ctx.session, org_id=ctx.org_id, include_tasks=include_tasks
    )
    centrality = await svc.compute_pagerank(
        ctx.session, org_id=ctx.org_id, include_tasks=include_tasks
    )
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
    res = await svc.compute_leiden_clusters(
        ctx.session,
        org_id=ctx.org_id,
        include_tasks=get_settings().garden_unified_task_graph_enabled,
    )
    return GardenClustersOut(
        clusters=res.clusters,
        modularity=res.modularity,
        count=len(set(res.clusters.values())),
    )


def _classify_out_from_live(res: classify_svc.ClassifyResult) -> GardenClassifyOut:
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
        source="live",
    )


def _classify_out_from_cache(
    node_id: uuid.UUID,
    rows: list[PrecomputedSuggestion],
    wanted: frozenset[str],
) -> GardenClassifyOut:
    """Rebuild the response from the persisted ``precomputed_suggestions``
    cache (ADR-0042 D4/D6). Cached rows carry the apply-shape value + the
    confidence + rationale; the two display-only extras the cache does not
    keep (maturity ``auto_apply`` / cluster ``modularity``) default to a safe
    value — the live ``refresh`` path renders them faithfully."""
    tags: list[GardenTagSuggestionOut] = []
    links: list[GardenLinkCandidateOut] = []
    maturity: GardenMaturitySuggestionOut | None = None
    cluster: GardenClusterSuggestionOut | None = None
    for row in rows:
        v = row.suggestion_value
        if row.suggestion_type == "tag" and "tags" in wanted:
            tags.append(
                GardenTagSuggestionOut(
                    tag_id=uuid.UUID(str(v["tag_id"])),
                    confidence=row.confidence,
                    rationale=row.rationale or "",
                )
            )
        elif row.suggestion_type == "link" and "links" in wanted:
            links.append(
                GardenLinkCandidateOut(
                    target_id=uuid.UUID(str(v["target_id"])),
                    link_kind=str(v.get("link_kind", "related")),
                    confidence=row.confidence,
                    rationale=row.rationale or "",
                )
            )
        elif row.suggestion_type == "maturity" and "maturity" in wanted:
            maturity = GardenMaturitySuggestionOut(
                value=str(v["value"]),
                confidence=row.confidence,
                rationale=row.rationale or "",
                auto_apply=bool(v.get("auto_apply", False)),
            )
        elif row.suggestion_type == "cluster" and "cluster" in wanted:
            cluster = GardenClusterSuggestionOut(
                leiden_id=v.get("leiden_id"),
                modularity=v.get("modularity"),
                confidence=row.confidence,
            )
    return GardenClassifyOut(
        node_id=node_id,
        node_kind=rows[0].node_kind,
        tags=tags,
        links=links,
        maturity=maturity,
        cluster=cluster,
        signals_used=["precomputed"],
        model_version=classify_svc.MODEL_VERSION,
        generated_at=rows[0].computed_at,
        source="precomputed",
    )


@router.get("/classify/{node_id}", response_model=GardenClassifyOut)
async def garden_classify(
    node_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    kinds: Annotated[
        str | None,
        Query(description="CSV subset of tags,links,maturity,cluster (default: all)"),
    ] = None,
    refresh: Annotated[
        bool,
        Query(description="Bypass the persisted cache and recompute live (ADR-0042 D6)."),
    ] = False,
) -> GardenClassifyOut:
    """The proposal engine (ADR-0032 / ADR-0042): a structured enrichment
    proposal ``{tags, links, maturity, cluster}`` for a note or task, each
    with confidence + rationale, plus ``signals_used`` for transparency.
    **Read-only** — nothing is mutated; the user (or an agent) applies a
    suggestion via ``POST /garden/apply``.

    Serves the persisted on-create suggestions when fresh (``source =
    precomputed``); otherwise — or when ``refresh`` is set — recomputes live
    (``source = live``). The cache is populated by the on-create queue, so a
    read never writes. Tasks are classifiable only when the unified-task-graph
    flag is on (404 otherwise). Unknown ``kinds`` tokens are dropped; an
    all-unknown set falls back to all."""
    wanted: frozenset[str] | None = None
    if kinds is not None:
        requested = {k.strip() for k in kinds.split(",") if k.strip()}
        wanted = frozenset(requested & classify_svc.ALL_KINDS) or classify_svc.ALL_KINDS
    effective = wanted if wanted is not None else classify_svc.ALL_KINDS
    if not refresh:
        cached = await classify_svc.read_classification(
            ctx.session, org_id=ctx.org_id, node_id=node_id
        )
        if cached is not None:
            return _classify_out_from_cache(node_id, cached, effective)
    res = await classify_svc.classify_node(
        ctx.session, org_id=ctx.org_id, node_id=node_id, kinds=wanted, user_id=ctx.user_id
    )
    return _classify_out_from_live(res)


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


@router.get("/review/pending", response_model=list[GardenReviewPendingItem])
async def garden_review_pending(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[GardenReviewPendingItem]:
    """The review inbox (ADR-0043): AUTONOMOUSLY-generated humus notes awaiting
    human approval (``review_state='proposed'``), newest first, each with the
    model that produced it (``origin_model_id``) so the reviewer sees WHICH
    model wrote the summary. A pure read; RLS-scoped."""
    pending = await review_svc.list_pending(ctx.session, org_id=ctx.org_id, limit=limit)
    return [
        GardenReviewPendingItem(
            note_id=p.note_id,
            title=p.title,
            humus_kind=p.humus_kind,
            origin_model_id=p.origin_model_id,
            preview=p.preview,
            created_at=p.created_at,
        )
        for p in pending
    ]


@router.post("/review/approve", response_model=GardenReviewActionOut)
async def garden_review_approve(
    body: GardenReviewActionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> GardenReviewActionOut:
    """Approve a proposed humus note (ADR-0043): it becomes effective and
    re-enters retrieval/search/listings. Audited; emits a bus ``commit``
    event. Idempotent. Member role."""
    note = await review_svc.approve_node(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, note_id=body.note_id
    )
    return GardenReviewActionOut(
        note_id=note.id,
        review_state=note.review_state,
        origin_model_id=note.origin_model_id,
        rejected=False,
    )


@router.post("/review/reject", response_model=GardenReviewActionOut)
async def garden_review_reject(
    body: GardenReviewActionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> GardenReviewActionOut:
    """Reject a proposed humus note (ADR-0043): soft-delete it so a weak
    summary never pollutes the corpus (reversible via trash/restore). Audited;
    emits a bus ``reject`` event carrying ``origin_model_id``. Idempotent.
    Member role."""
    note = await review_svc.reject_node(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        note_id=body.note_id,
        reason=body.reason,
    )
    return GardenReviewActionOut(
        note_id=note.id,
        review_state=note.review_state,
        origin_model_id=note.origin_model_id,
        rejected=note.deleted_at is not None,
    )


def _delta_out(d: learning_svc.FeatureDelta) -> GardenFeatureDeltaOut:
    return GardenFeatureDeltaOut(
        feature_key=d.feature_key, before=d.before, after=d.after, delta=d.delta
    )


@router.post("/learning/rollback", response_model=GardenLearningRollbackOut)
async def garden_learning_rollback(
    body: GardenLearningRollbackIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> GardenLearningRollbackOut:
    """Rewind the caller's own learned priors to their state at ``to``
    (ADR-0037 "Snapshots and rollback"): restores the closest daily
    snapshot at-or-before the cut, replays the feedback delta on top, and
    writes a fresh checkpoint — decay-aware and fully reproducible. Returns
    a one-line diff of the largest-moved feature. Per-user: a member can
    only rewind their own priors (``ctx.user_id``)."""
    res = await learning_svc.rollback_priors(
        ctx.session, org_id=ctx.org_id, user_id=ctx.user_id, to=body.to
    )
    if res.top_change is None:
        summary = "No prior changed: the priors already matched that point in time."
    else:
        tc = res.top_change
        direction = "less" if tc.delta < 0 else "more"
        summary = (
            f"Rewound {res.features_changed} feature(s); the system is now {direction} "
            f"biased toward {tc.feature_key} ({tc.before:+.2f} -> {tc.after:+.2f})."
        )
    return GardenLearningRollbackOut(
        rolled_back_to=res.rolled_back_to,
        snapshot_at=res.snapshot_at,
        replayed_events=res.replayed_events,
        features_changed=res.features_changed,
        top_change=_delta_out(res.top_change) if res.top_change is not None else None,
        summary=summary,
    )


@router.get("/learning/telemetry", response_model=GardenLearningTelemetryOut)
async def garden_learning_telemetry(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    reject_days: Annotated[int, Query(ge=1, le=365)] = 90,
    drift_days: Annotated[int, Query(ge=1, le=365)] = 30,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> GardenLearningTelemetryOut:
    """The caller's learning telemetry for the sensors dashboard (ADR-0037):

    - ``reject_hotspots``: the suggestion features they decline most, so
      they can mute at the source.
    - ``drift``: which priors moved the most over ``drift_days`` (vs the
      snapshot that old; empty until a snapshot that old exists).

    Read-only, the caller's own history only (ADR-0037 privacy: no
    cross-user comparison). "Show, never judge"."""
    hotspots = await learning_svc.reject_hotspots(
        ctx.session, org_id=ctx.org_id, user_id=ctx.user_id, days=reject_days, limit=limit
    )
    drift = await learning_svc.prior_drift(
        ctx.session, org_id=ctx.org_id, user_id=ctx.user_id, days=drift_days, limit=limit
    )
    return GardenLearningTelemetryOut(
        reject_hotspots=[
            GardenRejectHotspotOut(
                suggestion_type=h.suggestion_type,
                feature_key=h.feature_key,
                declines=h.declines,
                last_declined_at=h.last_declined_at,
            )
            for h in hotspots
        ],
        drift=[_delta_out(d) for d in drift],
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
    # Mark which steps are humus so the mindmap renders the leaf (ADR-0034);
    # the walk already biased toward them, this is the transparency half.
    humus = await svc.humus_note_ids(ctx.session, org_id=ctx.org_id)
    steps = [
        GardenWalkStep(
            note_id=nid,
            step=i,
            weight=1.0 / max(1, i),
            provenance="humus" if nid in humus else None,
        )
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
