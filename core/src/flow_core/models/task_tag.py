"""Task<->tag association (single relation for every tag kind,
docs/adr/0003). org_id carried for RLS."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, PrimaryKeyConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base, OrgScopedMixin


class TaskTag(OrgScopedMixin, Base):
    __tablename__ = "task_tags"
    __table_args__ = (PrimaryKeyConstraint("task_id", "tag_id"),)

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
