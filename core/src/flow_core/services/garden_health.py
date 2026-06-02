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
  * ``recall_at_k``: blocked on search-click logs (the ``is_probe`` flag)
    that do not exist yet.
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

ACCEPT_RATE_FLOOR = 0.40
TAG_ENTROPY_FLOOR = 1.2

# A human decision on a proposal. ``auto`` (the worker's system
# promotion) is excluded so the ratio reflects what the *person* chose.
_DECIDED: tuple[str, ...] = ("accept", "override", "reject", "ignore")
_ACCEPTED: tuple[str, ...] = ("accept", "override")

_NO_DECISIONS = "no classification decisions in window yet"
_TODO = "not yet implemented (follow-up)"
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
    return GardenHealth(
        accept_rate_classify_7d=Metric(
            r7, ACCEPT_RATE_FLOOR, None if r7 is not None else _NO_DECISIONS
        ),
        accept_rate_classify_30d=Metric(
            r30, ACCEPT_RATE_FLOOR, None if r30 is not None else _NO_DECISIONS
        ),
        time_to_first_link=Metric(None, None, _TODO),
        tag_entropy_local=Metric(None, TAG_ENTROPY_FLOOR, _TODO),
        leiden_modularity=Metric(None, None, _TODO),
        fungal_lag=Metric(None, None, _FUNGAL_BLOCKED),
        density_delta_7d=Metric(None, None, _TODO),
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
