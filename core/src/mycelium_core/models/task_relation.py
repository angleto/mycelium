"""Symmetric "related task" link: a single canonical row per unordered
pair (``task_a_id < task_b_id`` is enforced as a CHECK and as the natural
key). Pure navigation aid, no scheduling semantics (contrast with
``task_dependencies``)."""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class TaskRelation(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "task_relations"
    __table_args__ = (
        UniqueConstraint(
            "task_a_id",
            "task_b_id",
            name="uq_task_relations_pair",
        ),
        CheckConstraint(
            "task_a_id < task_b_id",
            name="ck_task_relations_ordered",
        ),
    )

    task_a_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_b_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
