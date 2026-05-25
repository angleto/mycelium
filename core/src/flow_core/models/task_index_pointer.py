"""Pointer linking a task to its 1:1 searchable memory blob.

Lets the existing memory pipeline (``memory_blobs``: FTS + pgvector +
RRF) carry task search without a parallel index. The pointer is
maintained by the SQLAlchemy event listener in ``services.task_search``:
INSERT/UPDATE on a task or its checklist items enqueues a resync in
``session.info`` and the ``before_commit`` hook upserts the blob; a
``content_hash`` over the rendered text skips metadata-only mutations
(state/priority/due date don't change ``text``, so no re-embed).

FK direction is asymmetric: ``task_id -> tasks(id)`` is a simple FK
(``tasks`` is not partitioned); ``(blob_id, org_id) -> memory_blobs``
is composite because ``memory_blobs`` is PARTITION BY HASH (org_id).
``UNIQUE(blob_id)`` keeps the binding strictly 1:1.
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


class TaskIndexPointer(Base):
    __tablename__ = "task_index_pointer"
    __table_args__ = (
        PrimaryKeyConstraint("task_id", name="pk_task_index_pointer"),
        UniqueConstraint("blob_id", name="uq_task_index_pointer_blob_id"),
        ForeignKeyConstraint(
            ["blob_id", "org_id"],
            ["memory_blobs.id", "memory_blobs.org_id"],
            ondelete="CASCADE",
            name="fk_task_index_pointer_blob_id_memory_blobs",
        ),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
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
