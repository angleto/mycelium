"""Binary attachment on a note OR a task (FR-16 adjacency).

Storage is pluggable (``attachment_store.py``). DEFAULT ``pg``: the
bytes live in the DB ``BYTEA`` (the ``data`` column), atomic with the
row, no external store -- the original single-node co-tenant design.
``s3``: the bytes go to an S3-compatible object store and the row keeps
``data NULL`` + ``storage_key`` set instead. A per-file cap
(``attachment_max_bytes``) is enforced in the service. One row per
file, with the parent FK, filename, mime type and size; the original
is served back as-is and the browser scales image previews (no server
thumbnail). ``storage_key`` is internal, never surfaced to the client.

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

from mycelium_core.models.base import (
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
    # BYTEA: the file content lives in the DB on the ``pg`` backend
    # (legacy rows always do). NULL on the ``s3`` backend, where the
    # bytes are in the object store and ``storage_key`` locates them.
    # NOT loaded by the metadata list query (deferred at the query
    # level, not here, so a single mapping serves both list and read).
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Object-store key when the bytes are off-DB (``s3`` backend); NULL
    # for legacy / ``pg``-backend rows. Internal: never serialised to
    # the client (not a field of AttachmentOut).
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
