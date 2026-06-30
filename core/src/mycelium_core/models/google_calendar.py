"""Google Calendar subscription (epic #125 P1).

A per-user binding between an internal ``working_calendar`` and a remote
Google calendar. ``refresh_token_encrypted`` holds a Fernet envelope
(never plaintext, ADR-0006). Idempotent ingest is enforced by the
service via the natural key ``(subscription_id, external_id)``: an
``Event`` row already grown by the ingest carries ``external_provider``
and ``external_id`` (added on the events table by the same migration).

Org-scoped + RLS like every other tenant table.
"""

from __future__ import annotations

import datetime
import enum
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class GoogleCalendarStatus(enum.StrEnum):
    active = "active"
    error = "error"
    disabled = "disabled"


class CalendarSubscription(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    """A bound (user, working_calendar, google_calendar_id) triple."""

    __tablename__ = "google_calendar_subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    our_calendar_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("working_calendars.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    google_calendar_id: Mapped[str] = mapped_column(String(320), nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[GoogleCalendarStatus] = mapped_column(
        SAEnum(
            GoogleCalendarStatus,
            name="google_calendar_status",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        server_default="active",
    )
    last_sync_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
