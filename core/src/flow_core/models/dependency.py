"""Typed task dependency (docs/adr/0004): FS/SS/FF/SF + working-time
lag/lead. The set forms a DAG; cycle detection is in the service
layer. CPM scheduling consumes these in F3."""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class DependencyType(enum.StrEnum):
    FS = "FS"
    SS = "SS"
    FF = "FF"
    SF = "SF"


class TaskDependency(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "predecessor_id",
            "successor_id",
            "type",
            name="uq_task_dependencies_predecessor_id",
        ),
        CheckConstraint(
            "predecessor_id <> successor_id",
            name="ck_task_dependencies_no_self",
        ),
    )

    predecessor_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    successor_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[DependencyType] = mapped_column(
        SAEnum(
            DependencyType,
            name="dependency_type",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )
    lag_working_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
