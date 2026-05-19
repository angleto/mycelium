"""Unified tag (docs/adr/0003): one concept with a ``kind``; client
and project carry typed satellite profiles (separate tables)."""

from __future__ import annotations

import enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class TagKind(enum.StrEnum):
    generic = "generic"
    client = "client"
    project = "project"
    # A memory "channel": an orthogonal facet for grouping memory blobs
    # (e.g. an agent's working set). Created/listed via the generic tag
    # endpoints; member-level like ``generic`` (see taxonomy.create_tag).
    memory_channel = "memory_channel"


class Tag(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("org_id", "kind", "name"),)

    kind: Mapped[TagKind] = mapped_column(
        SAEnum(TagKind, name="tag_kind", native_enum=True, create_type=False),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    # Stable slug for a ``memory_channel`` tag (e.g. "email", "telegram",
    # "manual", "agent"): the DETERMINISTIC target an integration writes
    # into. NULL for every non-channel tag and for an admin-added custom
    # channel with no integration binding. A partial unique index keeps
    # it unique per (org_id, system_key) only where it is set
    # (migration 0042). Enable/disable reuses ``status`` (active vs
    # archived), no separate flag.
    system_key: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
