"""Voice/text/conversation notes (docs/adr/0020, 0021, FR-16).

Capture is offline-first and unmetered; processing (STT/LLM/TTS) is
metered. Raw audio is an S3 ref, never stored in the DB. A
``conversation`` note is a thread of ``note_turns``; (org, project)
scope is the hard isolation boundary (ADR-0007)."""

from __future__ import annotations

import datetime
import enum
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
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


class NoteKind(enum.StrEnum):
    voice = "voice"
    text = "text"
    conversation = "conversation"


class NoteStatus(enum.StrEnum):
    captured = "captured"
    transcribing = "transcribing"
    ready = "ready"
    error = "error"


class TurnRole(enum.StrEnum):
    user = "user"
    assistant = "assistant"


class Note(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "notes"

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    kind: Mapped[NoteKind] = mapped_column(
        SAEnum(NoteKind, name="note_kind", native_enum=True, create_type=False),
        nullable=False,
    )
    status: Mapped[NoteStatus] = mapped_column(
        SAEnum(NoteStatus, name="note_status", native_enum=True, create_type=False),
        nullable=False,
        server_default="captured",
    )
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    audio_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_archived: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class NoteTurn(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "note_turns"
    __table_args__ = (UniqueConstraint("note_id", "ord", name="uq_note_turns_note_id"),)

    note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[TurnRole] = mapped_column(
        SAEnum(TurnRole, name="turn_role", native_enum=True, create_type=False),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
