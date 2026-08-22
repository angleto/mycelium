"""Garden health sensors (ADR-0035): structural metrics on the
memory<->mycelium symbiosis, so the forester can *see* whether the
system is getting more useful over time.

Principle "show, never judge" (manifesto): every metric carries its
value and its health floor (when it has one), never a verdict.

v1 computes ``accept_rate_classify`` (the headline sensor, floor 0.40)
live from the ``classification_feedback`` log. The other six sensors are
declared with their floors but return ``value=None`` + a ``reason``
until their data source lands -- explicit, never a faked number:

  * ``time_to_first_link`` / ``tag_entropy_local`` / ``leiden_modularity``
    / ``density_delta_7d``: follow-up (graph + snapshot-history queries).
  * ``recall_at_k``: computed from the ``search_clicks`` log (task
    89508ca9). Today the production ranking IS the held-out re-rank
    (no learning loop trains on clicks yet, ADR-0037 pending), so the
    metric is the fraction of real (non-probe) clicked queries whose
    clicked node sat at rank 1 of the top-``RECALL_K``. When ADR-0037
    lands, this computation must switch to the leave-one-out re-rank
    the ADR specifies.
  * ``fungal_lag`` (WS-C6): median seconds from a source note's first
    archiving to its first distillation (``hypha_of`` humus atom) -- how
    long decomposition takes to turn an archived note into humus.
    ``value=None`` with a reason only when there are genuinely no
    distillations yet (or none from an archived source).
  * ``embedding_coverage`` (WS-A): fraction of the embeddable memory
    corpus carrying a local dense vector, floor-gated -- the visible alert
    that the dense backfill stalled and semantic retrieval has decayed to
    keyword-only. ``value=None`` only when nothing is embeddable yet.

The nightly worker tick calls :func:`persist_snapshot` to write one
``garden_health_daily`` row per org per day; the live endpoint reads the
current values from :func:`compute_health` and the trend from
:func:`recent_snapshots`.

PERIMETER (ADR-0043 D1, task 02f8f7c7): every sensor that counts notes
or links counts the EFFECTIVE ones -- ``review_state IS DISTINCT FROM
'proposed' AND deleted_at IS NULL`` -- through
:func:`~mycelium_core.services.note_effective.effective_note_clause`, at
both ends of a link. The graph sensors (``tag_entropy_local``,
``leiden_modularity``) get it from ``graph``; ``time_to_first_link``,
``density_delta_7d`` and ``fungal_lag`` apply it here. Snapshots persist
the value that was true under the perimeter of the day they were taken,
so the stored series steps once at the first tick after this shipped
(most visibly where a workspace holds proposals or a full bin) and is
continuous from there: the history is not rewritten, and no sensor is
back-filled to pretend it always read this way.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.models.classification_feedback import ClassificationFeedback
from mycelium_core.models.garden_health import GardenHealthDaily
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.models.note import Note
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.models.retrieval_trace import RetrievalTrace
from mycelium_core.models.search_click import SearchClick
from mycelium_core.services import graph as graph_svc
from mycelium_core.services.note_effective import effective_note_clause

ACCEPT_RATE_FLOOR = 0.40
TAG_ENTROPY_FLOOR = 1.2
# Dense-tier embedding coverage floor (WS-A, task 38ac2bc0): the fraction of
# the embeddable corpus that must carry a local dense vector before semantic
# retrieval is considered materially complete. A *persistently* low reading
# means the backfill is not keeping up or the embedder is unavailable -- the
# "dense tier empty" failure this sensor guards. A tunable module constant,
# not a verdict: per ADR-0035 "show, never judge", the card shows value vs
# floor and the forester decides.
EMBEDDING_COVERAGE_FLOOR = 0.80
# Earned-autonomy reliability floor (ADR-0043 D4): the rate at or above which
# a model's AUTONOMOUSLY-generated proposals are accepted by a human often
# enough to be considered reliable. A reference line, not a verdict ("show,
# never judge"); a future per-workspace policy MAY use it to earn a model
# auto-approve, never assumed.
AUTONOMOUS_ACCEPT_RATIO_FLOOR = 0.70
# Top-K window of the recall sensor: a click below this rank says the
# ranking failed badly enough that "was it top-1?" is no longer the
# interesting question, so it is excluded from the denominator.
RECALL_K = 10

# "what changed" timeline (ADR-0035 §84) ------------------------------
# A UTC day whose note create/archive/delete count reaches this is
# surfaced as a "big corpus edit": enough to plausibly move density /
# note count, not the everyday one-off capture. A module constant, not a
# per-org setting (YAGNI): promote to Organization.settings only if a
# workspace ever needs to tune it.
BULK_EDIT_THRESHOLD = 10
# Note lifecycle actions that change the *shape* of the corpus (count /
# density) -- the bursts that plausibly explain a sensor shift. Content
# edits (update / attach_tag) are excluded: they don't move the
# structural sensors and would only add noise to the timeline.
_CORPUS_SHAPE_ACTIONS: tuple[str, ...] = ("create", "archive", "delete")

# A human decision on a proposal. ``auto`` (the worker's system
# promotion) is excluded so the ratio reflects what the *person* chose.
_DECIDED: tuple[str, ...] = ("accept", "override", "reject", "ignore")
_ACCEPTED: tuple[str, ...] = ("accept", "override")

_NO_DECISIONS = "no classification decisions in window yet"
_NO_LINKS = "no note-to-note links yet"
_NO_TAGGED_NEIGHBOURHOOD = "no linked neighbourhood carries a generic tag yet"
_NO_CLUSTERS = "clustering unavailable (needs the optional extra, or >=1 community)"
_NO_NOTES = "no notes yet"
_NO_CLICKS = "no real (non-probe) search clicks in window yet"
_NO_EMBEDDABLE = "no embeddable blobs yet"
_NO_DISTILLATIONS = "no distillation notes yet (decomposition has not produced humus)"
_NO_ARCHIVED_DISTILLATIONS = "distillations exist but none derive from an archived source yet"
_NO_REVIEWS = "no autonomous proposals reviewed yet"


@dataclass(frozen=True)
class Metric:
    value: float | None
    floor: float | None = None
    # Populated only when ``value`` is None: why the sensor has no reading.
    reason: str | None = None


@dataclass(frozen=True)
class GardenHealth:
    accept_rate_classify_7d: Metric
    accept_rate_classify_30d: Metric
    time_to_first_link: Metric
    recall_at_k: Metric
    tag_entropy_local: Metric
    leiden_modularity: Metric
    fungal_lag: Metric
    density_delta_7d: Metric
    # WS-A (task 38ac2bc0): fraction of the embeddable memory corpus that
    # carries a local dense vector. Floor-gated -- a persistently low reading
    # means the dense backfill has stalled (or the embedder is down), so
    # semantic retrieval is degraded to keyword-only.
    embedding_coverage: Metric
    # WS-F5: today's autonomous (system) spend against the per-workspace
    # daily cap. value=spend / floor=cap when capped; value=None + reason
    # when the kill-switch is off or no cap is configured.
    autonomous_spend_today: Metric
    # ADR-0043 D4: how often a human approved (vs rejected) the garden's
    # AUTONOMOUSLY-generated proposals, workspace-wide. The earned-autonomy
    # reliability signal; value=None + reason until any proposal is reviewed.
    autonomous_accept_ratio: Metric
    # ADR-0048 (task 68052297): rows in ``retrieval_trace`` OLDER than the
    # effective retention window -- rows the edge-usage fold can never read
    # again, so with a healthy ``fuel_retention`` sweep this sits at ~0.
    # A persistently growing reading means the pruner is not running and
    # the fuel table is accumulating unbounded (the storage blind spot the
    # 2026-07-17 memory audit flagged). Count shown as-is, no floor:
    # "show, never judge".
    trace_backlog: Metric

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {key: asdict(metric) for key, metric in vars(self).items()}


@dataclass(frozen=True)
class HealthEvent:
    """One entry on the "what changed" timeline (ADR-0035 §84): when it
    happened, its kind, and a small kind-specific detail bag. Read-only
    and factual ("show, never judge"): it records *that* something
    happened, never whether it was good."""

    at: datetime.datetime
    kind: Literal["classifier_version", "corpus_edit"]
    detail: dict[str, Any]


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


async def _accept_rate(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    window_days: int,
    now: datetime.datetime,
) -> float | None:
    """Fraction of human decisions in the trailing window that accepted
    the suggestion (accept or override), over all decisions
    (accept/override/reject/ignore). None when there were no decisions
    -- a ratio over an empty denominator is not "0% healthy", it is
    "no signal yet"."""
    since = now - datetime.timedelta(days=window_days)
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(ClassificationFeedback.action.in_(_ACCEPTED)),
            ).where(
                ClassificationFeedback.org_id == org_id,
                ClassificationFeedback.action.in_(_DECIDED),
                ClassificationFeedback.ts >= since,
            )
        )
    ).one()
    total, accepted = int(row[0]), int(row[1])
    if not total:
        return None
    return round(accepted / total, 4)


async def _recall_at_k(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    window_days: int,
    now: datetime.datetime,
    k: int = RECALL_K,
) -> float | None:
    """Fraction of real (non-probe) search clicks in the trailing window,
    landing inside the top-``k``, whose clicked node was rank 1.

    ADR-0035 defines recall over the *held-out re-rank*; until the
    online-learning loop (ADR-0037) trains on clicks, the production
    ranking and the leave-one-out re-rank are the same thing, so the
    logged rank is the held-out rank. None when there were no real
    clicks: an empty denominator is "no signal yet", not zero."""
    since = now - datetime.timedelta(days=window_days)
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(SearchClick.rank == 1),
            ).where(
                SearchClick.org_id == org_id,
                SearchClick.is_probe.is_(False),
                SearchClick.rank <= k,
                SearchClick.ts >= since,
            )
        )
    ).one()
    total, top1 = int(row[0]), int(row[1])
    if not total:
        return None
    return round(top1 / total, 4)


async def _time_to_first_link(session: AsyncSession, *, org_id: uuid.UUID) -> float | None:
    """Median seconds from a note's creation to its first note<->note link
    (incoming or outgoing). None when no note has a link yet. Lower means
    the mycelium absorbs new notes faster.

    Read over the EFFECTIVE corpus (ADR-0043 D1), at BOTH ends: a trashed
    or un-approved note is not a node, and an edge with one such end is
    not an edge -- ``graph.compute_note_edge_weights`` drops it, so
    timing an absorption through it would report a link the graph does
    not have. ``created`` already holds exactly the effective notes,
    which makes "endpoint absent from ``created``" the same test as
    "endpoint is ineffective", with no second query to run.
    """
    links = (
        await session.execute(
            select(
                NoteNoteLink.parent_note_id,
                NoteNoteLink.child_note_id,
                NoteNoteLink.created_at,
            ).where(NoteNoteLink.org_id == org_id)
        )
    ).all()
    if not links:
        return None
    created_rows = (
        await session.execute(
            select(Note.id, Note.created_at).where(Note.org_id == org_id, effective_note_clause())
        )
    ).all()
    created: dict[uuid.UUID, datetime.datetime] = {nid: ts for nid, ts in created_rows}
    first: dict[uuid.UUID, datetime.datetime] = {}
    for parent_id, child_id, ts in links:
        if parent_id not in created or child_id not in created:
            continue
        for nid in (parent_id, child_id):
            cur = first.get(nid)
            if cur is None or ts < cur:
                first[nid] = ts
    deltas = sorted((link_ts - created[nid]).total_seconds() for nid, link_ts in first.items())
    if not deltas:
        return None
    mid = len(deltas) // 2
    median = deltas[mid] if len(deltas) % 2 else (deltas[mid - 1] + deltas[mid]) / 2
    return round(median, 1)


async def _density_delta_7d(
    session: AsyncSession, *, org_id: uuid.UUID, now: datetime.datetime
) -> float | None:
    """Change in links-per-note over the last 7 days: current density minus
    the density as of 7 days ago (by row creation time). Positive means the
    mycelium is thickening. None until there were notes 7 days ago.

    Numerator and denominator are read over the SAME perimeter -- the
    effective corpus (ADR-0043 D1), at both ends of every link. Filtering
    the notes alone, which is what this did, made rejecting a proposal
    RAISE the density: ``garden_review.reject_node`` soft-deletes the
    node, so the denominator lost it while its links stayed in the
    numerator, and the sensor reported a thickening mycelium every time
    the forester threw something away.

    Both readings are taken over the corpus AS IT STANDS NOW ("created
    before the cutoff and effective today"), not as it stood 7 days ago:
    the perimeter is derived from the note row and there is no history of
    it to query, so a note trashed yesterday is absent from both terms.
    That makes the delta a comparison between two states of today's
    corpus, which is the honest reading available here -- and the reason
    this is a trend indicator, never an audit figure.
    """
    cutoff = now - datetime.timedelta(days=7)
    parent, child = aliased(Note), aliased(Note)
    links_q = (
        select(func.count())
        .select_from(NoteNoteLink)
        .join(parent, parent.id == NoteNoteLink.parent_note_id)
        .join(child, child.id == NoteNoteLink.child_note_id)
        .where(
            NoteNoteLink.org_id == org_id,
            effective_note_clause(entity=parent),
            effective_note_clause(entity=child),
        )
    )
    notes_q = (
        select(func.count()).select_from(Note).where(Note.org_id == org_id, effective_note_clause())
    )
    notes_now = int((await session.execute(notes_q)).scalar_one())
    notes_then = int((await session.execute(notes_q.where(Note.created_at <= cutoff))).scalar_one())
    if not notes_now or not notes_then:
        return None
    links_now = int((await session.execute(links_q)).scalar_one())
    links_then = int(
        (await session.execute(links_q.where(NoteNoteLink.created_at <= cutoff))).scalar_one()
    )
    return round(links_now / notes_now - links_then / notes_then, 4)


async def _embedding_coverage(session: AsyncSession, *, org_id: uuid.UUID) -> float | None:
    """Fraction of the embeddable memory corpus carrying a local dense vector
    (WS-A, task 38ac2bc0).

    The denominator is the blobs the backfill can actually embed -- ``text``
    present AND non-empty -- mirroring ``embedding_migration._backfill_tier``'s
    ``txt.strip()`` eligibility, so empty-body rows (which the backfill skips
    forever) never peg coverage below 1.0. The numerator is those whose local
    ``embedding`` is populated (``model_id`` is then the producing model, never
    the keyword-only 'none'). None when nothing is embeddable yet: an empty
    denominator is "no signal", not "0% covered"."""
    embeddable = MemoryBlob.text.is_not(None) & (func.trim(MemoryBlob.text) != "")
    row = (
        await session.execute(
            select(
                func.count().filter(embeddable),
                func.count().filter(embeddable & MemoryBlob.embedding.is_not(None)),
            ).where(MemoryBlob.org_id == org_id)
        )
    ).one()
    total, embedded = int(row[0]), int(row[1])
    if not total:
        return None
    return round(embedded / total, 4)


async def _fungal_lag(
    session: AsyncSession, *, org_id: uuid.UUID
) -> tuple[float | None, str | None]:
    """Median seconds from a source note's first archiving to its first
    distillation (the ``hypha_of`` humus atom) -- how long decomposition
    takes to turn an archived note into humus (WS-C6).

    Returns ``(value, reason)``; ``reason`` is set only when ``value`` is
    None: ``_NO_DISTILLATIONS`` when there are genuinely zero effective
    distillations, ``_NO_ARCHIVED_DISTILLATIONS`` when some exist but none
    derive from an archived source (e.g. all distilled on-demand while
    live). "Effective" is the perimeter below: a workspace whose only
    distillations are proposals awaiting review reads as no humus
    produced, which is what it is.
    """
    # (source_id, distillation created_at) for every hypha_of humus atom,
    # over the effective corpus (ADR-0043 D1) at BOTH ends: an un-approved
    # distillation is a PROPOSAL of humus, not humus produced, and counting
    # it timed a decomposition nobody has accepted; a trashed distillation
    # (or a trashed source) is not in the mycelium at all. Only the
    # ``review_state`` leg is new here -- the soft-delete leg was already
    # filtered, on the distillation alone.
    source = aliased(Note)
    distill_rows = (
        await session.execute(
            select(
                NoteNoteLink.parent_note_id,
                Note.created_at,
                effective_note_clause(entity=source).label("source_effective"),
            )
            .join(Note, Note.id == NoteNoteLink.child_note_id)
            .join(source, source.id == NoteNoteLink.parent_note_id)
            .where(
                NoteNoteLink.org_id == org_id,
                NoteNoteLink.kind == "hypha_of",
                Note.humus_kind == "distillation",
                effective_note_clause(),
            )
        )
    ).all()
    if not distill_rows:
        return None, _NO_DISTILLATIONS
    # First distillation per source (the idempotency guard means one per
    # source in practice; min() honours "first" if that ever changes).
    # The source perimeter is read as a COLUMN, not as a WHERE leg, on
    # purpose: an atom whose source has left the mycelium is still an atom,
    # so it belongs in the "are there any distillations at all" answer and
    # only its interval is unmeasurable -- filtering it in SQL would report
    # ``_NO_DISTILLATIONS`` over a corpus that visibly holds humus.
    first_distill: dict[uuid.UUID, datetime.datetime] = {}
    for source_id, distill_ts, source_effective in distill_rows:
        if not source_effective:
            continue
        cur = first_distill.get(source_id)
        if cur is None or distill_ts < cur:
            first_distill[source_id] = distill_ts
    # First archiving per source: the earliest action='archive' audit event.
    # A note starts is_archived=False, so its first archive event is always
    # the flip to True -- unambiguous without parsing the diff (archive and
    # unarchive share the action name).
    archive_rows = (
        await session.execute(
            select(ActivityLog.entity_id, func.min(ActivityLog.ts))
            .where(
                ActivityLog.entity == "note",
                ActivityLog.action == "archive",
                ActivityLog.entity_id.in_(list(first_distill)),
            )
            .group_by(ActivityLog.entity_id)
        )
    ).all()
    first_archive: dict[uuid.UUID, datetime.datetime] = {
        sid: ts for sid, ts in archive_rows if sid is not None
    }
    lags = sorted(
        (first_distill[sid] - first_archive[sid]).total_seconds()
        for sid in first_distill
        if sid in first_archive and first_archive[sid] <= first_distill[sid]
    )
    if not lags:
        return None, _NO_ARCHIVED_DISTILLATIONS
    mid = len(lags) // 2
    median = lags[mid] if len(lags) % 2 else (lags[mid - 1] + lags[mid]) / 2
    return round(median, 1), None


async def _trace_backlog(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    now: datetime.datetime,
) -> float:
    """Count of ``retrieval_trace`` rows older than the effective
    retention window (ADR-0048). The window is the same one the pruner
    applies: the configured retention floored at the edge-usage
    aggregation window, so a non-zero backlog is exactly the set of rows
    the ``fuel_retention`` sweep should have deleted."""
    from mycelium_core.config import get_settings
    from mycelium_core.services.edge_usage import EDGE_USAGE_WINDOW_DAYS

    days = max(get_settings().retrieval_trace_retention_days, EDGE_USAGE_WINDOW_DAYS)
    cutoff = now - datetime.timedelta(days=days)
    count = (
        await session.execute(
            select(func.count())
            .select_from(RetrievalTrace)
            .where(RetrievalTrace.org_id == org_id, RetrievalTrace.created_at < cutoff)
        )
    ).scalar_one()
    return float(count)


async def compute_health(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> GardenHealth:
    """Current sensor readings for one org, computed live."""
    now = now or _utcnow()
    r7 = await _accept_rate(session, org_id=org_id, window_days=7, now=now)
    r30 = await _accept_rate(session, org_id=org_id, window_days=30, now=now)
    recall = await _recall_at_k(session, org_id=org_id, window_days=30, now=now)
    ttl = await _time_to_first_link(session, org_id=org_id)
    entropy = await graph_svc.compute_tag_neighborhood_entropy(session, org_id=org_id)
    modularity = (await graph_svc.compute_leiden_clusters(session, org_id=org_id)).modularity
    density = await _density_delta_7d(session, org_id=org_id, now=now)
    coverage = await _embedding_coverage(session, org_id=org_id)
    fungal_value, fungal_reason = await _fungal_lag(session, org_id=org_id)
    from mycelium_core.services import autonomous_budget, garden_review

    review_ratio = await garden_review.accept_ratio_overall(session, org_id=org_id)
    budget = await autonomous_budget.status(session, org_id=org_id, now=now)
    if not budget.enabled:
        autonomous_metric = Metric(None, None, "autonomous jobs paused (kill switch)")
    elif budget.cap is None:
        autonomous_metric = Metric(None, None, "no autonomous budget cap configured")
    else:
        autonomous_metric = Metric(float(budget.spent_today), float(budget.cap), None)
    return GardenHealth(
        accept_rate_classify_7d=Metric(
            r7, ACCEPT_RATE_FLOOR, None if r7 is not None else _NO_DECISIONS
        ),
        accept_rate_classify_30d=Metric(
            r30, ACCEPT_RATE_FLOOR, None if r30 is not None else _NO_DECISIONS
        ),
        time_to_first_link=Metric(ttl, None, None if ttl is not None else _NO_LINKS),
        recall_at_k=Metric(recall, None, None if recall is not None else _NO_CLICKS),
        tag_entropy_local=Metric(
            entropy,
            TAG_ENTROPY_FLOOR,
            None if entropy is not None else _NO_TAGGED_NEIGHBOURHOOD,
        ),
        leiden_modularity=Metric(
            modularity, None, None if modularity is not None else _NO_CLUSTERS
        ),
        fungal_lag=Metric(fungal_value, None, fungal_reason),
        density_delta_7d=Metric(density, None, None if density is not None else _NO_NOTES),
        embedding_coverage=Metric(
            coverage,
            EMBEDDING_COVERAGE_FLOOR,
            None if coverage is not None else _NO_EMBEDDABLE,
        ),
        autonomous_spend_today=autonomous_metric,
        autonomous_accept_ratio=Metric(
            review_ratio,
            AUTONOMOUS_ACCEPT_RATIO_FLOOR,
            None if review_ratio is not None else _NO_REVIEWS,
        ),
        trace_backlog=Metric(await _trace_backlog(session, org_id=org_id, now=now), None, None),
    )


async def persist_snapshot(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> None:
    """Upsert today's snapshot for one org (idempotent per day): the
    nightly worker tick can run many times a day and only the latest
    computation for ``day`` is kept."""
    now = now or _utcnow()
    health = await compute_health(session, org_id=org_id, now=now)
    metrics = health.as_dict()
    stmt = (
        pg_insert(GardenHealthDaily)
        .values(org_id=org_id, day=now.date(), metrics=metrics)
        .on_conflict_do_update(
            constraint="uq_garden_health_daily_org_day",
            set_={"metrics": metrics, "computed_at": func.now()},
        )
    )
    await session.execute(stmt)


async def recent_snapshots(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    days: int = 30,
) -> list[GardenHealthDaily]:
    """The latest ``days`` daily snapshots, newest first (sparkline)."""
    rows = (
        await session.execute(
            select(GardenHealthDaily)
            .where(GardenHealthDaily.org_id == org_id)
            .order_by(GardenHealthDaily.day.desc())
            .limit(days)
        )
    ).scalars()
    return list(rows)


async def recent_events(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    days: int = 90,
    now: datetime.datetime | None = None,
) -> list[HealthEvent]:
    """The "what changed" timeline (ADR-0035 §84): discrete events that
    plausibly explain a shift in the sensors, so a reading is interpreted
    ("the model changed" vs "I archived 200 notes") instead of guessed.

    DERIVED, not a separate event store: both sources are existing
    RLS-scoped append-only streams, so the timeline is consistent with
    the source of truth by construction and needs no new write path or
    migration. New sources are additive -- a new branch here.

      * ``classifier_version`` -- first appearance of each distinct
        ``classification_feedback.model_version`` inside the window (a
        classifier "bump"). One entry today (``MODEL_VERSION`` is a
        constant); more once the classifier is actually versioned.
      * ``corpus_edit`` -- a UTC day whose note create/archive/delete
        count reached ``BULK_EDIT_THRESHOLD`` (a bulk archive/import/
        cleanup that moves density or note count). One entry per
        (day, action).

    Learning-loop snapshots (ADR-0035 §84's third source) are deferred:
    ADR-0037 has no snapshot stream to derive from yet.

    Returned newest-first, matching the snapshot trend.
    """
    now = now or _utcnow()
    since = now - datetime.timedelta(days=days)
    events: list[HealthEvent] = []

    # Classifier bumps: first ts per distinct model_version, kept only
    # when that first appearance falls inside the window.
    ver_rows = (
        await session.execute(
            select(
                ClassificationFeedback.model_version,
                func.min(ClassificationFeedback.ts),
            )
            .where(ClassificationFeedback.org_id == org_id)
            .group_by(ClassificationFeedback.model_version)
            .having(func.min(ClassificationFeedback.ts) >= since)
        )
    ).all()
    for version, first_ts in ver_rows:
        events.append(
            HealthEvent(at=first_ts, kind="classifier_version", detail={"version": version})
        )

    # Big corpus edits: a UTC day with a burst of shape-changing note
    # actions, one entry per (day, action) at/above the threshold. The
    # ``archive`` action counts both archive and unarchive flips (they
    # share the action name); the count is still an honest "N notes had
    # their archived flag flipped that day".
    day = func.date_trunc("day", ActivityLog.ts)
    edit_rows = (
        await session.execute(
            select(day, ActivityLog.action, func.count())
            .where(
                ActivityLog.org_id == org_id,
                ActivityLog.entity == "note",
                ActivityLog.action.in_(_CORPUS_SHAPE_ACTIONS),
                ActivityLog.ts >= since,
            )
            .group_by(day, ActivityLog.action)
            .having(func.count() >= BULK_EDIT_THRESHOLD)
        )
    ).all()
    for day_ts, action, n in edit_rows:
        events.append(
            HealthEvent(at=day_ts, kind="corpus_edit", detail={"action": action, "count": int(n)})
        )

    events.sort(key=lambda e: e.at, reverse=True)
    return events
