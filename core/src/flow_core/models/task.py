"""Task: primary unit. State is a workflow state (docs/adr/0004,
FR-6); scheduler fields arrive in F3. Executor = human user or LLM
agent."""

from __future__ import annotations

import datetime
import enum
import uuid
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class ExecKind(enum.StrEnum):
    human = "human"
    llm_agent = "llm_agent"


class ScheduleMode(enum.StrEnum):
    auto = "auto"
    manual = "manual"


class ConstraintKind(enum.StrEnum):
    none = "none"
    SNET = "SNET"  # start no earlier than
    MSO = "MSO"  # must start on
    MFO = "MFO"  # must finish on


class Necessity(enum.StrEnum):
    must = "must"
    should = "should"
    nice = "nice"


class Task(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "tasks"

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="3")
    # Eisenhower inputs (1..5); priority is derived from them in the
    # service. Nullable: the legacy priority path (or pre-migration
    # tasks) leaves them unset.
    importance: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    urgency: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    start_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    state_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workflow_states.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    estimate_effort_h: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    executor_kind: Mapped[ExecKind] = mapped_column(
        SAEnum(ExecKind, name="exec_kind", native_enum=True, create_type=False),
        nullable=False,
        server_default="human",
    )
    executor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_archived: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Scheduler fields (F3). Defaults keep earlier phases unaffected.
    remaining_effort_h: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    actual_start: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    schedule_mode: Mapped[ScheduleMode] = mapped_column(
        SAEnum(
            ScheduleMode,
            name="schedule_mode",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        server_default="auto",
    )
    constraint_kind: Mapped[ConstraintKind] = mapped_column(
        SAEnum(
            ConstraintKind,
            name="constraint_kind",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        server_default="none",
    )
    constraint_date: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_milestone: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    # Personal-domain attributes (F4b, docs/adr/0014).
    monetary_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    necessity: Mapped[Necessity] = mapped_column(
        SAEnum(Necessity, name="necessity", native_enum=True, create_type=False),
        nullable=False,
        server_default="should",
    )
    budget_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("budgets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
