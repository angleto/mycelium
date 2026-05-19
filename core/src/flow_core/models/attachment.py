"""Binary attachment on a note OR a task (FR-16 adjacency).

Stored as DB ``BYTEA`` (the ``data`` column): no filesystem / object
store, which fits the single-node co-tenant deploy and keeps erasure /
backup atomic with the row. A per-file cap (``attachment_max_bytes``)
is enforced in the service. One row per file, with the parent FK,
filename, mime type and size; the original is served back as-is and
the browser scales image previews (no server thumbnail).

Exactly one of ``note_id`` / ``task_id`` is non-null (a table CHECK
constraint enforces it): an attachment belongs to a single parent.
Both parent FKs are ``ON DELETE CASCADE`` so deleting the note/task
removes its attachments. Org-scoped + RLS like every tenant table
(``OrgScopedMixin``); the migration adds the policy + flow_app grants
exactly like ``note_tags`` / ``blob_sources``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
)


class Attachment(UUIDPKMixin, OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "attachments"
    __table_args__ = (
        # Exactly one parent: a note attachment XOR a task attachment.
        CheckConstraint(
            "(note_id IS NOT NULL AND task_id IS NULL) "
            "OR (note_id IS NULL AND task_id IS NOT NULL)",
            name="one_parent",
        ),
    )

    note_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(160), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # BYTEA: the file content lives in the DB (ADR co-tenant deploy).
    # NOT loaded by the metadata list query (deferred at the query
    # level, not here, so a single mapping serves both list and read).
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
