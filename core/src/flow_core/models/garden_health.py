"""Daily garden-health snapshots (ADR-0035).

One immutable row per ``(org, day)``: the structural symbiosis metrics
the nightly worker tick computes, kept as a time-series so the dashboard
can draw a 30-day sparkline. The live ``GET /garden/health`` endpoint
computes current values directly; this table is the history. RLS-scoped
by ``org_id`` (ADR-0007).

``metrics`` is the full metric set as JSON (``{key: {value, floor,
reason}}``) so adding a sensor never needs a schema migration.
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import Date, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from flow_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class GardenHealthDaily(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "garden_health_daily"
    __table_args__ = (UniqueConstraint("org_id", "day", name="uq_garden_health_daily_org_day"),)

    day: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
