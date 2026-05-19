"""Time tracking (docs/adr/0002, FR-5). A live timer is a row with
``ended_at IS NULL``; stopping it sets ``ended_at`` and
``duration_seconds``. At most one running timer per (org, user) is
guaranteed by a partial unique index (migration 0006). Billing rate is
snapshotted at creation so later rate edits do not rewrite history."""

from __future__ import annotations

import datetime
import enum
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
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
from flow_core.models.task import ExecKind


class TimeSource(enum.StrEnum):
    timer = "timer"
    manual = "manual"


class TimeEntry(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "time_entries"
    __table_args__ = (
        CheckConstraint(
            "ended_at IS NULL OR ended_at > started_at",
            name="ck_time_entries_interval",
        ),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="ck_time_entries_duration",
        ),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[TimeSource] = mapped_column(
        SAEnum(TimeSource, name="time_source", native_enum=True, create_type=False),
        nullable=False,
    )
    # Snapshot of the task's executor at entry time so AI-tracked time
    # is distinguishable and never summed into a human's totals.
    executor_kind: Mapped[ExecKind] = mapped_column(
        SAEnum(ExecKind, name="exec_kind", native_enum=True, create_type=False),
        nullable=False,
        server_default="human",
    )
    # Serial (false): classic single running timer, mutually exclusive.
    # Parallel (true): runs concurrently with others (e.g. LLM tasks).
    parallel: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    billable: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    rate_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="EUR")
    # Free-text memo on the entry. Renamed from ``note`` (migration
    # 0041) so it no longer collides with the Note entity / ``note_id``.
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Provenance: the work note this time was logged in (Proposal A).
    # Billing still rolls up to ``task_id`` (NOT NULL); this is nullable
    # and ``ON DELETE SET NULL`` so deleting the note never deletes
    # billed time, the entry just loses the note link.
    note_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
