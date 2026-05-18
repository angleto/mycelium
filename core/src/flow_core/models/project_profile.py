"""Typed satellite profile for tags of kind ``project`` (docs/adr/0003).
References a client tag; carries billing config."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    VersionMixin,
)


class ProjectProfile(OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "project_profile"

    tag_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    client_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Free description, useful as AI context (docs/adr/0005). The
    # project's colour lives on its tag (tags.color); billable AND the
    # hourly rate are client-level (ClientProfile). Budget stays here.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    budget: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
