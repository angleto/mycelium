"""Events / appointments (docs/adr/0008, FR-4): time-pinned, exclusive
per participant (no-ubiquity is enforced in the service layer)."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    PrimaryKeyConstraint,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class Event(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "events"
    __table_args__ = (CheckConstraint("end_at > start_at", name="ck_events_interval"),)

    project_tag_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    client_tag_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    start_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)


class EventParticipant(OrgScopedMixin, Base):
    __tablename__ = "event_participants"
    __table_args__ = (PrimaryKeyConstraint("event_id", "user_id"),)

    event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("events.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
