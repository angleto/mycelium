"""Multi-part notes (parent task c0459c4b, design note 2d228758).

A note is a list of ordered markdown blocks; the previous
``notes.transcript`` is a single implicit part (ord=0) until Phase 6
of the rollout (task 1cd8bc0a) drops the column entirely. The
``NotePart`` model owns the body; ``NotePartUIState`` owns the
per-user collapse state synced cross-device.

Order is the natural axis: every part carries an integer ``ord``,
unique within a note (DEFERRABLE so the SPA can shuffle the whole
sequence in one transaction). ``lang`` is a free-form ISO 639-1
hint (NULL = unspecified) that lights up the side-by-side EN/IT
layout when exactly two parts disagree on language. ``merged_from_note_id``
records provenance after a ``POST /notes/merge`` so the audit trail
survives the source-note soft delete.
"""

from __future__ import annotations

import datetime
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
    func,
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


class NotePart(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "note_part"
    __table_args__ = (
        # DEFERRABLE INITIALLY DEFERRED so a single reorder transaction
        # can swap two ords without forcing the client to step through
        # a "next free" scratchpad ord. Enforced at COMMIT time.
        UniqueConstraint(
            "note_id",
            "ord",
            name="uq_note_part_note_id_ord",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Integer, sparse OK. The service layer normalises (0, 1, 2, ...)
    # on every reorder so reading by ord still gives a tidy sequence.
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # ISO 639-1 (e.g. "en", "it") or longer subtag ("pt-BR"). NULL =
    # unspecified / mixed; the SPA can't yet route retrieval by lang
    # in v1, so a NULL is harmless.
    lang: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Provenance after merge_notes: the source note's id stays here
    # so an audit reader can trace where the body originated even
    # after the source note is soft-deleted.
    merged_from_note_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )


class NotePartUIState(Base):
    """Per-user collapse state. No row = expanded; default behaviour
    materialised lazily on the first toggle. The PK is (user_id,
    part_id) so a user only ever has one state per part; the FK
    cascade on ``part_id`` cleans up when the part disappears."""

    __tablename__ = "note_part_ui_state"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "part_id", name="pk_note_part_ui_state"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    part_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("note_part.id", ondelete="CASCADE"),
        nullable=False,
    )
    collapsed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
