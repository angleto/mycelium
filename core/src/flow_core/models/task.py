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
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY
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


class SchedulePolicy(enum.StrEnum):
    """Resource-leveling objective, selected per recompute run
    (docs/adr/0025, P1). Not persisted on the task: it is a knob of the
    recompute call (like ``as_of``), so it lives with the task enums but
    has no column. Every policy is fully deterministic (id final
    tie-break) -- see ``scheduler._policy_key``."""

    fastest = "fastest"
    cheapest = "cheapest"
    balanced = "balanced"
    throughput = "throughput"


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
    # Human-readable assignee (migration 0060). Backfilled from
    # ``executor_user_id`` -> users.handle. NULL when the task has no
    # human executor (LLM agent or unassigned). Stage A of the
    # "kill Executor" refactor: ``executor_user_id`` and ``executor_kind``
    # remain authoritative for the scheduler/dispatch; ``assignee_handle``
    # is the new addressable surface for UI / MCP picker.
    assignee_handle: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Capabilities this task needs from its executor (docs/adr/0025 P2
    # admission control). An llm task is eligible for an ``llm_agent``
    # executor iff this set is a subset of the executor's
    # ``capability_tags`` (empty = any enabled agent). ``text[]`` (the
    # flat-string-list norm); matching is Python set containment.
    required_capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'")
    )
    # NULL = inherit the project's default_billable (or true with no
    # project); true/false = explicit per-task override.
    billable: Mapped[bool | None] = mapped_column(nullable=True)
    is_archived: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    # Contract-net (docs/adr/0025, P4): the lightweight "announced /
    # awaiting claim" flag. Set by ``POST /tasks/{id}/offer`` (owner),
    # cleared on ``claim`` (the claimant becomes a TaskAssignee) -- a
    # single transient per-task boolean, no bid table (full bidding is
    # beyond P4's minimal contract-net; the llm_agent "award" is the P2
    # admission dispatch, not re-implemented here).
    offered: Mapped[bool] = mapped_column(nullable=False, server_default="false")
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
