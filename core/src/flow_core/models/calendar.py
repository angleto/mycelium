"""Working calendars (FR-4): per-Org default + per-user override with
daily capacity; holidays. Drives working-time scheduling (F3)."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class WorkingCalendar(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "working_calendars"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_default: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="Europe/Rome")
    weekly_hours: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class CalendarHoliday(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "calendar_holidays"
    __table_args__ = (UniqueConstraint("calendar_id", "day"),)

    calendar_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("working_calendars.id", ondelete="CASCADE"),
        nullable=False,
    )
    day: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserCalendar(OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "user_calendar"
    __table_args__ = (PrimaryKeyConstraint("org_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    calendar_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("working_calendars.id", ondelete="CASCADE"),
        nullable=False,
    )
    daily_capacity_h: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default="8"
    )
