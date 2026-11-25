"""The garden's read and argument models (ADR-0068).

They were REST schemas in ``mycelium_api.schemas``; they live in core now so
the REST routes and the MCP tools that expose the same operation serve the
same model, and a field cannot reach one surface and not the other by
accident. The class names are unchanged, so the published OpenAPI document
and the web client's generated types keep their component names.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from mycelium_core.models.proposal_check import CheckVerdict, ClaimStatus

# --- Garden graph (note↔note weighted + PageRank centrality) -------
# Tasks 4467acb4 (note_edge_strength v1) + 8c0a8f08 (PageRank Phase 1).
# Both surfaces share one round-trip so the SPA mindmap doesn't have
# to coordinate two parallel fetches just to render one frame.


class GardenGraphEdge(BaseModel):
    """One undirected weighted edge between two notes. ``src`` and
    ``dst`` are canonically ordered (sorted by string repr) so two
    rows with the same endpoints never appear in different positions
    across requests. ``weight`` ∈ [0, 1] is the soft-OR of the
    per-kind contributions and the Adamic-Adar tag overlap; the
    third source (co-activity) is documented in ADR-0031 and
    deferred to Phase 2."""

    src: uuid.UUID
    dst: uuid.UUID
    weight: float


class GardenGraphOut(BaseModel):
    """Response of GET /garden/graph: edges + centrality in one
    payload. ``centrality`` is a ``{note_id: pagerank}`` map summing
    to 1 across the workspace; an empty workspace returns ``[]`` and
    ``{}`` respectively.

    Phase 2 (task d8664631): ``betweenness`` is the cluster-bridge
    centrality served from the worker-materialised snapshot (empty
    until the first refresh; ``analytics_computed_at`` is its age).
    ``recency`` is the separate freshness axis (``exp(-age/tau)`` per
    note, computed live) consumers combine with centrality to counter
    the cold start of new, not-yet-linked notes."""

    edges: list[GardenGraphEdge]
    centrality: dict[uuid.UUID, float]
    betweenness: dict[uuid.UUID, float] = Field(default_factory=dict)
    recency: dict[uuid.UUID, float] = Field(default_factory=dict)
    analytics_computed_at: datetime.datetime | None = None


class GardenClustersOut(BaseModel):
    """Response of GET /garden/clusters (task 8c0a8f08): the Leiden
    community index per note plus the partition's global modularity.
    ``clusters`` is ``{note_id: community_index}`` (0-based, dense);
    ``modularity`` is the structure thermometer (ADR-0035), or null when
    the optional clustering extra is not installed (graceful degrade).
    ``count`` is the number of distinct communities."""

    clusters: dict[uuid.UUID, int]
    modularity: float | None
    count: int


# --- Garden walk (task 5bf31b63) ----------------------------------------
# Two regimes packed in one shape: ``focused`` (PPR seeded) returns
# the top-K notes by induced mass and the step is their rank; ``free_
# wander`` (Node2Vec) returns the actual trajectory and the step is
# the hop index. The SPA renders them as the "pollinator trail" with
# the same component.


class GardenWalkStep(BaseModel):
    note_id: uuid.UUID
    step: int
    weight: float
    # Provenance marker (ADR-0034): "humus" when the node is a flagged
    # humus note (the free wander biases toward high-centrality humus),
    # else None. The mindmap renders a leaf marker on humus steps.
    provenance: str | None = None


class GardenWalkOut(BaseModel):
    seed: uuid.UUID
    mode: str  # "focused" | "free_wander"
    steps: list[GardenWalkStep]


# --- Link suggestions (task c7d0bb4c) --------------------------------
# Returned by GET /garden/link-suggestions/{note_id}. ``score`` ∈
# [0, 1], soft-OR of Adamic-Adar (rare-tag overlap) and PPR-induced
# mass, damped by candidate degree to avoid hub bias. ``signals``
# carries the per-feature contribution so the SPA can render a
# tooltip and ADR-0037's audit log can persist the breakdown.


class GardenLinkSuggestion(BaseModel):
    note_id: uuid.UUID
    # The MCP tool served the title and the REST route did not, so a reader
    # of the REST answer needed a lookup per row; one model now serves both.
    title: str | None = None
    score: float
    rationale: str
    signals: dict[str, float]


class GardenLinkSuggestionsOut(BaseModel):
    source_note_id: uuid.UUID
    suggestions: list[GardenLinkSuggestion]


# --- garden_classify proposal engine (ADR-0032) ----------------------
# classify is read-only: it proposes {tags, links, maturity, cluster}
# each with a confidence + rationale; ``signals_used`` names the signals
# that fired (transparency). ``apply`` is the mutating, reversible
# counterpart that records a classification_feedback event (ADR-0037).


class GardenTagSuggestionOut(BaseModel):
    tag_id: uuid.UUID
    confidence: float
    rationale: str


class GardenLinkCandidateOut(BaseModel):
    target_id: uuid.UUID
    link_kind: str
    confidence: float
    rationale: str


class GardenMaturitySuggestionOut(BaseModel):
    value: str  # "mature" in v1 (the value axis only proposes upward)
    confidence: float
    rationale: str
    auto_apply: bool


class GardenClusterSuggestionOut(BaseModel):
    leiden_id: int | None
    modularity: float | None
    confidence: float


class GardenClassifyOut(BaseModel):
    """Response of GET /garden/classify/{node_id} (ADR-0032 / ADR-0042). A
    block is null/empty when its signal was not requested or produced nothing;
    ``signals_used`` names the signals that actually fired (and records
    ``leiden_extra_absent`` when clustering degraded gracefully).

    ``source`` (ADR-0042 D4/D6) tells the SPA whether these are the persisted
    on-create suggestions (``precomputed``) or a fresh live recompute
    (``live``); ``generated_at`` dates them so the panel can show freshness and
    offer a refresh."""

    node_id: uuid.UUID
    node_kind: str
    tags: list[GardenTagSuggestionOut]
    links: list[GardenLinkCandidateOut]
    maturity: GardenMaturitySuggestionOut | None
    cluster: GardenClusterSuggestionOut | None
    signals_used: list[str]
    model_version: str
    generated_at: datetime.datetime
    source: Literal["precomputed", "live"] = "live"


class GardenHealthMetricOut(BaseModel):
    """One sensor reading (ADR-0035, "show, never judge"): the value, its
    health floor when it has one, and -- only when ``value`` is null --
    the reason there is no reading yet (data source not built / blocked
    upstream), never a faked number."""

    value: float | None = None
    floor: float | None = None
    reason: str | None = None


class GardenHealthSnapshotOut(BaseModel):
    day: datetime.date
    metrics: dict[str, GardenHealthMetricOut]


class GardenHealthOut(BaseModel):
    """Response of GET /garden/health: current sensor readings plus the
    recent daily snapshots (newest first) for the sparkline."""

    generated_at: datetime.datetime
    metrics: dict[str, GardenHealthMetricOut]
    trend: list[GardenHealthSnapshotOut]


class GardenHealthEventOut(BaseModel):
    """One entry of the "what changed" timeline (ADR-0035 §84): a factual
    record correlating a sensor shift with a cause -- a classifier bump
    or a bulk corpus edit. ``detail`` is a small kind-specific bag:
    ``{"version": ...}`` for ``classifier_version``; ``{"action",
    "count"}`` for ``corpus_edit``. "Show, never judge": it states that
    something happened, never whether it was good."""

    at: datetime.datetime
    kind: Literal["classifier_version", "corpus_edit"]
    detail: dict[str, Any]


class GardenEventOut(BaseModel):
    """One ``event_outbox`` row on the workspace event stream (ADR-0036
    audit panel): the verbatim coordinated read/propose/commit/reject/
    snapshot event. ``applied_state`` is null until the adjudicator decides
    a propose. "Show, never judge": the event, not a verdict."""

    id: uuid.UUID
    actor_id: uuid.UUID
    actor_kind: Literal["human", "agent", "system"]
    kind: Literal["read", "propose", "commit", "reject", "snapshot"]
    node_kind: str | None = None
    node_id: uuid.UUID | None = None
    parent_event_id: uuid.UUID | None = None
    payload: dict[str, Any]
    ts: datetime.datetime
    applied_at: datetime.datetime | None = None
    applied_state: str | None = None


class GardenApplyIn(BaseModel):
    """Apply (or decline) one suggestion. ``accept``/``override`` mutate
    via the existing services; ``reject``/``ignore`` only record the
    decision. ``auto`` is reserved for the worker and is not accepted on
    this surface (a client cannot forge a system promotion)."""

    node_id: uuid.UUID
    suggestion_type: Literal["tag", "link", "maturity", "cluster"]
    suggestion_value: dict[str, Any]
    action: Literal["accept", "reject", "override", "ignore"]
    override_value: dict[str, Any] | None = None
    model_version: str | None = None
    signals_snapshot: dict[str, Any] | None = None


class GardenApplyOut(BaseModel):
    feedback_id: uuid.UUID
    node_id: uuid.UUID
    suggestion_type: str
    action: str
    applied: bool  # True when the action mutated (accept / override)


class GardenReviewPendingItem(BaseModel):
    """One AUTONOMOUSLY-generated humus note awaiting human review (ADR-0043
    review inbox). ``origin_model_id`` is the transparency requirement: the
    reviewer sees the producing model before approving/rejecting."""

    note_id: uuid.UUID
    title: str | None = None
    humus_kind: str | None = None
    origin_model_id: str | None = None
    preview: str
    created_at: datetime.datetime
    # Echo this back as ``expected_version`` on approve/reject: the TOCTOU
    # guard that ensures the reviewer blesses the content they saw.
    version: int


class GardenReviewRejectedItem(BaseModel):
    """One proposal a person DECLINED (ADR-0075): kept, readable, and
    reversible. ``rejected_at`` is when it was declined; ``version`` is the
    pin to echo back to a reopen, an approval or a removal."""

    note_id: uuid.UUID
    title: str | None = None
    humus_kind: str | None = None
    origin_model_id: str | None = None
    preview: str
    created_at: datetime.datetime
    # Echo this back as ``expected_version`` on a reopen, an approval or a
    # removal.
    version: int
    rejected_at: datetime.datetime


class GardenCandidateNode(BaseModel):
    """A NODE distillation candidate (task 4995a32f): inert material that
    could be compacted into a denser atom. ``kind`` is distill|pattern|
    season; ``note_ids`` are the source(s) to feed to the matching
    decomposition tool."""

    kind: str
    note_ids: list[uuid.UUID]
    title: str
    reason: str
    score: float
    preview: str


class GardenCandidateEdge(BaseModel):
    """An EDGE curation candidate (task 4995a32f): distillation as graph
    maintenance. ``op`` is add (a strong tag/co-activity pair with no
    manual link) or prune (a ``related`` link whose basis has decayed)."""

    op: str
    src_note_id: uuid.UUID
    dst_note_id: uuid.UUID
    link_kind: str
    src_title: str
    dst_title: str
    reason: str
    score: float


class GardenCandidatesOut(BaseModel):
    """Distillation candidates: nodes to compact + edges to add/prune.
    Pure read; nothing is distilled until the caller acts on a candidate."""

    nodes: list[GardenCandidateNode]
    edges: list[GardenCandidateEdge]


class GardenReviewActionIn(BaseModel):
    """Approve, or reject, one proposed node by id (ADR-0043). ``reason`` is
    an optional note recorded on a reject (ignored by approve).
    ``expected_version`` is the version the reviewer READ (served by the
    pending listing): when set, the action fails with ``stale_version`` if
    the node changed in between (TOCTOU guard, task 2e36e732)."""

    note_id: uuid.UUID
    reason: str | None = None
    expected_version: int | None = None


class GardenReviewActionOut(BaseModel):
    note_id: uuid.UUID
    review_state: str | None  # 'approved', 'declined' or 'proposed' after the action
    origin_model_id: str | None = None
    rejected: bool  # True when the node is declined after the action
    version: int  # post-action version (callers can chain guarded actions)


class GardenReviewNodeIn(BaseModel):
    """One generated note, by id."""

    note_id: uuid.UUID


class GardenReviewGuardedIn(BaseModel):
    """One generated note, with the version the person read. Required, not
    optional: reopening or removing acts on what was read."""

    note_id: uuid.UUID
    expected_version: int


class GardenReviewSourceOut(BaseModel):
    """A note a proposal was generated from. ``body_truncated`` says the body
    is the opening only, ``body_chars`` how long the whole is; read the rest
    with garden_review_read_source."""

    note_id: uuid.UUID
    title: str | None = None
    body: str
    is_archived: bool
    body_chars: int
    body_truncated: bool = False


class GardenReviewReadIn(BaseModel):
    """``full_text`` serves the sources whole, for a person reading in the
    app. Leave it false from an agent: the sources then share a budget, and
    each says whether it was cut."""

    note_id: uuid.UUID
    full_text: bool = False


class GardenReviewSourceIn(BaseModel):
    note_id: uuid.UUID = Field(description="The proposal.")
    source_id: uuid.UUID = Field(description="One of its sources.")
    offset: int = Field(0, ge=0)
    limit: int = Field(20000, ge=1, le=20000)


class GardenReviewSourceSliceOut(BaseModel):
    source_id: uuid.UUID
    offset: int
    text: str
    total_chars: int
    next_offset: int | None = None


class GardenReviewFindingIn(BaseModel):
    """One claim of the proposal, read against its sources. ``status``:
    ``supported`` (the source says it), ``unsupported`` (the source does not
    say it), ``contradicted`` (the source says otherwise), ``unclear``.
    ``evidence`` is the passage of the source the status rests on."""

    claim: str = Field(min_length=1, max_length=2000)
    status: ClaimStatus
    evidence: str = Field("", max_length=2000)
    comment: str = Field("", max_length=2000)


class GardenReviewCheckIn(BaseModel):
    """Attach a check to a generated note (ADR-0076). ``verdict`` is
    ``supported`` when every finding is, ``issues`` otherwise; the server
    refuses a verdict that disagrees with its findings. ``expected_version``
    is the version you read with garden_review_read."""

    note_id: uuid.UUID
    expected_version: int
    verdict: CheckVerdict
    summary: str = Field(min_length=1, max_length=2000)
    findings: list[GardenReviewFindingIn] = Field(min_length=1, max_length=50)
    checker_model_id: str | None = Field(None, max_length=128)


class GardenReviewCheckOut(BaseModel):
    check_id: uuid.UUID
    verdict: str
    summary: str
    findings: list[GardenReviewFindingIn]
    checker_identity_id: uuid.UUID | None = None
    checker_model_id: str | None = None
    note_version: int
    stale: bool
    created_at: datetime.datetime


class GardenReviewReviseIn(BaseModel):
    """Replace a pending or declined generated note with a better version
    (ADR-0076). ``text`` is the whole revised text; ``origin_model_id`` the
    model that wrote it. ``expected_version`` from garden_review_read."""

    note_id: uuid.UUID
    expected_version: int
    text: str = Field(min_length=1, max_length=20000)
    origin_model_id: str | None = Field(None, max_length=96)


class GardenReviewReviseOut(BaseModel):
    revision_note_id: uuid.UUID
    revises_note_id: uuid.UUID
    review_state: str
    version: int


class GardenReviewRevisionRef(BaseModel):
    note_id: uuid.UUID
    title: str | None = None
    origin_model_id: str | None = None
    review_state: str | None = None
    body: str | None = None


class GardenReviewReadingOut(BaseModel):
    """A generated note read in full by the person deciding it (ADR-0075),
    with its sources. ``review_state`` is 'proposed', 'declined' or
    'approved'; ``decided_at`` is when it was last approved or declined."""

    note_id: uuid.UUID
    title: str | None = None
    body: str
    humus_kind: str | None = None
    origin_model_id: str | None = None
    review_state: str
    created_at: datetime.datetime
    decided_at: datetime.datetime | None = None
    version: int
    sources: list[GardenReviewSourceOut]
    checks: list[GardenReviewCheckOut] = []
    revises: GardenReviewRevisionRef | None = None
    revised_by: list[GardenReviewRevisionRef] = []


class GardenReviewForSourceItem(BaseModel):
    """A note generated from a given source, with where it stands."""

    note_id: uuid.UUID
    title: str | None = None
    humus_kind: str | None = None
    review_state: str
    created_at: datetime.datetime
    decided_at: datetime.datetime | None = None


class GardenReviewRemoveOut(BaseModel):
    note_id: uuid.UUID
    removed: bool


class GardenRestoreSourceOut(BaseModel):
    """Fase P (task 561c6aca): outcome of "ripristina originale" on a humus
    atom -- the sources revived and the atom retired (the ``hypha_of`` chain
    is the stack; nothing is ever hard-deleted)."""

    atom_note_id: uuid.UUID
    source_ids: list[uuid.UUID]
    restored_source_ids: list[uuid.UUID]
    atom_retired: bool


class GardenAcceptRatioOut(BaseModel):
    """Per-model reliability of AUTONOMOUSLY-generated proposals (ADR-0043
    D4): how often a human approved vs rejected this model's output. ``ratio``
    is null when there are no decisions yet (no signal, not 0%)."""

    model_id: str
    approved: int
    rejected: int
    ratio: float | None = None


class GardenLearningRollbackIn(BaseModel):
    """Rewind the caller's own learned priors to their state at ``to``
    (ADR-0037 "Snapshots and rollback")."""

    to: datetime.datetime


class GardenFeatureDeltaOut(BaseModel):
    """One feature's prior value before vs after (rollback diff / drift)."""

    feature_key: str
    before: float
    after: float
    delta: float


class GardenLearningRollbackOut(BaseModel):
    """Result of a rollback: the restored window + a one-line diff
    (ADR-0037: "the system was slightly more biased toward tag X")."""

    rolled_back_to: datetime.datetime
    snapshot_at: datetime.datetime | None
    replayed_events: int
    features_changed: int
    top_change: GardenFeatureDeltaOut | None
    summary: str


class GardenRejectHotspotOut(BaseModel):
    """A suggestion feature the caller keeps declining (ADR-0037 reject-
    hotspot view). ``feature_key`` is type-prefixed; the SPA resolves the
    human label from its tag/note context."""

    suggestion_type: str
    feature_key: str
    declines: int
    last_declined_at: datetime.datetime


class GardenLearningTelemetryOut(BaseModel):
    """Read-only learning telemetry for the sensors dashboard (ADR-0037):
    the reject-hotspots and the prior-drift bars, the caller's own history
    only. "Show, never judge"."""

    reject_hotspots: list[GardenRejectHotspotOut]
    drift: list[GardenFeatureDeltaOut]


# --- Arguments of the operations that had none as a model (ADR-0068) ---
# A REST route used to declare these as individual query parameters and the
# MCP tool as a function signature, each with its own bounds. One model now
# carries the bounds for both.


class GardenNoArgs(BaseModel):
    """An operation that takes no arguments."""


class GardenDaysIn(BaseModel):
    days: int = Field(90, ge=1, le=365, description="How many days back, newest first.")


class GardenEventsIn(BaseModel):
    limit: int = Field(100, ge=1, le=500)
    days: int | None = Field(None, ge=1, le=365, description="Only events from the last N days.")
    since: datetime.datetime | None = Field(
        None,
        description=(
            "ISO-8601 cursor: events at or after it (a timestamp without a zone is "
            "read as UTC). Delivery is at-least-once, so dedupe by id. Takes "
            "precedence over days."
        ),
    )


class GardenListIn(BaseModel):
    limit: int = Field(50, ge=1, le=200)


class GardenClassifyIn(BaseModel):
    node_id: uuid.UUID
    kinds: str | None = Field(
        None, description="CSV subset of tags,links,maturity,cluster (default: all)"
    )
    refresh: bool = Field(
        False, description="Bypass the persisted cache and recompute live (ADR-0042 D6)."
    )


class GardenCandidatesIn(BaseModel):
    kind: Literal[
        "all", "distill", "pattern", "season", "link_add", "link_prune", "link_direct"
    ] = "all"
    limit: int = Field(50, ge=1, le=200)


class GardenRestoreSourceIn(BaseModel):
    """Restore the sources of one humus atom. ``reason`` is recorded on the
    atom's retirement."""

    note_id: uuid.UUID
    reason: str | None = None


class GardenReviewDecisionIn(BaseModel):
    """Carry out a review a person wrote down (ADR-0043, 2026-11-12
    amendment). ``decision_comment_id`` is the person's comment on one of
    the note's SOURCES with a line ``approve <id>`` or ``reject <id>``;
    ``expected_version`` is the version the pending listing served."""

    note_id: uuid.UUID
    expected_version: int
    decision_comment_id: uuid.UUID


class GardenReviewDecisionOut(GardenReviewActionOut):
    decision_comment_id: uuid.UUID


class GardenTelemetryIn(BaseModel):
    reject_days: int = Field(90, ge=1, le=365)
    drift_days: int = Field(30, ge=1, le=365)
    limit: int = Field(10, ge=1, le=50)


class GardenWalkIn(BaseModel):
    seed: uuid.UUID = Field(description="Note id to seed the walk on")
    mode: Literal["focused", "free_wander", "bounded"] = Field(
        "focused",
        description=(
            "focused = PPR seeded; free_wander = Node2Vec walk; "
            "bounded = best-first thresholded (size-independent)"
        ),
    )
    budget: int = Field(24, ge=1, le=200)
    p: float = Field(1.0, gt=0.0, le=10.0)
    q: float = Field(1.0, gt=0.0, le=10.0)
    seed_rng: int | None = Field(None, ge=0)


class GardenReadingSetIn(BaseModel):
    seed: uuid.UUID
    mode: Literal["focused", "free_wander", "bounded"] = "focused"
    budget: int = Field(24, ge=1, le=200)


class GardenReadingStep(BaseModel):
    note_id: uuid.UUID
    step: int
    weight: float
    title: str | None = None
    snippet: str | None = None
    provenance: str | None = None


class GardenLinkSuggestionsIn(BaseModel):
    note_id: uuid.UUID
    k: int = Field(5, ge=1, le=20)


# --- Written references (ADR-0073) -----------------------------------


class GardenWrittenBacklinksIn(BaseModel):
    target_kind: Literal["note", "task", "tag"] = Field(
        description="What the backlinks point at: a note, a task or a tag."
    )
    target_id: uuid.UUID


class GardenWrittenBacklink(BaseModel):
    """One place where a person wrote a reference to the target. ``status``
    is ``resolved``, or ``ambiguous`` when the written code also names other
    entities and nobody has chosen; ``reference_id`` is what a person passes
    to choose. ``quote_prefix`` + ``quote_exact`` + ``quote_suffix`` is the
    sentence as a reader sees it. ``also_typed`` says whether the two ends
    also hold a typed link. ``first_seen_at`` is null for references found by
    the backfill, whose writing time is not known."""

    reference_id: uuid.UUID
    status: Literal["resolved", "ambiguous"]
    syntax: Literal["mention", "chip", "bare"]
    source_kind: Literal["note_part", "task"]
    note_id: uuid.UUID | None = None
    part_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    title: str | None = None
    source_archived: bool = False
    quote_exact: str | None = None
    quote_prefix: str | None = None
    quote_suffix: str | None = None
    also_typed: bool = False
    first_seen_at: datetime.datetime | None = None


class GardenWrittenBacklinksOut(BaseModel):
    backlinks: list[GardenWrittenBacklink]


class GardenReferenceChooseIn(BaseModel):
    reference_id: uuid.UUID
    target_kind: Literal["note", "task"]
    target_id: uuid.UUID = Field(
        description="The entity the person means; its id must start with the written code."
    )


class GardenReferenceChooseOut(BaseModel):
    reference_id: uuid.UUID
    status: Literal["resolved"]
    target_kind: Literal["note", "task"]
    target_id: uuid.UUID


# --- Link proposals (ADR-0073 decision 7) ----------------------------


class GardenProposalSubmitIn(BaseModel):
    source_note_id: uuid.UUID
    target_note_id: uuid.UUID
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Why you propose it, shown to the person: a quote, a reason.",
    )
    evidence_ref: str | None = Field(
        None,
        max_length=2000,
        description="A reference into your own evidence store, kept on the link if accepted.",
    )
    score: float | None = Field(
        None, description="Orders the queue among your proposals. Never shown to the person."
    )


class GardenProposalSubmitOut(BaseModel):
    """``proposal_id`` when a proposal is open for the pair; otherwise
    ``skipped``: ``rejected`` (a person rejected this pair), ``linked``
    (already linked), ``not_effective`` (a note is in the bin or awaiting
    review), ``bound`` (you hold the most open proposals allowed on one of
    these notes), ``self``."""

    proposal_id: uuid.UUID | None = None
    skipped: str | None = None


class GardenProposalQueueIn(BaseModel):
    note_id: uuid.UUID | None = Field(None, description="Only proposals touching this note.")
    limit: int = Field(20, ge=1, le=100)


class GardenProposalItem(BaseModel):
    proposal_id: uuid.UUID
    proposer: str
    source_note_id: uuid.UUID
    target_note_id: uuid.UUID
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime.datetime


class GardenProposalQueueOut(BaseModel):
    proposals: list[GardenProposalItem]


class GardenProposalShownIn(BaseModel):
    proposal_ids: list[uuid.UUID] = Field(max_length=100, description="In display order.")
    surface: str = Field(max_length=40, description="Where they were shown, e.g. note_panel.")


class GardenProposalShownOut(BaseModel):
    recorded: int


class GardenProposalAcceptIn(BaseModel):
    proposal_id: uuid.UUID
    kind: Literal["hypha_of", "related", "supersedes", "contradicts"]
    swap: bool = Field(
        False, description="Make the target the parent instead of the source (directed kinds)."
    )


class GardenProposalAcceptOut(BaseModel):
    link_id: uuid.UUID
    parent_note_id: uuid.UUID
    child_note_id: uuid.UUID
    kind: str
    origin_proposer: str


class GardenProposalRejectIn(BaseModel):
    proposal_id: uuid.UUID


class GardenProposalRejectOut(BaseModel):
    proposal_id: uuid.UUID
    status: Literal["rejected"]


class GardenProposerPrecision(BaseModel):
    """Accepted over decided, with its 95% Wilson interval. Expired
    proposals are not decisions and are reported apart."""

    proposer: str
    accepted: int
    rejected: int
    expired: int
    open: int
    shown: int
    precision: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None


class GardenProposerPrecisionOut(BaseModel):
    proposers: list[GardenProposerPrecision]
