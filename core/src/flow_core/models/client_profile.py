"""Typed satellite profile for tags of kind ``client`` (docs/adr/0003).
1:1 with a tag; PK is the tag id."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    VersionMixin,
)


class ClientProfile(OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "client_profile"

    tag_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    legal_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Persona fisica: when both set, FatturaPA emits Anagrafica/Nome+Cognome
    # instead of Denominazione (max 60 latin, AnagraficaType choice).
    first_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    vat_number: Mapped[str | None] = mapped_column(String(30), nullable=True)
    tax_code: Mapped[str | None] = mapped_column(String(30), nullable=True)
    address: Mapped[str | None] = mapped_column(String(200), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    province: Mapped[str | None] = mapped_column(String(4), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    sdi_code: Mapped[str | None] = mapped_column(String(7), nullable=True)
    pec: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Per-client invoice sezionale: the series prefix used for this client's
    # invoices (e.g. "ACME" -> ACME/2026/1). Gives each client an independent
    # progressive sequence (numbering is per issuer+series). Auto-derived from
    # the name + made unique within the org on first invoice; user-editable.
    # NULL on legacy clients -> resolved lazily at draft creation.
    invoice_series: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Client-specific payment IBAN: overrides the issuer default, is
    # itself overridden by an explicit per-invoice IBAN (precedence:
    # invoice > client > issuer).
    payment_iban: Mapped[str | None] = mapped_column(String(34), nullable=True)
    # Optional free description; useful as AI context (docs/adr/0005).
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Automatic billing default: a task is billable unless the task
    # overrides it. Lives on the CLIENT (was on the project): billing
    # is a client relationship, not a per-project trait.
    default_billable: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    # Hourly rate is also a client relationship (billed amount =
    # duration_seconds / 3600 * hourly_rate; see time_tracking._rate).
    hourly_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="EUR")
    # Preferred IANA timezone name (e.g. "Europe/Rome"); lets the SPA
    # render this client's time entries / report in its local time.
    timezone: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-client invoice payment defaults. NULL means "inherit": the
    # resolver falls back to the issuer, then to system defaults
    # (TP02 / MP05). Values are SdI enum codes (TPxx / MPxx) validated
    # at the service layer before they reach the XML build.
    default_payment_conditions_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    default_payment_method_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    # Net payment days. NULL = inherit (issuer default or none). When set
    # and an invoice carries no explicit due date, the draft service
    # computes payment_due_date = issued_or_today + days.
    default_payment_terms_days: Mapped[int | None] = mapped_column(nullable=True)
    # Locale for the courtesy PDF (BCP47 tag, e.g. "it", "en"). NULL ->
    # "it". The FatturaPA XML is not translated: SdI ignores the field
    # and the legally mandated purpose/dicitura stay verbatim Italian.
    invoice_language: Mapped[str | None] = mapped_column(String(8), nullable=True)
