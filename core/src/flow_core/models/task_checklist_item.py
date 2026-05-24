"""Checklist items attached to a task.

Lightweight ticked items inside a task (not sub-tasks): the SPA shows
the markdown description and the checklist as two tabs in the task
view. The model carries only what a "shopping-list" idiom needs (text,
done, position) plus the standard org/timestamp/version footprint.
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
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
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
