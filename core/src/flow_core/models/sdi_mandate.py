"""SdI transmission mandate (docs/adr/0011, FR-9).

The authorization a VAT subject (an ``IssuerProfile``) grants Flow, acting
as accredited intermediary, to transmit its invoices through the single
shared SdICoop channel. Per VAT subject (per P.IVA), not per Org: one Org
may hold several issuer profiles (distinct VAT subjects), each authorizing
transmission independently -- the same per-identity granularity as the
issuer's ``conservation_adhesion``. A grant inserts an ``active`` row; a
revoke flips it to ``revoked`` and stamps ``revoked_at`` (append-only,
audited). At most one active mandate per issuer profile.
"""

from __future__ import annotations

import datetime
import enum
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
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


class SdiMandateStatus(enum.StrEnum):
    active = "active"
    revoked = "revoked"


class SdiMandate(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "sdi_mandates"
    __table_args__ = (
        # At most one active mandate per VAT subject (partial unique index;
        # mirrors the migration's uq_sdi_mandates_active).
        Index(
            "uq_sdi_mandates_active",
            "issuer_profile_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    issuer_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("issuer_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[SdiMandateStatus] = mapped_column(
        SAEnum(SdiMandateStatus, name="sdi_mandate_status", native_enum=True, create_type=False),
        nullable=False,
        server_default="active",
    )
    # Coarse scope tag; "transmit" today (active cycle). Reserved for
    # future scopes (e.g. passive cycle) without a schema change.
    scope: Mapped[str] = mapped_column(String(40), nullable=False, server_default="transmit")
    # Free-text pointer to the signed authorization the tenant gave Flow.
    reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    granted_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
