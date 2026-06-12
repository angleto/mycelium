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
  * ``fungal_lag``: blocked on the decomposition pipeline (ADR-0039)
    emitting distillation notes.

The nightly worker tick calls :func:`persist_snapshot` to write one
``garden_health_daily`` row per org per day; the live endpoint reads the
current values from :func:`compute_health` and the trend from
:func:`recent_snapshots`.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.classification_feedback import ClassificationFeedback
from flow_core.models.garden_health import GardenHealthDaily
from flow_core.models.note import Note
from flow_core.models.note_link import NoteNoteLink
from flow_core.models.search_click import SearchClick
from flow_core.services import graph as graph_svc

ACCEPT_RATE_FLOOR = 0.40
TAG_ENTROPY_FLOOR = 1.2
# Top-K window of the recall sensor: a click below this rank says the
# ranking failed badly enough that "was it top-1?" is no longer the
# interesting question, so it is excluded from the denominator.
RECALL_K = 10

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
_FUNGAL_BLOCKED = "blocked: decomposition pipeline (ADR-0039) not emitting distillation notes yet"


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

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {key: asdict(metric) for key, metric in vars(self).items()}


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
    the mycelium absorbs new notes faster."""
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
            select(Note.id, Note.created_at).where(Note.org_id == org_id, Note.deleted_at.is_(None))
        )
    ).all()
    created: dict[uuid.UUID, datetime.datetime] = {nid: ts for nid, ts in created_rows}
    first: dict[uuid.UUID, datetime.datetime] = {}
    for parent_id, child_id, ts in links:
        for nid in (parent_id, child_id):
            cur = first.get(nid)
            if cur is None or ts < cur:
                first[nid] = ts
    deltas = sorted(
        (link_ts - created[nid]).total_seconds() for nid, link_ts in first.items() if nid in created
    )
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
    mycelium is thickening. None until there were notes 7 days ago."""
    cutoff = now - datetime.timedelta(days=7)
    notes_now = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Note)
                .where(Note.org_id == org_id, Note.deleted_at.is_(None))
            )
        ).scalar_one()
    )
    notes_then = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Note)
                .where(
                    Note.org_id == org_id,
                    Note.deleted_at.is_(None),
                    Note.created_at <= cutoff,
                )
            )
        ).scalar_one()
    )
    if not notes_now or not notes_then:
        return None
    links_now = int(
        (
            await session.execute(
                select(func.count()).select_from(NoteNoteLink).where(NoteNoteLink.org_id == org_id)
            )
        ).scalar_one()
    )
    links_then = int(
        (
            await session.execute(
                select(func.count())
                .select_from(NoteNoteLink)
                .where(NoteNoteLink.org_id == org_id, NoteNoteLink.created_at <= cutoff)
            )
        ).scalar_one()
    )
    return round(links_now / notes_now - links_then / notes_then, 4)


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
        fungal_lag=Metric(None, None, _FUNGAL_BLOCKED),
        density_delta_7d=Metric(density, None, None if density is not None else _NO_NOTES),
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
