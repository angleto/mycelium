"""Italian electronic invoicing (docs/adr/0009, 0010, 0011, FR-9).

An invoice is immutable after emission (only ``draft`` mutable;
correction is a TD04 credit note linked via ``parent_invoice_id``).
The progressive number per (org, series, year) is allocated
concurrency-safe only at draft -> transmitted via ``invoice_counters``
and never reused. ``identificativo_sdi`` is a first-class indexed
column for correlating SdI receipts to the tenant (ADR-0011).
"""

from __future__ import annotations

import datetime
import enum
import uuid
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
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


class ConservationAdhesion(enum.StrEnum):
    none = "none"
    requested = "requested"
    active = "active"


class InvoiceKind(enum.StrEnum):
    invoice = "invoice"
    credit_note = "credit_note"


class DocumentType(enum.StrEnum):
    TD01 = "TD01"
    TD04 = "TD04"


class InvoiceState(enum.StrEnum):
    draft = "draft"
    transmitted = "transmitted"
    delivered = "delivered"
    accepted = "accepted"
    rejected = "rejected"


class SdiStatus(enum.StrEnum):
    none = "none"
    RC = "RC"  # ricevuta di consegna
    MC = "MC"  # mancata consegna
    NS = "NS"  # notifica di scarto
    AT = "AT"  # attestazione trasmissione (impossibilita recapito)


class PaymentStatus(enum.StrEnum):
    unpaid = "unpaid"
    paid = "paid"


class ConservationStatus(enum.StrEnum):
    out_of_coverage = "out_of_coverage"
    ade_pending = "ade_pending"
    ade_covered = "ade_covered"


class IssuerProfile(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    """A billing identity the org issues invoices under (the invoice
    "intestazione"). An org can hold several (e.g. ditta individuale vs
    a controlled SRL); exactly one is the default (partial unique index
    ``uq_issuer_profiles_default``), pre-selected at draft creation. AdE
    free-conservation adhesion is per identity (it is per P.IVA), so it
    lives here, not org-wide. An emitted invoice's header is frozen in
    ``Invoice.xml`` at transmit, so editing/removing a profile later
    never mutates an already-emitted document (ADR-0009)."""

    __tablename__ = "issuer_profiles"

    label: Mapped[str] = mapped_column(String(120), nullable=False, server_default="Principale")
    regime_fiscale: Mapped[str] = mapped_column(String(4), nullable=False, server_default="RF01")
    paese: Mapped[str] = mapped_column(String(2), nullable=False, server_default="IT")
    piva: Mapped[str | None] = mapped_column(String(28), nullable=True)
    codice_fiscale: Mapped[str | None] = mapped_column(String(16), nullable=True)
    denominazione: Mapped[str] = mapped_column(String(200), nullable=False)
    indirizzo: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    cap: Mapped[str] = mapped_column(String(10), nullable=False, server_default="")
    comune: Mapped[str] = mapped_column(String(120), nullable=False, server_default="")
    provincia: Mapped[str | None] = mapped_column(String(4), nullable=True)
    nazione: Mapped[str] = mapped_column(String(2), nullable=False, server_default="IT")
    rea: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Fallback payment IBAN, used by an invoice when neither the invoice
    # nor the client carries one (IBAN precedence: invoice > client >
    # issuer). NULL until the issuer sets one.
    default_iban: Mapped[str | None] = mapped_column(String(34), nullable=True)
    is_default: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    conservation_adhesion: Mapped[ConservationAdhesion] = mapped_column(
        SAEnum(
            ConservationAdhesion,
            name="conservation_adhesion",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        server_default="none",
    )


class InvoiceCounter(Base):
    __tablename__ = "invoice_counters"
    __table_args__ = (PrimaryKeyConstraint("org_id", "series", "year", name="pk_invoice_counters"),)

    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    series: Mapped[str] = mapped_column(String(20), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class Invoice(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("org_id", "series", "year", "number", name="uq_invoices_org_id"),
    )

    client_tag_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    issuer_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("issuer_profiles.id", ondelete="RESTRICT"),
        nullable=True,
    )
    kind: Mapped[InvoiceKind] = mapped_column(
        SAEnum(InvoiceKind, name="invoice_kind", native_enum=True, create_type=False),
        nullable=False,
        server_default="invoice",
    )
    document_type: Mapped[DocumentType] = mapped_column(
        SAEnum(DocumentType, name="document_type", native_enum=True, create_type=False),
        nullable=False,
        server_default="TD01",
    )
    parent_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=True,
    )
    series: Mapped[str] = mapped_column(String(20), nullable=False, server_default="A")
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[InvoiceState] = mapped_column(
        SAEnum(InvoiceState, name="invoice_state", native_enum=True, create_type=False),
        nullable=False,
        server_default="draft",
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="EUR")
    causale: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_iban: Mapped[str | None] = mapped_column(String(34), nullable=True)
    payment_due_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    taxable: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default="0")
    vat: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default="0")
    # Virtual stamp duty (imposta di bollo): EUR 2.00 on a forfettario
    # invoice whose taxable >= 77.47, else 0. Persisted with the totals;
    # included in ``total`` and in ImportoTotaleDocumento.
    bollo: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default="0")
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default="0")
    identificativo_sdi: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sdi_status: Mapped[SdiStatus] = mapped_column(
        SAEnum(SdiStatus, name="sdi_status", native_enum=True, create_type=False),
        nullable=False,
        server_default="none",
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, name="payment_status", native_enum=True, create_type=False),
        nullable=False,
        server_default="unpaid",
    )
    conservation_status: Mapped[ConservationStatus] = mapped_column(
        SAEnum(
            ConservationStatus,
            name="conservation_status",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        server_default="out_of_coverage",
    )
    xml: Mapped[str | None] = mapped_column(Text, nullable=True)
    issued_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class InvoiceLine(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (
        UniqueConstraint("invoice_id", "line_no", name="uq_invoice_lines_invoice_id"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, server_default="1")
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default="22")
    natura: Mapped[str | None] = mapped_column(String(4), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SdiTransmissionCounter(Base):
    """Per-intermediary monotonic sequence for the SdI file name +
    ProgressivoInvio. These must be unique per *trasmittente*, and one
    accredited channel transmits for many tenants (ADR-0011), so the
    sequence is platform-level (keyed by the intermediary id_codice), not
    per-org: NOT OrgScoped, no RLS org policy. Allocated FOR UPDATE at
    transmit, like ``InvoiceCounter``."""

    __tablename__ = "sdi_transmission_counters"

    intermediary_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_number: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
