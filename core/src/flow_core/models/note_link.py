"""Typed M:N relations between notes, and between notes and tasks
(docs/adr/0029 D3, D4).

Two tables, same shape:

- ``note_note_link`` carries the structural relations among notes
  (atomic-of-index, references, replies_to, supersedes). Underpins
  the Zettelkasten index-note pattern.
- ``note_task_link`` carries the four bidirectional flow relations
  between notes and tasks (subject, artifact, derived_from,
  promoted_from). Replaces the single ``notes.task_id`` FK from
  Proposal A; the legacy column survives until ADR-0029 P3.

``created_by`` is an Identity (ADR-0028): a user or an ai_assistant
can both author a link, symmetrically. NULL only for the pre-0088
backfilled rows (no Identity reconstructible).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base, OrgScopedMixin, UUIDPKMixin

# Closed sets, mirrored at the DB level by CHECK constraints in
# migration 0088.
NOTE_NOTE_LINK_KINDS: frozenset[str] = frozenset(
    {"atom_of", "references", "replies_to", "supersedes"}
)
NOTE_TASK_LINK_KINDS: frozenset[str] = frozenset(
    {"subject", "artifact", "derived_from", "promoted_from"}
)


class NoteNoteLink(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "note_note_link"
    __table_args__ = (
        UniqueConstraint(
            "parent_note_id",
            "child_note_id",
            "kind",
            name="uq_note_note_link",
        ),
        CheckConstraint("parent_note_id <> child_note_id", name="ck_note_note_link_no_self"),
    )

    parent_note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    child_note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NoteTaskLink(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "note_task_link"
    __table_args__ = (UniqueConstraint("note_id", "task_id", "kind", name="uq_note_task_link"),)

    note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
