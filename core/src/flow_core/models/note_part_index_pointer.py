"""Pointer linking a note part to its 1:1 searchable memory blob.

The note-part analogue of ``task_index_pointer`` (see that model for the
shared rationale). Decision 2026-06-09: notes are indexed PER PART (one
blob per ``note_part``) so each part re-embeds independently when its
body changes. The pointer is maintained by the SQLAlchemy event
listeners in ``services.note_search`` (ORM create/update of a part) plus
explicit ``mark_note_part_dirty`` calls on the Core-update paths
(append/prepend/replace/update via ``optimistic_update``).

``note_id`` is carried denormalised so the unified search can resolve a
part blob back to its note without a second join, and so a note
hard-delete cascades to the pointer directly.

FK direction mirrors ``task_index_pointer``: ``part_id -> note_part(id)``
and ``note_id -> notes(id)`` are simple FKs (neither is partitioned);
``(blob_id, org_id) -> memory_blobs`` is composite because
``memory_blobs`` is PARTITION BY HASH (org_id). ``UNIQUE(blob_id)`` keeps
the binding strictly 1:1.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base


class NotePartIndexPointer(Base):
    __tablename__ = "note_part_index_pointer"
    __table_args__ = (
        PrimaryKeyConstraint("part_id", name="pk_note_part_index_pointer"),
        UniqueConstraint("blob_id", name="uq_note_part_index_pointer_blob_id"),
        ForeignKeyConstraint(
            ["blob_id", "org_id"],
            ["memory_blobs.id", "memory_blobs.org_id"],
            ondelete="CASCADE",
            name="fk_note_part_index_pointer_blob_id_memory_blobs",
        ),
    )

    part_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("note_part.id", ondelete="CASCADE"),
        nullable=False,
    )
    note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    blob_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
