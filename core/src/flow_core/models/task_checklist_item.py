"""Checklist items attached to a task OR a note (polymorphic owner).

Lightweight ticked items inside a task or a note (not sub-tasks): the
SPA shows the markdown description and the checklist as two tabs in the
task / note view, via one shared widget. The owner is exactly one of
``task_id`` / ``note_id`` (XOR check). Beyond the "shopping-list" core
(text, done, position) an item may carry an optional ``body``: an
articulate markdown comment, edited / opened as markdown in the widget
(task bae178d2). The legacy table name ``task_checklist_items`` is kept
(it predates the note owner); it is no longer task-only.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
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


class TaskChecklistItem(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "task_checklist_items"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(text)) > 0",
            name="ck_task_checklist_items_text_nonempty",
        ),
        # Exactly one owner: task XOR note (migration 0020).
        CheckConstraint(
            "(task_id IS NULL) <> (note_id IS NULL)",
            name="ck_task_checklist_items_owner_xor",
        ),
    )

    # Polymorphic owner: exactly one of task_id / note_id is set.
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    note_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional articulate markdown comment for the item (bae178d2).
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    done: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Sparse integer position with gap-based ordering. Reorder is a
    # bulk re-write (small N: a task's checklist is bounded by UX, not
    # by a hard cap); we don't need LexoRank here.
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    done_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    done_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
