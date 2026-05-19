"""Derived schedule (FR-4, docs/adr/0004). One row per task, written
by the deterministic scheduler; not under optimistic concurrency (the
most recent recompute wins)."""

from __future__ import annotations

import datetime
import decimal
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
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
    # Resource-aware critical chain (docs/adr/0025, P1): zero float in
    # the *leveled* plan (after resource contention), distinct from the
    # logical critical path which assumes infinite resources.
    on_critical_chain: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    # Projected LLM credit cost = effort_hours * executor.credit_rate
    # (0 for human tasks). Work is not run yet -> projection only, no
    # billing meter (ADR-0019).
    projected_cost: Mapped[decimal.Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, server_default=text("0")
    )
    scheduled_start: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scheduled_end: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Admission-control dispatch result (docs/adr/0025, P2). The executor
    # the scheduler placed this task on (NULL for human tasks routed by
    # calendar, off-timeline rows, or an unassignable llm task). FK SET
    # NULL: deleting an executor leaves prior schedule rows readable; the
    # next recompute re-dispatches.
    assigned_executor_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("executors.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # True iff no admissible executor exists for this llm task (no
    # capable enabled agent, or all eligible agents would exceed budget
    # within the horizon). A flagged dispatch gap, not silently
    # scheduled. ``unassignable_reason`` is a stable short string from a
    # fixed set (see scheduler ``_UNASSIGNABLE_*``).
    unassignable: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    unassignable_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    input_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
