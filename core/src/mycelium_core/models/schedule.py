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

from mycelium_core.models.base import Base, OrgScopedMixin


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
    # Signed: ``lf - ef`` in wall-clock minutes (services/slack.py). Below
    # zero the task cannot finish by the latest finish it is held to, its
    # due date or a successor's.
    slack_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    on_logical_critical_path: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    # The task has no estimate (and is not a milestone, an appointment or
    # closed): its duration is unknown and it was placed as a point.
    estimate_missing: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    # The dates depend on a duration nobody knows, this task's or one
    # upstream of it: they are the earliest the work could be.
    dates_lower_bound: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    # Minutes by which the leveled finish meets (+) or misses (-) the
    # task's own due date. NULL with no due date, no placement, or a
    # closed task.
    due_margin_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
