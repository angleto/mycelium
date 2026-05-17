"""Task: primary unit. F1 subset (workflow states in F2, scheduler
fields in F3). Executor = human user or LLM agent (docs/adr/0004)."""

from __future__ import annotations

import datetime
import enum
import uuid
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, SmallInteger, String, Text
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


class TaskStatus(enum.StrEnum):
    todo = "todo"
    in_progress = "in_progress"
    blocked = "blocked"
    done = "done"


class ExecKind(enum.StrEnum):
    human = "human"
    llm_agent = "llm_agent"


class Task(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "tasks"

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="3")
    start_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        SAEnum(TaskStatus, name="task_status", native_enum=True, create_type=False),
        nullable=False,
        server_default="todo",
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
