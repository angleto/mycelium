"""``garden_classify(node_id)`` — the proposal engine (ADR-0032).

Read-only. Composes the shipped graph + link-prediction + Leiden
substrate into a single structured proposal ``{tags, links, maturity,
cluster}`` for a node, each item carrying a confidence and a rationale
(the *transparency* constraint). It **never mutates**; the mutating,
reversible counterpart is ``garden_apply`` (a separate surface, ADR-0032).

v1 signals (heuristic, on today's substrate):

- **tags** — tag↔tag co-occurrence over the workspace, discounted by tag
  rarity (Adamic-Adar denominator ``1 / log(2 + deg(c))``: a candidate
  that already covers half the garden contributes less to its own
  confidence than a rare one would — ADR-0033 M1, native here).
- **links** — wraps ``link_prediction.suggest_links_for_note`` (Adamic-
  Adar + PPR + hub damping). v1 leaves ``link_kind`` at the conservative
  default ``references``; the kind-predicting MLP is v2.
- **maturity** — the *value* axis of the garden lifecycle: a note becomes
  a ``mature`` candidate from global PageRank percentile (centrality) AND
  manual-link degree (human curation). The *freshness* axis
  (``seed→growing→dormant``) stays in the worker tick; this answers a
  different question (see ADR-0032 "Relation to structural humus").
- **cluster** — ``graph.compute_leiden_clusters``; degrades to ``None``
  (recorded in ``signals_used`` as ``leiden_extra_absent``) when the
  optional ``clustering`` extra is not installed, never a silent empty.

Deferred to v2 (ADR-0032): bge-m3 embed-NN tags, the learned link-kind
model, structural Node2Vec, calibrated personal priors (ADR-0037) and the
anti-monoculture post-processing wrapper (ADR-0033). The confidence here
is a fixed, documented transform of the raw signal, not yet learned.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.config import get_settings
from flow_core.errors import ConflictError, DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.classification_feedback import (
    FEEDBACK_ACTIONS,
    SUGGESTION_TYPES,
    ClassificationFeedback,
)
from flow_core.models.classification_job import ClassificationJob
from flow_core.models.membership import Role
from flow_core.models.note import Note
from flow_core.models.note_link import NoteNoteLink
from flow_core.models.note_tag import NoteTag
from flow_core.models.precomputed_suggestion import PrecomputedSuggestion
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task import Task
from flow_core.models.task_tag import TaskTag
from flow_core.services import (
    audit,
    event_bus,
    garden_learning,
    graph_snapshot,
    note_inert,
    note_links,
)
from flow_core.services import graph as graph_svc
from flow_core.services import link_prediction as linkpred_svc
from flow_core.services import notes as notes_svc
from flow_core.services.rbac import require_role

MODEL_VERSION = "garden-classify-v1"

# Confidence floors / tiers (ADR-0032). Conservative by design: the safe
# failure mode for a system that can promote maturity automatically is to
# under-suggest. Tunable per workspace later; constants for v1.
TAG_FLOOR = 0.55
LINK_FLOOR = 0.45
MATURE_SUGGEST = 0.65  # surface a one-tap proposal at/above this
MATURE_AUTO = 0.85  # auto-promote (reversible, label-only) at/above this
DEG_SATURATION = 3  # ADR-0029 manual-link threshold for "mature"
DEFAULT_K = 5

# Cold-start damping (WS-D5, ADR-0032). Below this many notes the graph
# signals degenerate -- Leiden collapses to one community, PageRank /
# percentile are near-uniform, co-occurrence support is thin -- so a
# fixed-floor heuristic emits a "confident" but empty suggestion (E3:
# leiden_id 0, tags []) that teaches the forester to ignore the panel.
# A linear ramp damps every confidence toward 0 on a sparse corpus so weak
# signals fall back under their floors instead of masquerading as certainty.
COLD_START_NODES = 20

# The set of suggestion kinds the engine can produce.
ALL_KINDS: frozenset[str] = frozenset({"tags", "links", "maturity", "cluster"})


def _neutral_prior(_: uuid.UUID) -> float:
    """The identity re-rank factor: no personalisation (no user, or the user
    has no prior on this feature). Keeps ``classify_node`` structural-only and
    bit-for-bit unchanged when called without a ``user_id``."""
    return 1.0


def _factor_lookup(priors: dict[str, float], prefix: str) -> Callable[[uuid.UUID], float]:
    """Build the ``prior_of`` callable for a suggestion surface: map an
    entity id to its ADR-0037 re-rank factor ``exp(prior)`` (1.0 when absent)."""

    def _of(entity_id: uuid.UUID) -> float:
        return garden_learning.prior_factor(priors.get(f"{prefix}{entity_id}", 0.0))

    return _of


@dataclass(frozen=True)
class TagSuggestion:
    tag_id: uuid.UUID
    confidence: float
    rationale: str


@dataclass(frozen=True)
class LinkCandidate:
    target_id: uuid.UUID
    link_kind: str
    confidence: float
    rationale: str


@dataclass(frozen=True)
class MaturitySuggestion:
    value: str  # "mature" in v1 (the value axis only proposes upward)
    confidence: float
    rationale: str
    auto_apply: bool


@dataclass(frozen=True)
class ClusterSuggestion:
    leiden_id: int | None
    modularity: float | None
    confidence: float


@dataclass(frozen=True)
class ClassifyResult:
    node_id: uuid.UUID
    node_kind: str
    tags: list[TagSuggestion]
    links: list[LinkCandidate]
    maturity: MaturitySuggestion | None
    cluster: ClusterSuggestion | None
    signals_used: list[str]
    model_version: str
    generated_at: dt.datetime
    # Diagnostics never surfaced to the user but useful in tests / audit.
    raw: dict[str, float] = field(default_factory=dict)


async def _note_generic_tags(
    session: AsyncSession, *, org_id: uuid.UUID, include_tasks: bool = False
) -> tuple[dict[uuid.UUID, set[uuid.UUID]], dict[uuid.UUID, int]]:
    """``({node_id: {generic_tag_id}}, {generic_tag_id: degree})`` in one
    batched pass. Restricted to ``kind='generic'`` for the same reason as
    ``graph._generic_tag_degrees``: project/client tags are coarse buckets
    that every node shares, so they carry no discriminative signal.

    ``include_tasks`` (ADR-0042, gated by ``garden_unified_task_graph_enabled``)
    spans ``task_tags`` too, so the co-occurrence corpus is the unified
    foresta (notes and tasks); note and task ids never collide (distinct
    UUIDs). Default false keeps the notes-only corpus byte-identical."""
    by_node: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    deg: dict[uuid.UUID, int] = defaultdict(int)
    note_rows = (
        await session.execute(
            select(NoteTag.note_id, NoteTag.tag_id, Tag.kind)
            .join(Tag, Tag.id == NoteTag.tag_id)
            .where(NoteTag.org_id == org_id)
        )
    ).all()
    for node_id, tag_id, kind in note_rows:
        if kind is TagKind.generic:
            by_node[node_id].add(tag_id)
            deg[tag_id] += 1
    if include_tasks:
        task_rows = (
            await session.execute(
                select(TaskTag.task_id, TaskTag.tag_id, Tag.kind)
                .join(Tag, Tag.id == TaskTag.tag_id)
                .where(TaskTag.org_id == org_id)
            )
        ).all()
        for node_id, tag_id, kind in task_rows:
            if kind is TagKind.generic:
                by_node[node_id].add(tag_id)
                deg[tag_id] += 1
    return by_node, deg


def _suggest_tags(
    *,
    node_id: uuid.UUID,
    by_note: dict[uuid.UUID, set[uuid.UUID]],
    tag_deg: dict[uuid.UUID, int],
    k: int,
    damping: float = 1.0,
    prior_of: Callable[[uuid.UUID], float] = _neutral_prior,
) -> list[TagSuggestion]:
    """Candidate generic tags ranked by rarity-discounted co-occurrence
    with the node's existing generic tags. A tag found on many notes that
    share a (rare) tag with the node scores high; a near-ubiquitous tag is
    damped by ``1 / log(2 + deg)``.

    ``prior_of`` is the per-user re-rank factor (ADR-0037): it multiplies the
    *structural* confidence to order/display candidates, but the floor is
    checked on the structural value BEFORE the prior, so a strongly negative
    prior never prunes a structurally-valid tag (it only sinks it)."""
    node_tags = by_note.get(node_id, set())
    if not node_tags:
        return []
    support: dict[uuid.UUID, int] = defaultdict(int)
    for other_id, other_tags in by_note.items():
        if other_id == node_id or not (other_tags & node_tags):
            continue
        for t in other_tags:
            if t not in node_tags:
                support[t] += 1
    scored: list[tuple[float, TagSuggestion]] = []
    for c, n in support.items():
        raw = n / math.log(2.0 + tag_deg.get(c, 0))
        conf = (1.0 - 1.0 / (1.0 + raw)) * damping  # squash to [0, 1), cold-start damped
        if conf < TAG_FLOOR:  # floor on the STRUCTURAL confidence (pre-prior)
            continue
        ranked = conf * prior_of(c)  # personalised re-rank score (uncapped)
        scored.append(
            (
                ranked,
                TagSuggestion(
                    tag_id=c,
                    confidence=min(1.0, ranked),
                    rationale=(
                        f"co-occurs on {n} related note(s); rarity 1/log(2+{tag_deg.get(c, 0)})"
                    ),
                ),
            )
        )
    scored.sort(key=lambda t: (-t[0], str(t[1].tag_id)))
    return [s for _, s in scored[: max(0, k)]]


async def _suggest_links(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    k: int,
    damping: float = 1.0,
    prior_of: Callable[[uuid.UUID], float] = _neutral_prior,
) -> list[LinkCandidate]:
    """Link candidates from ``link_prediction``, re-ranked by the user's
    per-target prior (ADR-0037). Floor on the structural score (pre-prior),
    so a muted target sinks but is never pruned below a structural pass."""
    rows = await linkpred_svc.suggest_links_for_note(session, org_id=org_id, note_id=node_id, k=k)
    scored: list[tuple[float, LinkCandidate]] = []
    for r in rows:
        conf = r.score * damping  # structural
        if conf < LINK_FLOOR:
            continue
        ranked = conf * prior_of(r.note_id)
        scored.append(
            (
                ranked,
                LinkCandidate(
                    target_id=r.note_id,
                    link_kind="related",  # v1 default; kind MLP is v2 (ADR-0032)
                    confidence=min(1.0, ranked),
                    rationale=r.rationale,
                ),
            )
        )
    scored.sort(key=lambda t: (-t[0], str(t[1].target_id)))
    return [c for _, c in scored]


def _pr_percentile(pageranks: dict[uuid.UUID, float], node_id: uuid.UUID) -> float:
    """Rank percentile of ``node_id`` in the workspace PageRank
    distribution, in [0, 1]. The unique maximum scores 1.0 regardless of
    the raw value (rank-based, so the maturity tiers are stable across
    workspaces); 0.0 for an empty/singleton workspace. Shared by the
    interactive ``_suggest_maturity`` and the batch ``auto_promote_mature``
    so the two never diverge on the policy."""
    n = len(pageranks)
    if n <= 1:
        return 0.0
    node_pr = pageranks.get(node_id, 0.0)
    rank = sum(1 for v in pageranks.values() if v <= node_pr)
    return (rank - 1) / (n - 1)


def _cold_start_damping(node_count: int) -> float:
    """Confidence multiplier in [0, 1]: full at ``COLD_START_NODES`` notes or
    more, a linear ramp below (0 for an empty corpus). Applied to every
    suggestion's confidence before its floor, so a sparse corpus -- where the
    graph statistics are unreliable -- yields fewer/no over-confident
    suggestions rather than degenerate ones (WS-D5)."""
    if node_count >= COLD_START_NODES:
        return 1.0
    return max(0.0, node_count / COLD_START_NODES)


async def _suggest_maturity(
    session: AsyncSession, *, org_id: uuid.UUID, note: Note, damping: float = 1.0
) -> tuple[MaturitySuggestion | None, dict[str, float]]:
    """The value-axis promotion ``growing → mature``.

    ``conf_mature = min(pr_pct, deg_term)`` — the ``min`` is the AND
    semantics of ADR-0032: a note must be *both* central (top of the
    PageRank distribution) *and* humanly curated (≥3 manual links) to be
    auto-matured. A hub with no manual links, or a heavily linked private
    silo, lands in the proposal tier at most, never auto.
    """
    diag: dict[str, float] = {}
    # Only the value-axis promotion from ``growing`` is in scope; the
    # worker owns seed→growing→dormant, and a transplanted note is
    # read-only (ADR-0029 D2).
    if note.promoted_at is not None or note.maturity != "growing":
        return None, diag

    pageranks = await graph_svc.compute_pagerank(session, org_id=org_id)
    pr_pct = _pr_percentile(pageranks, note.id)

    degree = await _manual_link_degree(session, org_id=org_id, note_id=note.id)
    deg_term = min(1.0, degree / DEG_SATURATION)
    conf = min(pr_pct, deg_term) * damping
    diag.update(pr_pct=pr_pct, manual_degree=float(degree), conf_mature=conf)

    if conf < MATURE_SUGGEST:
        return None, diag
    return (
        MaturitySuggestion(
            value="mature",
            confidence=conf,
            rationale=(
                f"PageRank p{round(pr_pct * 100)}, {degree} manual link(s) "
                f"(needs both: central AND curated)"
            ),
            auto_apply=conf >= MATURE_AUTO,
        ),
        diag,
    )


async def _manual_link_degree(
    session: AsyncSession, *, org_id: uuid.UUID, note_id: uuid.UUID
) -> int:
    """Undirected manual-link degree: the number of typed note↔note link
    rows touching the note (either endpoint)."""
    rows = (
        await session.execute(
            select(NoteNoteLink.parent_note_id, NoteNoteLink.child_note_id).where(
                NoteNoteLink.org_id == org_id
            )
        )
    ).all()
    return sum(1 for p, c in rows if note_id in (p, c))


async def _suggest_cluster(
    session: AsyncSession, *, org_id: uuid.UUID, node_id: uuid.UUID, damping: float = 1.0
) -> tuple[ClusterSuggestion | None, str]:
    res = await graph_svc.compute_leiden_clusters(session, org_id=org_id)
    if res.modularity is None:
        # The optional ``clustering`` extra is absent; degrade explicitly.
        return None, "leiden_extra_absent"
    return (
        ClusterSuggestion(
            leiden_id=res.clusters.get(node_id),
            modularity=res.modularity,
            # Modularity is the confidence proxy: a well-separated
            # partition means the community assignment is trustworthy.
            # Cold-start damped (a 1-community collapse is near-0 already).
            confidence=max(0.0, res.modularity) * damping,
        ),
        "leiden_cluster",
    )


async def classify_node(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    kinds: frozenset[str] | None = None,
    k: int = DEFAULT_K,
    user_id: uuid.UUID | None = None,
) -> ClassifyResult:
    """Return the structured enrichment proposal for ``node_id``.

    Read-only. RLS-scoped (every underlying query filters ``org_id`` and
    runs on the caller's tenant session). v1 classifies **notes**; tasks
    (tags-only per ADR-0032) and note_part/blob are a follow-up.

    When ``user_id`` is given, the user's learned priors (ADR-0037) re-rank
    the tag and link candidates toward what they have accepted before; the
    confidence floors stay on the structural signal, so the prior re-ranks
    but never prunes. Omitting ``user_id`` is the pure structural path
    (system callers, tests), unchanged.
    """
    wanted = kinds if kinds is not None else ALL_KINDS
    # Tasks become classifiable only when the unified-task-graph flag is on
    # (ADR-0042). With it off, a non-note id is "not found" exactly as before
    # and the corpus/behaviour for notes is byte-identical.
    unified = get_settings().garden_unified_task_graph_enabled
    note = (
        await session.execute(
            select(Note).where(Note.id == node_id, Note.org_id == org_id, Note.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    node_kind = "note"
    if note is None:
        task = None
        if unified:
            task = (
                await session.execute(
                    select(Task).where(
                        Task.id == node_id, Task.org_id == org_id, Task.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
        if task is None:
            raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
        node_kind = "task"

    signals: list[str] = []
    raw: dict[str, float] = {}

    # Cold-start damping (WS-D5): scale every confidence by how mature the
    # corpus is. On a sparse graph the floor-based heuristics would emit
    # "confident" but empty suggestions; damping pushes weak signals back
    # under their floors instead. Counted once and threaded into each suggester.
    # When the task graph is unified (ADR-0042) the corpus is notes + tasks.
    node_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Note)
                .where(Note.org_id == org_id, Note.deleted_at.is_(None))
            )
        ).scalar_one()
    )
    if unified:
        node_count += int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.org_id == org_id, Task.deleted_at.is_(None))
                )
            ).scalar_one()
        )
    damping = _cold_start_damping(node_count)
    if damping < 1.0:
        signals.append("corpus_too_sparse")
        raw["cold_start_damping"] = damping
        raw["node_count"] = float(node_count)

    personalized = False

    tags: list[TagSuggestion] = []
    if "tags" in wanted:
        by_note, tag_deg = await _note_generic_tags(session, org_id=org_id, include_tasks=unified)
        tag_prior_of: Callable[[uuid.UUID], float] = _neutral_prior
        if user_id is not None:
            tp = await garden_learning.personal_priors(
                session, org_id=org_id, user_id=user_id, suggestion_type="tag"
            )
            if tp:
                personalized = True
                tag_prior_of = _factor_lookup(tp, "tag:")
        tags = _suggest_tags(
            node_id=node_id,
            by_note=by_note,
            tag_deg=tag_deg,
            k=k,
            damping=damping,
            prior_of=tag_prior_of,
        )
        if tags:
            signals.append("tag_cooccur_adamic_adar")

    links: list[LinkCandidate] = []
    if "links" in wanted:
        if node_kind == "task":
            # Task link-prediction needs tasks in the graph (ADR-0042 D1);
            # until that lands a task gets no link suggestions, surfaced
            # honestly rather than as a silent empty.
            signals.append("task_links_pending_graph")
        else:
            link_prior_of: Callable[[uuid.UUID], float] = _neutral_prior
            if user_id is not None:
                lp = await garden_learning.personal_priors(
                    session, org_id=org_id, user_id=user_id, suggestion_type="link"
                )
                if lp:
                    personalized = True
                    link_prior_of = _factor_lookup(lp, "link_target:")
            links = await _suggest_links(
                session,
                org_id=org_id,
                node_id=node_id,
                k=k,
                damping=damping,
                prior_of=link_prior_of,
            )
            if links:
                signals.append("linkpred_ppr")

    maturity: MaturitySuggestion | None = None
    if "maturity" in wanted and node_kind == "note" and note is not None:
        # Maturity is a NOTE lifecycle (seed/growing/mature/dormant). Tasks
        # carry workflow states instead, so maturity is N/A for them
        # (ADR-0042 D3, confirmed 2026-06-20): leave it None, no suggestion.
        maturity, mat_diag = await _suggest_maturity(
            session, org_id=org_id, note=note, damping=damping
        )
        raw.update(mat_diag)
        if maturity is not None:
            signals.extend(["pagerank_pct", "manual_degree"])

    cluster: ClusterSuggestion | None = None
    if "cluster" in wanted:
        if node_kind == "task":
            # Leiden runs on the note graph; tasks join it in ADR-0042 D1.
            signals.append("task_cluster_pending_graph")
        else:
            cluster, cluster_signal = await _suggest_cluster(
                session, org_id=org_id, node_id=node_id, damping=damping
            )
            signals.append(cluster_signal)

    # Transparency (ADR-0037): record when a personal prior actually moved
    # the ranking, so the rationale upstream can say "ranked for you".
    if personalized:
        signals.append("personal_prior_applied")

    return ClassifyResult(
        node_id=node_id,
        node_kind=node_kind,
        tags=tags,
        links=links,
        maturity=maturity,
        cluster=cluster,
        signals_used=signals,
        model_version=MODEL_VERSION,
        generated_at=dt.datetime.now(dt.UTC),
        raw=raw,
    )


# --- ADR-0042 D4: the precomputed-suggestion cache -------------------------

# A cached suggestion older than this is recomputed live (the graph may have
# moved under it). These are read-only proposals, so a little staleness is fine.
SUGGESTION_TTL = dt.timedelta(hours=1)


async def persist_classification(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    node_kind: str,
    node_id: uuid.UUID,
    result: ClassifyResult,
    now: dt.datetime | None = None,
) -> int:
    """Cache ``result`` for the node, REPLACING any prior cache for it (the
    table is a cache, not a log -- ADR-0042 D4). Each suggestion becomes one
    ``precomputed_suggestions`` row whose ``suggestion_value`` is the exact
    shape ``apply_suggestion`` consumes. Returns the number of rows written."""
    now = now or dt.datetime.now(dt.UTC)
    await session.execute(
        delete(PrecomputedSuggestion).where(
            PrecomputedSuggestion.org_id == org_id,
            PrecomputedSuggestion.node_id == node_id,
        )
    )
    rows: list[PrecomputedSuggestion] = []

    def _add(stype: str, value: dict[str, Any], confidence: float, rationale: str | None) -> None:
        rows.append(
            PrecomputedSuggestion(
                org_id=org_id,
                node_kind=node_kind,
                node_id=node_id,
                suggestion_type=stype,
                suggestion_value=value,
                confidence=confidence,
                rationale=rationale,
                computed_at=now,
            )
        )

    for t in result.tags:
        _add("tag", {"tag_id": str(t.tag_id)}, t.confidence, t.rationale)
    for link in result.links:
        _add(
            "link",
            {"target_id": str(link.target_id), "link_kind": link.link_kind},
            link.confidence,
            link.rationale,
        )
    if result.maturity is not None:
        _add(
            "maturity",
            {"value": result.maturity.value},
            result.maturity.confidence,
            result.maturity.rationale,
        )
    if result.cluster is not None:
        _add("cluster", {"leiden_id": result.cluster.leiden_id}, result.cluster.confidence, None)

    session.add_all(rows)
    await session.flush()
    return len(rows)


async def read_classification(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    ttl: dt.timedelta = SUGGESTION_TTL,
    now: dt.datetime | None = None,
) -> list[PrecomputedSuggestion] | None:
    """The node's cached suggestions when FRESH (newest within ``ttl``), else
    ``None`` so the caller recomputes live (ADR-0042 D4). RLS scopes the read
    to the caller's org."""
    now = now or dt.datetime.now(dt.UTC)
    rows = list(
        (
            await session.execute(
                select(PrecomputedSuggestion)
                .where(
                    PrecomputedSuggestion.org_id == org_id,
                    PrecomputedSuggestion.node_id == node_id,
                )
                .order_by(PrecomputedSuggestion.computed_at.desc())
            )
        ).scalars()
    )
    if not rows or rows[0].computed_at < now - ttl:
        return None
    return rows


# Cap per drain so the on-create queue bleeds off over ticks (backpressure),
# like the search/embedding backfills.
_JOB_BATCH = 50


async def process_classification_jobs(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    limit: int = _JOB_BATCH,
    now: dt.datetime | None = None,
) -> dict[str, int]:
    """Drain pending on-create classification jobs (ADR-0042 D5): classify each
    node and cache the result (D4). Per-job SAVEPOINT isolation so one bad node
    never rolls back the batch; a poison node is marked ``error`` with the
    message, never retried in a loop. Returns ``{"processed", "errors"}``."""
    now = now or dt.datetime.now(dt.UTC)
    jobs = list(
        (
            await session.execute(
                select(ClassificationJob)
                .where(
                    ClassificationJob.org_id == org_id,
                    ClassificationJob.status == "pending",
                )
                .order_by(ClassificationJob.created_at)
                .limit(limit)
            )
        ).scalars()
    )
    processed = 0
    errors = 0
    for job in jobs:
        try:
            async with session.begin_nested():
                result = await classify_node(session, org_id=org_id, node_id=job.node_id)
                await persist_classification(
                    session,
                    org_id=org_id,
                    node_kind=job.node_kind,
                    node_id=job.node_id,
                    result=result,
                    now=now,
                )
            job.status = "done"
            job.processed_at = now
            processed += 1
        except Exception as exc:  # isolate a poison job; mark error, never loop on it
            job.status = "error"
            job.error = str(exc)[:500]
            job.processed_at = now
            errors += 1
    await session.flush()
    return {"processed": processed, "errors": errors}


async def _mutate(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    node_id: uuid.UUID,
    node_kind: str,
    suggestion_type: str,
    value: dict[str, Any] | None,
) -> None:
    """Perform the accepted mutation via the existing services (each is
    idempotent and audited on its own). ``cluster`` is informational in v1
    (clusters are computed, not stored on the node) so it is a no-op. A
    ``tag`` routes to the note- or task-tag service by ``node_kind``
    (ADR-0042); ``link`` / ``maturity`` are note-only in v1."""
    if not value:
        return
    if suggestion_type == "tag":
        tag_id = uuid.UUID(str(value["tag_id"]))
        if node_kind == "task":
            # Local import: avoid a static cycle through flow_core.services.
            from flow_core.services import tasks as tasks_svc

            await tasks_svc.attach_tag(
                session, org_id=org_id, actor_id=actor_id, task_id=node_id, tag_id=tag_id
            )
        else:
            await notes_svc.attach_tag(
                session, org_id=org_id, actor_id=actor_id, note_id=node_id, tag_id=tag_id
            )
    elif suggestion_type == "link":
        await note_links.link_notes(
            session,
            org_id=org_id,
            actor_id=actor_id,
            parent_note_id=node_id,
            child_note_id=uuid.UUID(str(value["target_id"])),
            kind=str(value.get("link_kind", "related")),
        )
    elif suggestion_type == "maturity":
        await note_links.set_maturity(
            session,
            org_id=org_id,
            actor_id=actor_id,
            note_id=node_id,
            maturity=str(value["value"]),
        )


async def apply_suggestion(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    node_id: uuid.UUID,
    suggestion_type: str,
    suggestion_value: dict[str, Any],
    action: str,
    override_value: dict[str, Any] | None = None,
    model_version: str = MODEL_VERSION,
    signals_snapshot: dict[str, Any] | None = None,
) -> ClassificationFeedback:
    """Apply (or decline) a ``classify_node`` suggestion and record the
    decision (ADR-0032 / ADR-0037).

    The mutating, reversible counterpart to ``classify_node``:
    ``accept``/``override`` perform the mutation through the existing
    idempotent services; ``reject``/``ignore`` mutate nothing. Either way
    an append-only ``classification_feedback`` row is written — the event
    that makes the decision auditable and the learning loop / rollback
    possible. Member role required; RLS-scoped.
    """
    if suggestion_type not in SUGGESTION_TYPES:
        raise DomainError(
            MessageCode.GARDEN_SUGGESTION_TYPE_INVALID,
            suggestion_type=suggestion_type,
            valid=", ".join(sorted(SUGGESTION_TYPES)),
        )
    if action not in FEEDBACK_ACTIONS:
        raise DomainError(
            MessageCode.GARDEN_ACTION_INVALID,
            action=action,
            valid=", ".join(sorted(FEEDBACK_ACTIONS)),
        )
    await require_role(session, org_id, actor_id, Role.member)
    actor_kind = await event_bus.session_actor_kind(session)
    # Resolve the node kind once: a Note row means "note", otherwise "task"
    # (ADR-0042). The is_inert gate below is a NOTE invariant, so it is a
    # no-op for tasks (``note`` is None for a task id).
    note = await session.get(Note, node_id)
    node_kind = "note" if note is not None else "task"

    if action in {"accept", "override", "auto"}:
        # ADR-0036 amendment (c19b5489): an autonomous AGENT must not
        # commit over a live note. Human actors are the authority on their
        # own live notes; system batch jobs carry their own open-work guard
        # (auto_promote_mature), so the gate is agent-only. Tasks are not
        # subject to the note anti-mutation invariant.
        if (
            actor_kind == "agent"
            and note is not None
            and not await note_inert.is_inert(session, note=note)
        ):
            raise ConflictError(MessageCode.EVENT_NODE_NOT_INERT)
        effective = override_value if action == "override" else suggestion_value
        await _mutate(
            session,
            org_id=org_id,
            actor_id=actor_id,
            node_id=node_id,
            node_kind=node_kind,
            suggestion_type=suggestion_type,
            value=effective,
        )

    feedback = ClassificationFeedback(
        org_id=org_id,
        user_id=actor_id,
        node_id=node_id,
        suggestion_type=suggestion_type,
        suggestion_value=suggestion_value,
        action=action,
        override_value=override_value,
        model_version=model_version,
        signals_snapshot=signals_snapshot or {},
    )
    session.add(feedback)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity=node_kind,
        entity_id=node_id,
        action=f"garden_apply:{suggestion_type}:{action}",
    )
    # ADR-0036: the decision rides the bus too (synthetic propose -> commit
    # /reject), in the SAME transaction as the feedback row so the audit
    # stream and the learning loop can never disagree.
    await event_bus.record_classification_decision(
        session,
        actor_kind=actor_kind,
        org_id=org_id,
        actor_id=actor_id,
        node_id=node_id,
        suggestion_type=suggestion_type,
        suggestion_value=suggestion_value,
        action=action,
        model_version=model_version,
        signals_snapshot=signals_snapshot or {},
        override_value=override_value,
    )
    # ADR-0037: fold the decision into the actor's personal priors, in the
    # SAME transaction as the feedback row so the prior stays a coherent
    # projection of the log. A no-op for ``auto`` and non-learnable types.
    await garden_learning.record_decision(
        session,
        org_id=org_id,
        user_id=actor_id,
        suggestion_type=suggestion_type,
        suggestion_value=suggestion_value,
        action=action,
        override_value=override_value,
    )
    return feedback


async def auto_promote_mature(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID
) -> int:
    """Batch value-axis auto-promotion ``growing → mature`` for a
    workspace — the worker step behind ADR-0032's automatic idea evolution.

    PageRank + degree are computed once; every growing, non-promoted note
    is evaluated with the same ``min(pr_pct, deg_term)`` policy as
    ``classify_node``. Notes clearing ``MATURE_AUTO`` (central AND curated)
    are promoted via ``set_maturity`` (which audits) and get an append-only
    ``classification_feedback`` row with ``action='auto'`` so the automatic
    decision is transparent and reversible. Returns the count promoted. The
    worker runs this as the workspace owner.
    """
    pageranks = await graph_svc.compute_pagerank(session, org_id=org_id)
    if len(pageranks) <= 1:
        return 0
    growing = (
        (
            await session.execute(
                select(Note).where(
                    Note.org_id == org_id,
                    Note.maturity == "growing",
                    Note.promoted_at.is_(None),
                    Note.deleted_at.is_(None),
                    # Invariant (task 8a26c000): do not auto-crystallise a
                    # note with active work (an open linked task).
                    ~note_inert.open_work_exists(Note.id),
                )
            )
        )
        .scalars()
        .all()
    )
    if not growing:
        return 0
    link_rows = (
        await session.execute(
            select(NoteNoteLink.parent_note_id, NoteNoteLink.child_note_id).where(
                NoteNoteLink.org_id == org_id
            )
        )
    ).all()
    degree: dict[uuid.UUID, int] = defaultdict(int)
    for parent_id, child_id in link_rows:
        degree[parent_id] += 1
        degree[child_id] += 1

    promoted = 0
    for note in growing:
        pr_pct = _pr_percentile(pageranks, note.id)
        deg_term = min(1.0, degree.get(note.id, 0) / DEG_SATURATION)
        conf = min(pr_pct, deg_term)
        if conf < MATURE_AUTO:
            continue
        await note_links.set_maturity(
            session, org_id=org_id, actor_id=actor_id, note_id=note.id, maturity="mature"
        )
        signals = {
            "pr_pct": pr_pct,
            "manual_degree": float(degree.get(note.id, 0)),
            "conf_mature": conf,
        }
        session.add(
            ClassificationFeedback(
                org_id=org_id,
                user_id=actor_id,
                node_id=note.id,
                suggestion_type="maturity",
                suggestion_value={"value": "mature"},
                action="auto",
                model_version=MODEL_VERSION,
                signals_snapshot=signals,
            )
        )
        # ADR-0036 §123: the worker's auto-promotion is a system commit on
        # the bus (no inert gate -- a growing note is never inert; the
        # open_work_exists guard above already protects active work).
        await event_bus.record_classification_decision(
            session,
            actor_kind="system",
            org_id=org_id,
            actor_id=actor_id,
            node_id=note.id,
            suggestion_type="maturity",
            suggestion_value={"value": "mature"},
            action="auto",
            model_version=MODEL_VERSION,
            signals_snapshot=signals,
        )
        promoted += 1
    await session.flush()
    return promoted


async def autoclassify_unprocessed(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    limit: int = 100,
) -> int:
    """Autonomous, read-only classify-on-ingest (WS-D2, ADR-0032 P4).

    Stamps notes the autonomous pass has never seen (``auto_classified_at``
    IS NULL) with the structural Leiden community the offline graph snapshot
    ALREADY computed for them (read from ``graph_snapshot`` -- zero per-node
    recompute, the O(V·E) Leiden run is not paid here) plus an
    ``auto_classified_at`` marker. The opinionated tag/link/maturity
    suggestions stay human-applied via the live ``classify_node`` panel; this
    records only the cheap, objective community signal so a new node is
    grouped without the user triggering classify per node. Bounded by
    ``limit`` (drains over ticks, like the search backfills); returns the
    count classified this pass. A note with no community in the snapshot
    (isolated / clustering extra absent) is still marked seen, with
    ``auto_cluster`` left NULL -- the marker distinguishes "processed,
    singleton" from "not processed".

    No ``require_role``: the caller is the worker's system actor on the
    owner's tenant session (RLS-scoped), mirroring ``auto_promote_mature``.
    """
    snap = await graph_snapshot.get_graph_snapshot(session, org_id=org_id)
    if snap is None:
        return 0
    clusters = snap.clusters or {}
    rows = list(
        (
            await session.execute(
                select(Note)
                .where(
                    Note.org_id == org_id,
                    Note.deleted_at.is_(None),
                    Note.auto_classified_at.is_(None),
                )
                .order_by(Note.created_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    now = dt.datetime.now(dt.UTC)
    for note in rows:
        community = clusters.get(str(note.id))
        note.auto_cluster = community if isinstance(community, int) else None
        note.auto_classified_at = now
    await session.flush()
    return len(rows)


__all__ = [
    "ClassifyResult",
    "ClusterSuggestion",
    "LinkCandidate",
    "MaturitySuggestion",
    "TagSuggestion",
    "apply_suggestion",
    "auto_promote_mature",
    "autoclassify_unprocessed",
    "classify_node",
]
