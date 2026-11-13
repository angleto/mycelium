"""The garden's operations (ADR-0068): the first domain on the registry.

Each function here used to exist twice, as a REST route body in
``mycelium_api.routers.garden`` and as an MCP tool body in
``mycelium_mcp.server``, and the two had drifted: the tool never read the
classification cache and dropped the caller's learned priors, the route
served link suggestions without titles, the tool listed candidates without
the project perimeter. One body now serves both surfaces.

Who may call what is unchanged: reads cost ``notes:read``, the event stream
``events:read``, writes ``notes:write``, and approving or rejecting
generated text stays a person's act (``HUMAN_ONLY``) unless an assistant
carries out a decision the person wrote down.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select

from mycelium_core.config import get_settings
from mycelium_core.mcp_scopes import HUMAN_ONLY
from mycelium_core.models.garden_health import GardenHealthDaily
from mycelium_core.models.note import Note
from mycelium_core.models.precomputed_suggestion import PrecomputedSuggestion
from mycelium_core.operations import Kind, OpCtx, executor_identity, operation
from mycelium_core.operations.garden_views import (
    GardenAcceptRatioOut,
    GardenApplyIn,
    GardenApplyOut,
    GardenCandidateEdge,
    GardenCandidateNode,
    GardenCandidatesIn,
    GardenCandidatesOut,
    GardenClassifyIn,
    GardenClassifyOut,
    GardenClustersOut,
    GardenClusterSuggestionOut,
    GardenDaysIn,
    GardenEventOut,
    GardenEventsIn,
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
    GardenLinkSuggestionsIn,
    GardenLinkSuggestionsOut,
    GardenListIn,
    GardenMaturitySuggestionOut,
    GardenNoArgs,
    GardenReadingSetIn,
    GardenReadingStep,
    GardenRejectHotspotOut,
    GardenRestoreSourceIn,
    GardenRestoreSourceOut,
    GardenReviewActionIn,
    GardenReviewActionOut,
    GardenReviewDecisionIn,
    GardenReviewDecisionOut,
    GardenReviewPendingItem,
    GardenReviewRejectedItem,
    GardenTagSuggestionOut,
    GardenTelemetryIn,
    GardenWalkIn,
    GardenWalkOut,
    GardenWalkStep,
)
from mycelium_core.services import candidates as candidates_svc
from mycelium_core.services import event_bus, focus_context, graph_local
from mycelium_core.services import garden_classify as classify_svc
from mycelium_core.services import garden_health as health_svc
from mycelium_core.services import garden_learning as learning_svc
from mycelium_core.services import garden_review as review_svc
from mycelium_core.services import graph as graph_svc
from mycelium_core.services import graph_snapshot as graph_snapshot_svc
from mycelium_core.services import link_prediction as linkpred_svc

GARDEN = "garden"

# --------------------------------------------------------------------------
# Sensors
# --------------------------------------------------------------------------


def _snapshot_out(s: GardenHealthDaily) -> GardenHealthSnapshotOut:
    return GardenHealthSnapshotOut(
        day=s.day,
        metrics={k: GardenHealthMetricOut(**v) for k, v in s.metrics.items()},
    )


@operation(
    "garden.health",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenNoArgs,
    result=GardenHealthOut,
)
async def health(ctx: OpCtx, _p: GardenNoArgs) -> GardenHealthOut:
    """The garden's structural sensors (ADR-0035): the current readings and
    the last 30 daily snapshots, newest first. Values and floors, never a
    verdict."""
    live = await health_svc.compute_health(ctx.session, org_id=ctx.org_id)
    snaps = await health_svc.recent_snapshots(ctx.session, org_id=ctx.org_id, days=30)
    return GardenHealthOut(
        generated_at=datetime.datetime.now(datetime.UTC),
        metrics={k: GardenHealthMetricOut(**v) for k, v in live.as_dict().items()},
        trend=[_snapshot_out(s) for s in snaps],
    )


@operation(
    "garden.health.timeseries",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenDaysIn,
    result=list[GardenHealthSnapshotOut],
)
async def health_timeseries(ctx: OpCtx, p: GardenDaysIn) -> list[GardenHealthSnapshotOut]:
    """Daily sensor snapshots over the last ``days``, newest first. Reads the
    persisted snapshots only, no live recompute."""
    snaps = await health_svc.recent_snapshots(ctx.session, org_id=ctx.org_id, days=p.days)
    return [_snapshot_out(s) for s in snaps]


@operation(
    "garden.health.events",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenDaysIn,
    result=list[GardenHealthEventOut],
)
async def health_events(ctx: OpCtx, p: GardenDaysIn) -> list[GardenHealthEventOut]:
    """What changed (ADR-0035): the discrete events that plausibly explain a
    shift in the sensors, a classifier bump or a bulk edit, newest first."""
    events = await health_svc.recent_events(ctx.session, org_id=ctx.org_id, days=p.days)
    return [GardenHealthEventOut(at=e.at, kind=e.kind, detail=e.detail) for e in events]


@operation(
    "garden.events",
    domain=GARDEN,
    kind=Kind.READ,
    access="events:read",
    params=GardenEventsIn,
    result=list[GardenEventOut],
)
async def events(ctx: OpCtx, p: GardenEventsIn) -> list[GardenEventOut]:
    """The workspace coordination event stream (ADR-0036): the read, propose,
    commit, reject and snapshot events people and agents emit, newest first.
    There is no subscription: poll, passing the largest ``ts`` seen back as
    ``since``; a page of exactly ``limit`` rows may have more behind it."""
    since = p.since
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=datetime.UTC)
    if since is None and p.days is not None:
        since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=p.days)
    rows = await event_bus.recent_events(ctx.session, org_id=ctx.org_id, limit=p.limit, since=since)
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
        for e in rows
    ]


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


@operation(
    "garden.graph",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenNoArgs,
    result=GardenGraphOut,
    only="rest",
    why=(
        "The whole workspace's edge list and centrality maps, which grow with the "
        "workspace, feed the mindmap renderer. An agent navigates the same graph "
        "with graph_walk and graph_focus_context."
    ),
)
async def graph(ctx: OpCtx, _p: GardenNoArgs) -> GardenGraphOut:
    """The workspace note graph in one answer: weighted undirected edges,
    PageRank centrality, live recency, and the betweenness of the last
    snapshot (``analytics_computed_at`` is its age; empty until the first)."""
    include_tasks = get_settings().garden_unified_task_graph_enabled
    edges = await graph_svc.compute_note_edge_weights(
        ctx.session, org_id=ctx.org_id, include_tasks=include_tasks
    )
    centrality = await graph_svc.compute_pagerank(
        ctx.session, org_id=ctx.org_id, include_tasks=include_tasks
    )
    recency = await graph_svc.compute_recency(ctx.session, org_id=ctx.org_id)
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


@operation(
    "garden.clusters",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenNoArgs,
    result=GardenClustersOut,
)
async def clusters(ctx: OpCtx, _p: GardenNoArgs) -> GardenClustersOut:
    """Leiden communities over the weighted note graph: the community index
    of every note and the partition's modularity. An empty map and a null
    modularity when the clustering extra is not installed."""
    res = await graph_svc.compute_leiden_clusters(
        ctx.session,
        org_id=ctx.org_id,
        include_tasks=get_settings().garden_unified_task_graph_enabled,
    )
    return GardenClustersOut(
        clusters=res.clusters,
        modularity=res.modularity,
        count=len(set(res.clusters.values())),
    )


@operation(
    "garden.walk",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenWalkIn,
    result=GardenWalkOut,
    only="rest",
    why=(
        "A trajectory for the mindmap's pollinator animation, revisits included. "
        "An agent reads the same walks as a reading set with titles and snippets "
        "(garden.walk.reading_set)."
    ),
)
async def walk(ctx: OpCtx, p: GardenWalkIn) -> GardenWalkOut:
    """Three graph walks rooted at ``seed``: ``focused`` (personalised
    PageRank, top ``budget`` by mass), ``free_wander`` (a Node2Vec walk of
    length ``budget`` biased by ``p`` and ``q``), ``bounded`` (best-first,
    cost independent of the workspace size)."""
    seed = p.seed
    if p.mode == "bounded":
        hood = await graph_local.bounded_neighborhood(
            ctx.session,
            org_id=ctx.org_id,
            actor_id=ctx.actor_id,
            seed_note_id=seed,
            node_budget=p.budget,
        )
        humus_b = await graph_svc.humus_note_ids(ctx.session, org_id=ctx.org_id)
        return GardenWalkOut(
            seed=seed,
            mode=p.mode,
            steps=[
                GardenWalkStep(
                    note_id=n.note_id,
                    step=n.hop,
                    weight=n.weight,
                    provenance="humus" if n.note_id in humus_b else None,
                )
                for n in hood.nodes
            ],
        )
    if p.mode == "focused":
        ranks = await graph_svc.compute_personalized_pagerank(
            ctx.session, org_id=ctx.org_id, seed_ids=[seed]
        )
        ordered = sorted(
            ((nid, m) for nid, m in ranks.items() if nid != seed and m > 0),
            key=lambda kv: -kv[1],
        )[: p.budget]
        steps = [
            GardenWalkStep(note_id=nid, step=i + 1, weight=mass)
            for i, (nid, mass) in enumerate(ordered)
        ]
        return GardenWalkOut(seed=seed, mode=p.mode, steps=steps)
    path = await graph_svc.biased_random_walk(
        ctx.session,
        org_id=ctx.org_id,
        seed_id=seed,
        budget=p.budget,
        p=p.p,
        q=p.q,
        seed_rng=p.seed_rng,
    )
    humus = await graph_svc.humus_note_ids(ctx.session, org_id=ctx.org_id)
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
    return GardenWalkOut(seed=seed, mode=p.mode, steps=steps)


@operation(
    "garden.walk.reading_set",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenReadingSetIn,
    result=list[GardenReadingStep],
    only="mcp",
    why=(
        "The SPA draws the trajectory (garden.walk). A reading set, each note "
        "once with its title and a snippet, is for a reader without the mindmap."
    ),
)
async def walk_reading_set(ctx: OpCtx, p: GardenReadingSetIn) -> list[GardenReadingStep]:
    """Traverse the note graph from ``seed`` and return the walked notes as a
    reading set: ``focused`` is the seed's personalised-PageRank
    neighbourhood, ``free_wander`` a Node2Vec walk that drifts across
    domains, ``bounded`` a best-first traversal whose cost does not grow
    with the workspace. Each step carries title, snippet and provenance, so
    you can move several hops without a lookup per note. For a reading set
    shaped by a query, use graph_focus_context. Read-only, no LLM."""
    steps = await focus_context.walk_context(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        seed_id=p.seed,
        mode=p.mode,
        budget=p.budget,
    )
    return [
        GardenReadingStep(
            note_id=w.note_id,
            step=w.step,
            weight=w.weight,
            title=w.title,
            snippet=w.snippet,
            provenance=w.provenance,
        )
        for w in steps
    ]


@operation(
    "garden.link_suggestions",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenLinkSuggestionsIn,
    result=GardenLinkSuggestionsOut,
)
async def link_suggestions(ctx: OpCtx, p: GardenLinkSuggestionsIn) -> GardenLinkSuggestionsOut:
    """The top ``k`` notes to link from ``note_id``: Adamic-Adar over shared
    tags mixed with personalised-PageRank mass, already-linked pairs
    excluded. Nothing is created: a person or an agent confirms a link with
    link_notes. Empty when there is no signal."""
    rows = await linkpred_svc.suggest_links_for_note(
        ctx.session, org_id=ctx.org_id, note_id=p.note_id, k=p.k
    )
    ids = [r.note_id for r in rows]
    titles: dict[uuid.UUID, str | None] = {}
    if ids:
        found = await ctx.session.execute(select(Note.id, Note.title).where(Note.id.in_(ids)))
        titles = {nid: title for nid, title in found.all()}
    return GardenLinkSuggestionsOut(
        source_note_id=p.note_id,
        suggestions=[
            GardenLinkSuggestion(
                note_id=r.note_id,
                title=titles.get(r.note_id),
                score=r.score,
                rationale=r.rationale,
                signals=r.signals,
            )
            for r in rows
        ],
    )


# --------------------------------------------------------------------------
# Proposals: classify and apply
# --------------------------------------------------------------------------


def _classify_from_live(res: classify_svc.ClassifyResult) -> GardenClassifyOut:
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


def _classify_from_cache(
    node_id: uuid.UUID, rows: list[PrecomputedSuggestion], wanted: frozenset[str]
) -> GardenClassifyOut:
    """Rebuild the answer from the persisted suggestions (ADR-0042 D4/D6).
    The two display-only extras the cache does not keep (maturity
    ``auto_apply``, cluster ``modularity``) take a safe default; a
    ``refresh`` recomputes them."""
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


@operation(
    "garden.classify",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenClassifyIn,
    result=GardenClassifyOut,
)
async def classify(ctx: OpCtx, p: GardenClassifyIn) -> GardenClassifyOut:
    """The proposal engine (ADR-0032, ADR-0042): tags, links, maturity and
    cluster proposed for a note (or a task, when the unified task graph is
    on), each with a confidence and a rationale, plus ``signals_used``.
    Read-only: act on a proposal with garden_apply. Serves the suggestions
    persisted at creation when they exist (``source = precomputed``) and
    recomputes otherwise or on ``refresh`` (``source = live``). Unknown
    ``kinds`` are dropped; an all-unknown set means all."""
    wanted: frozenset[str] | None = None
    if p.kinds is not None:
        requested = {k.strip() for k in p.kinds.split(",") if k.strip()}
        wanted = frozenset(requested & classify_svc.ALL_KINDS) or classify_svc.ALL_KINDS
    effective = wanted if wanted is not None else classify_svc.ALL_KINDS
    if not p.refresh:
        cached = await classify_svc.read_classification(
            ctx.session, org_id=ctx.org_id, node_id=p.node_id
        )
        if cached is not None:
            return _classify_from_cache(p.node_id, cached, effective)
    res = await classify_svc.classify_node(
        ctx.session, org_id=ctx.org_id, node_id=p.node_id, kinds=wanted, user_id=ctx.actor_id
    )
    return _classify_from_live(res)


@operation(
    "garden.apply",
    domain=GARDEN,
    kind=Kind.WRITE,
    access="notes:write",
    params=GardenApplyIn,
    result=GardenApplyOut,
)
async def apply(ctx: OpCtx, p: GardenApplyIn) -> GardenApplyOut:
    """Apply or decline one garden_classify suggestion (ADR-0032, ADR-0037).
    ``accept`` and ``override`` perform the change through the ordinary
    idempotent services; ``reject`` and ``ignore`` change nothing. Every
    action records a classification_feedback event, the trail behind the
    learning loop and its rollback. ``auto`` is the worker's and is refused
    here, so a client cannot forge a system promotion."""
    feedback = await classify_svc.apply_suggestion(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        node_id=p.node_id,
        suggestion_type=p.suggestion_type,
        suggestion_value=p.suggestion_value,
        action=p.action,
        override_value=p.override_value,
        model_version=p.model_version or classify_svc.MODEL_VERSION,
        signals_snapshot=p.signals_snapshot,
    )
    return GardenApplyOut(
        feedback_id=feedback.id,
        node_id=p.node_id,
        suggestion_type=p.suggestion_type,
        action=p.action,
        applied=p.action in ("accept", "override"),
    )


@operation(
    "garden.candidates",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenCandidatesIn,
    result=GardenCandidatesOut,
)
async def candidates(ctx: OpCtx, p: GardenCandidatesIn) -> GardenCandidatesOut:
    """Is there distillation to do? Distillation is graph maintenance, so
    this returns two families, read-only and without an LLM call.

    NODE candidates compact inert material into a denser atom: ``distill``
    (one inert note), ``pattern`` (a Leiden cluster of archived notes),
    ``season`` (a quarter's archived notes). Act with distill_note,
    extract_cluster_pattern or synthesize_season.

    EDGE candidates curate the links: ``link_add`` (a strong pair with no
    link), ``link_prune`` (a related link whose basis has decayed),
    ``link_direct`` (search keeps crossing the pair one way; never applied
    automatically).

    Every atom produced from these waits in garden_review_pending until a
    person approves it."""
    result = await candidates_svc.list_distillation_candidates(
        ctx.session, org_id=ctx.org_id, project_id=ctx.project_id, kind=p.kind, limit=p.limit
    )
    return GardenCandidatesOut(
        nodes=[GardenCandidateNode(**n) for n in result["nodes"]],
        edges=[GardenCandidateEdge(**e) for e in result["edges"]],
    )


# --------------------------------------------------------------------------
# Review of generated text (ADR-0043)
# --------------------------------------------------------------------------


@operation(
    "garden.review.pending",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenListIn,
    result=list[GardenReviewPendingItem],
)
async def review_pending(ctx: OpCtx, p: GardenListIn) -> list[GardenReviewPendingItem]:
    """The review inbox (ADR-0043): generated notes awaiting a person's
    approval, newest first, each with the model that wrote it
    (``origin_model_id``) and the ``version`` to echo when acting on it."""
    pending = await review_svc.list_pending(ctx.session, org_id=ctx.org_id, limit=p.limit)
    return [
        GardenReviewPendingItem(
            note_id=x.note_id,
            title=x.title,
            humus_kind=x.humus_kind,
            origin_model_id=x.origin_model_id,
            preview=x.preview,
            created_at=x.created_at,
            version=x.version,
        )
        for x in pending
    ]


@operation(
    "garden.review.rejected",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenListIn,
    result=list[GardenReviewRejectedItem],
)
async def review_rejected(ctx: OpCtx, p: GardenListIn) -> list[GardenReviewRejectedItem]:
    """The review bin (ADR-0043): proposals a person declined, most recently
    declined first. No other listing shows them, so this is how a rejection
    is found again; undo one by restoring the note, echoing ``version``."""
    rejected = await review_svc.list_rejected(ctx.session, org_id=ctx.org_id, limit=p.limit)
    return [
        GardenReviewRejectedItem(
            note_id=r.note_id,
            title=r.title,
            humus_kind=r.humus_kind,
            origin_model_id=r.origin_model_id,
            preview=r.preview,
            created_at=r.created_at,
            rejected_at=r.rejected_at,
            version=r.version,
        )
        for r in rejected
    ]


def _action_out(note: Note) -> GardenReviewActionOut:
    return GardenReviewActionOut(
        note_id=note.id,
        review_state=note.review_state,
        origin_model_id=note.origin_model_id,
        rejected=note.deleted_at is not None,
        version=note.version,
    )


@operation(
    "garden.review.approve",
    domain=GARDEN,
    kind=Kind.WRITE,
    access=HUMAN_ONLY,
    params=GardenReviewActionIn,
    result=GardenReviewActionOut,
)
async def review_approve(ctx: OpCtx, p: GardenReviewActionIn) -> GardenReviewActionOut:
    """Approve a generated note (ADR-0043): it becomes effective and enters
    retrieval, search and listings; for a distillation, the source becomes
    humus if it is still inert. A person's act: a credential carrying a
    scope list cannot call it. Audited, idempotent. Pass ``expected_version``
    from the pending listing to fail with ``stale_version`` if the note
    changed after you read it; ``reason`` is ignored."""
    note = await review_svc.approve_node(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        note_id=p.note_id,
        expected_version=p.expected_version,
    )
    return _action_out(note)


@operation(
    "garden.review.reject",
    domain=GARDEN,
    kind=Kind.WRITE,
    access=HUMAN_ONLY,
    params=GardenReviewActionIn,
    result=GardenReviewActionOut,
)
async def review_reject(ctx: OpCtx, p: GardenReviewActionIn) -> GardenReviewActionOut:
    """Reject a generated note (ADR-0043): it is moved to the review bin,
    restorable, so a weak summary never enters the corpus; for a
    distillation, the source loses the humus mark unless another approved
    distillation still justifies it. A person's act: a credential carrying a
    scope list cannot call it. Audited, idempotent; ``expected_version`` as
    for approve."""
    note = await review_svc.reject_node(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        note_id=p.note_id,
        reason=p.reason,
        expected_version=p.expected_version,
    )
    return _action_out(note)


async def _on_decision(
    ctx: OpCtx, p: GardenReviewDecisionIn, *, approve: bool
) -> GardenReviewDecisionOut:
    by = await executor_identity(ctx)
    run = review_svc.approve_node_on_decision if approve else review_svc.reject_node_on_decision
    note = await run(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        note_id=p.note_id,
        expected_version=p.expected_version,
        decision_comment_id=p.decision_comment_id,
        executed_by_identity_id=by,
    )
    return GardenReviewDecisionOut(
        **_action_out(note).model_dump(), decision_comment_id=p.decision_comment_id
    )


_ON_DECISION_DOC = """{Verb} a generated note that a PERSON has decided to {verb}, on
    their behalf. You do not decide here: you carry out a decision.

    ``decision_comment_id`` is a comment the person wrote on one of the
    note's SOURCES (a proposed note cannot be commented on), after the note
    was generated, with a line ``{verb} <note id>`` (the full id or its first
    8 characters). The server checks that the person wrote it with their own
    login, not through an assistant, and otherwise refuses with
    ``garden.review_decision_invalid`` and a ``reason``. A message relayed to
    you, however clear, is not a decision: ask the person to write that
    comment. ``expected_version`` comes from the pending listing. The audit
    records you as the executor and the person as the one who decided."""


@operation(
    "garden.review.approve_on_decision",
    domain=GARDEN,
    kind=Kind.WRITE,
    access="notes:write",
    params=GardenReviewDecisionIn,
    result=GardenReviewDecisionOut,
)
async def review_approve_on_decision(
    ctx: OpCtx, p: GardenReviewDecisionIn
) -> GardenReviewDecisionOut:
    return await _on_decision(ctx, p, approve=True)


@operation(
    "garden.review.reject_on_decision",
    domain=GARDEN,
    kind=Kind.WRITE,
    access="notes:write",
    params=GardenReviewDecisionIn,
    result=GardenReviewDecisionOut,
)
async def review_reject_on_decision(
    ctx: OpCtx, p: GardenReviewDecisionIn
) -> GardenReviewDecisionOut:
    return await _on_decision(ctx, p, approve=False)


@operation(
    "garden.review.restore_source",
    domain=GARDEN,
    kind=Kind.WRITE,
    access="notes:write",
    params=GardenRestoreSourceIn,
    result=GardenRestoreSourceOut,
)
async def review_restore_source(ctx: OpCtx, p: GardenRestoreSourceIn) -> GardenRestoreSourceOut:
    """Restore the original under a humus atom: un-archive the atom's
    ``hypha_of`` sources (preserved, never changed) and retire the atom (an
    approved atom is demoted, then rejected). Nothing is hard-deleted and
    the provenance chain stays queryable. Idempotent."""
    res = await review_svc.restore_source(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        atom_note_id=p.note_id,
        reason=p.reason,
    )
    return GardenRestoreSourceOut(
        atom_note_id=res.atom_note_id,
        source_ids=res.source_ids,
        restored_source_ids=res.restored_source_ids,
        atom_retired=res.atom_retired,
    )


@operation(
    "garden.review.accept_ratio",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenNoArgs,
    result=list[GardenAcceptRatioOut],
)
async def review_accept_ratio(ctx: OpCtx, _p: GardenNoArgs) -> list[GardenAcceptRatioOut]:
    """How often a person approved, rather than rejected, each model's
    generated notes (ADR-0043 D4), most-decided model first. ``ratio`` is
    null when nothing was decided yet: no signal, not 0%."""
    rows = await review_svc.accept_ratio_by_model(ctx.session, org_id=ctx.org_id)
    return [
        GardenAcceptRatioOut(
            model_id=r.model_id, approved=r.approved, rejected=r.rejected, ratio=r.ratio
        )
        for r in rows
    ]


# --------------------------------------------------------------------------
# What the classifier learned from the caller (ADR-0037)
# --------------------------------------------------------------------------


def _delta_out(d: learning_svc.FeatureDelta) -> GardenFeatureDeltaOut:
    return GardenFeatureDeltaOut(
        feature_key=d.feature_key, before=d.before, after=d.after, delta=d.delta
    )


@operation(
    "garden.learning.telemetry",
    domain=GARDEN,
    kind=Kind.READ,
    access="notes:read",
    params=GardenTelemetryIn,
    result=GardenLearningTelemetryOut,
)
async def learning_telemetry(ctx: OpCtx, p: GardenTelemetryIn) -> GardenLearningTelemetryOut:
    """What the classifier learned from the caller (ADR-0037): the
    suggestion features they decline most (``reject_hotspots``, so they can
    be muted at the source) and the priors that moved most over
    ``drift_days`` (empty until a snapshot that old exists). The caller's own
    history only."""
    hotspots = await learning_svc.reject_hotspots(
        ctx.session, org_id=ctx.org_id, user_id=ctx.actor_id, days=p.reject_days, limit=p.limit
    )
    drift = await learning_svc.prior_drift(
        ctx.session, org_id=ctx.org_id, user_id=ctx.actor_id, days=p.drift_days, limit=p.limit
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


@operation(
    "garden.learning.rollback",
    domain=GARDEN,
    kind=Kind.WRITE,
    access="notes:write",
    params=GardenLearningRollbackIn,
    result=GardenLearningRollbackOut,
)
async def learning_rollback(ctx: OpCtx, p: GardenLearningRollbackIn) -> GardenLearningRollbackOut:
    """Rewind the caller's learned priors to their state at ``to`` (ADR-0037):
    the closest daily snapshot at or before it, plus the feedback since,
    written as a fresh checkpoint, so the rewind itself can be rewound. Only
    the caller's own priors; no note is touched. Returns the feature that
    moved most."""
    res = await learning_svc.rollback_priors(
        ctx.session, org_id=ctx.org_id, user_id=ctx.actor_id, to=p.to
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


review_approve_on_decision.__doc__ = _ON_DECISION_DOC.format(Verb="Approve", verb="approve")
review_reject_on_decision.__doc__ = _ON_DECISION_DOC.format(Verb="Reject", verb="reject")
