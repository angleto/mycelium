"""Received invoices (passive SdI cycle, docs/adr/0011 post-v1 anticipated).

When the cedente lists our channel's CodiceDestinatario, SdI delivers the
``FatturaElettronica`` to our always-on inbound. This table stores the raw
XML + structured metadata for each received invoice. Org resolution
mirrors the active cycle (sdi_resolve_invoice_org / 0074): a SECURITY
DEFINER ``sdi_resolve_recipient_org`` returns the org_id from the
``IssuerProfile.codice_destinatario_ricezione`` -> the insert runs under a
normal tenant_session so the write stays RLS-scoped.

Status machine is intentionally minimal (``new`` only) -- downstream is
not built yet; ADR-0011 v1 keeps passive cycle deferred. ``new`` rows are
ready for a future worker pipeline (classify, notify the user, build
EsitoCommittente if PA, ...).
"""

from __future__ import annotations

import datetime
import enum
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, text
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


class CommittenteVerdict(enum.StrEnum):
    none = "none"
    accepted = "accepted"
    rejected = "rejected"
    deemed_accepted = "deemed_accepted"  # DT timeout on receiver side


class ReceivedInvoice(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    """A FatturaElettronica delivered to one of our IssuerProfile's
    codice destinatario via the accredited channel. Raw XML is kept as
    bytes (the originating cedente signs / Flow stores verbatim, never
    mutates). ``identificativo_sdi`` is globally unique on the channel,
    so a SdI retry of the same delivery is idempotent on the unique
    index (the insert no-ops via ON CONFLICT in the service)."""

    __tablename__ = "received_invoices"
    __table_args__ = (
        Index(
            "uq_received_invoices_idsdi",
            "identificativo_sdi",
            unique=True,
        ),
    )

    issuer_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("issuer_profiles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    identificativo_sdi: Mapped[str] = mapped_column(String(64), nullable=False)
    nome_file: Mapped[str] = mapped_column(String(120), nullable=False)
    formato_trasmissione: Mapped[str] = mapped_column(String(8), nullable=False)
    sender_id_paese: Mapped[str] = mapped_column(String(2), nullable=False)
    sender_id_codice: Mapped[str] = mapped_column(String(28), nullable=False)
    sender_denominazione: Mapped[str | None] = mapped_column(String(200), nullable=True)
    codice_destinatario: Mapped[str] = mapped_column(String(7), nullable=False)
    raw_xml: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    processing_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'new'")
    )
    # Denormalized committente outcome (EC sent / SE / DT receiver-side).
    # Full audit log lives in ``received_invoice_notifications``.
    committente_verdict: Mapped[CommittenteVerdict] = mapped_column(
        SAEnum(
            CommittenteVerdict,
            name="committente_verdict",
            native_enum=False,
            create_constraint=False,
            length=20,
        ),
        nullable=False,
        server_default="none",
    )
    committente_verdict_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dt_received_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
