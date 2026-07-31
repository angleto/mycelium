"""Typed satellite profile for tags of kind ``project`` (docs/adr/0003).
References a client tag; carries billing config."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Numeric, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    VersionMixin,
)
from mycelium_core.models.tag import TagKind


class ProjectProfile(OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "project_profile"
    # Migration 0086. The two constant ``*_kind`` columns exist only as
    # the second leg of a composite FK into ``tags(id, kind)``: they turn
    # "this satellite hangs off a PROJECT tag and points at a CLIENT tag"
    # into a declarative fact instead of a trigger. ``tags.kind`` is
    # immutable at the service layer (taxonomy.update_tag writes only
    # name / color / status), so no ON UPDATE CASCADE is needed.
    __table_args__ = (
        CheckConstraint("tag_kind = 'project'", name="tag_kind"),
        CheckConstraint("client_kind = 'client'", name="client_kind"),
        ForeignKeyConstraint(
            ["tag_id", "tag_kind"],
            ["tags.id", "tags.kind"],
            ondelete="CASCADE",
            name="fk_project_profile_tag_kind",
        ),
        # NO ACTION DEFERRABLE, never RESTRICT: delete_organization
        # (0001_baseline.sql:620-658) is a single DELETE FROM
        # organizations that relies on CASCADE reaching both ``tags``
        # and ``project_profile``, and RESTRICT is checked BEFORE
        # sibling cascade actions run.
        ForeignKeyConstraint(
            ["client_tag_id", "client_kind"],
            ["tags.id", "tags.kind"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
            name="fk_project_profile_client_kind",
        ),
    )

    tag_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Invariant (d), migration 0086: every project has exactly one
    # client. The pointer used to be nullable with ON DELETE SET NULL,
    # which is mutually exclusive with NOT NULL; a client is removed
    # through ``taxonomy.purge_client``, which purges its projects
    # first, so the pointer never has to be blanked in place.
    client_tag_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED"),
        nullable=False,
        index=True,
    )
    tag_kind: Mapped[TagKind] = mapped_column(
        SAEnum(TagKind, name="tag_kind", native_enum=True, create_type=False),
        nullable=False,
        server_default="project",
    )
    client_kind: Mapped[TagKind] = mapped_column(
        SAEnum(TagKind, name="tag_kind", native_enum=True, create_type=False),
        nullable=False,
        server_default="client",
    )
    # Free description, useful as AI context (docs/adr/0005). The
    # project's colour lives on its tag (tags.color); billable AND the
    # hourly rate are client-level (ClientProfile). Budget stays here.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    budget: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
