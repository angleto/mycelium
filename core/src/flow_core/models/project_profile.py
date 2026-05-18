"""Typed satellite profile for tags of kind ``project`` (docs/adr/0003).
References a client tag; carries billing config."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String
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
    # Hourly rate: billed amount = duration_seconds / 3600 * tariffa
    # (see time_tracking._amount).
    tariffa: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    valuta: Mapped[str] = mapped_column(String(3), nullable=False, server_default="EUR")
    # Automatic project property: its tasks are billable unless a task
    # overrides via task.billable.
    default_billable: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    budget: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
