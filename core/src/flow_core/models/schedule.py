"""Derived schedule (FR-4, docs/adr/0004). One row per task, written
by the deterministic scheduler; not under optimistic concurrency (the
most recent recompute wins)."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base, OrgScopedMixin


class Schedule(OrgScopedMixin, Base):
    __tablename__ = "schedule"
    __table_args__ = (PrimaryKeyConstraint("task_id"),)

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    es: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ef: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ls: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lf: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slack_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    on_logical_critical_path: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    scheduled_start: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scheduled_end: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    input_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
