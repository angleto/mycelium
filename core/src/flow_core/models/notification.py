"""Notifications, per-user channel prefs, task recurrence (FR-12).

Recurrence instances are independent task rows (no shared state); in
v1 recurrence and dependencies are mutually exclusive (enforced in the
service). Notifications are idempotent per (org, dedupe_key)."""

from __future__ import annotations

import datetime
import enum
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
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


class NotificationChannelKind(enum.StrEnum):
    telegram = "telegram"
    email = "email"


class NotificationStatus(enum.StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class RecurrenceFreq(enum.StrEnum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"
    yearly = "yearly"


class Notification(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("org_id", "dedupe_key", name="uq_notifications_org_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[NotificationChannelKind] = mapped_column(
        SAEnum(
            NotificationChannelKind,
            name="notification_channel",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(
            NotificationStatus,
            name="notification_status",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        server_default="pending",
    )
    sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class NotificationPref(TimestampMixin, VersionMixin, Base):
    __tablename__ = "notification_prefs"
    __table_args__ = (
        PrimaryKeyConstraint("org_id", "user_id", "channel", name="pk_notification_prefs"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[NotificationChannelKind] = mapped_column(
        SAEnum(
            NotificationChannelKind,
            name="notification_channel",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    target: Mapped[str] = mapped_column(String(320), nullable=False, server_default="")


class TaskRecurrence(TimestampMixin, VersionMixin, Base):
    __tablename__ = "task_recurrences"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    freq: Mapped[RecurrenceFreq] = mapped_column(
        SAEnum(RecurrenceFreq, name="recurrence_freq", native_enum=True, create_type=False),
        nullable=False,
    )
    interval: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    next_run: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    until: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_spawned_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
