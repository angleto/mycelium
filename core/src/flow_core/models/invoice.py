"""Italian electronic invoicing (docs/adr/0009, 0010, 0011, FR-9).

An invoice is immutable after emission (only ``draft`` mutable;
correction is a TD04 credit note linked via ``parent_invoice_id``).
The progressive number per (issuer_profile, series, year) is allocated
concurrency-safe only at draft -> transmitted via ``invoice_counters``
and never reused (it belongs to the cedente, DPR 633/72 art.21).
``identificativo_sdi`` is a first-class indexed column for correlating
SdI receipts to the tenant (ADR-0011).
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
    # Persona fisica: when both set, FatturaPA emits Anagrafica/Nome+Cognome
    # instead of Denominazione (AnagraficaType is a choice; max 60 latin).
    nome: Mapped[str | None] = mapped_column(String(60), nullable=True)
    cognome: Mapped[str | None] = mapped_column(String(60), nullable=True)
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
    # Free-text legal reference for the VAT exemption, emitted in
    # DatiRiepilogo/RiferimentoNormativo for lines carrying a Natura (max 100
    # latin chars, XSD String100LatinType). NULL -> a default for RF19.
    riferimento_normativo: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Contact channels. PEC is printed on the courtesy PDF; the others go
    # in optional CedentePrestatore/Contatti (XSD: Telefono/Fax/Email).
    # All facoltative; none are emitted when NULL.
    pec: Mapped[str | None] = mapped_column(String(320), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    telefono: Mapped[str | None] = mapped_column(String(20), nullable=True)
    fax: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Issuer-level defaults for payment metadata; the resolver falls back
    # to these only when the client (and the invoice itself) carry none.
    # Resolution precedence: invoice > client > issuer > system default
    # (TP02 for CondizioniPagamento, MP05 for ModalitaPagamento).
    default_condizioni_pagamento: Mapped[str | None] = mapped_column(String(4), nullable=True)
    default_modalita_pagamento: Mapped[str | None] = mapped_column(String(4), nullable=True)
    default_payment_terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_default: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    # Recipient CodiceDestinatario AdE assigns at accreditamento Ricezione
    # (7 alphanumeric). When set, this issuer is reachable as cessionario:
    # SdI delivers any incoming FatturaElettronica with this codice to our
    # /sdi/notification endpoint (services/sdi_passive.ingest_passive_invoice).
    # NULL = the issuer is accredited only as transmitter (active cycle).
    codice_destinatario_ricezione: Mapped[str | None] = mapped_column(
        String(7), nullable=True, unique=True
    )
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
    """Progressive invoice numbering, keyed per **issuer profile** (per VAT
    subject), not per org. The progressive number that "la identifichi in modo
    univoco" (DPR 633/72 art.21 c.2) belongs to the cedente/prestatore, so each
    issuer profile owns an independent sequence; an org holding several P.IVA
    keeps them separate (otherwise two VAT subjects would interleave one
    sequence). ``series`` is the sezionale (default "A"): per-client numbering,
    when wanted, is a series-per-client, never a separate counter dimension.
    A profile cannot be deleted while it has invoices (FK RESTRICT on
    ``invoices.issuer_profile_id``), so the profile-id key never restarts a live
    sequence. Allocated FOR UPDATE at transmit; numbers are never reused.

    ``org_id`` is retained NOT as part of the key but only to drive the table's
    RLS policy (``USING/WITH CHECK org_id = current_org``, FORCE RLS): it is
    functionally determined by ``issuer_profile_id`` and must be set on insert
    to the issuer's org so the row passes the tenant check."""

    __tablename__ = "invoice_counters"
    __table_args__ = (
        PrimaryKeyConstraint("issuer_profile_id", "series", "year", name="pk_invoice_counters"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    issuer_profile_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    series: Mapped[str] = mapped_column(String(20), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class Invoice(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        # Number uniqueness is per issuer (the cedente owns its sequence), not
        # per org: two VAT subjects in one org may legitimately share a number.
        UniqueConstraint(
            "issuer_profile_id", "series", "year", "number", name="uq_invoices_issuer"
        ),
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
    # Per-document override of CondizioniPagamento / ModalitaPagamento /
    # GiorniTerminiPagamento. NULL = inherit from the client (then issuer,
    # then system default). Whitelisted to the SdI enums at the service
    # layer; the XML build never sees a value outside the FatturaPA tables.
    condizioni_pagamento: Mapped[str | None] = mapped_column(String(4), nullable=True)
    modalita_pagamento: Mapped[str | None] = mapped_column(String(4), nullable=True)
    payment_terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    """Monotonic sequence for the SdI file name + ProgressivoInvio. These
    must be unique per *trasmittente*, so the key is the trasmittente's fiscal
    id: the accredited channel holder when Flow transmits as intermediary (one
    channel for many tenants, ADR-0011), or the cedente itself on
    self-submission / manual export. Platform-level (NOT OrgScoped, no RLS org
    policy). Allocated FOR UPDATE at transmit, like ``InvoiceCounter``. (The
    column keeps the name ``intermediary_id`` for migration stability.)"""

    __tablename__ = "sdi_transmission_counters"

    intermediary_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_number: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
