"""Inbound payment-provider connectors: Stripe events -> FatturaPA documents
(ADR-0051).

Four org-scoped tables. The direction is the mirror image of ADR-0047: there we
PUSH signed events to an integrator, here a payment provider PUSHES events to
us and we turn them into fiscal documents. Nothing in this subsystem keeps
in-process state, so N API replicas and N workers are interchangeable:
authentication is a stateless MAC over the request body, dedup is a UNIQUE
constraint, and the work queue is claimed with ``FOR UPDATE SKIP LOCKED`` under
an expiring column lease.

- :class:`PaymentConnector` -- the per-issuer-profile configuration. Holds the
  provider's webhook signing secret as a REVERSIBLE Fernet envelope (we must
  recompute a MAC we did not generate, exactly like WebhookEndpoint) and an
  OPTIONAL extra inbound API key as a one-way peppered hash (we only ever
  compare it, so it follows the ADR-0045 credential-at-rest pattern, not the
  envelope one). Both rotate with a grace window so a rotation never drops an
  in-flight redelivery. Unlike its siblings this table is ENABLE-but-NOT-FORCE
  RLS: the inbound request arrives with no tenant context and is resolved by the
  SECURITY DEFINER ``resolve_payment_connector`` function, the same shape
  migration 0077 used for ``authenticate_issuer_api_key``.
- :class:`PaymentConnectorEvent` -- one row per provider event, the durable
  ingress ledger AND the work queue. The HTTP handler does nothing but verify,
  insert and answer 2xx; every fiscal decision happens later in the worker, so a
  slow SdI dispatch can never blow the provider's webhook timeout. ``payload``
  is the raw event, frozen, so a reprocess is deterministic.
- :class:`PaymentObjectLink` -- provider object id -> the document we emitted
  for it. This is what makes emission idempotent across event types (an
  ``invoice.paid`` and a ``charge.succeeded`` naming the same money resolve to
  the same invoice) and what lets a refund find its parent.
- :class:`PaymentCustomerLink` -- provider customer id -> the client tag. The
  connector's own identity map, because ``taxonomy.resolve_or_create_client``
  dedupes on fiscal id with a SELECT-then-INSERT that is not race-safe under
  concurrent redelivery.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)

# --- closed vocabularies ---------------------------------------------------


def _sql_in(values: tuple[str, ...]) -> str:
    """Render a closed vocabulary as a SQL ``IN`` list.

    Not ``repr()``: a one-element Python tuple reprs as ``('stripe',)`` and the
    trailing comma is a syntax error in Postgres.
    """
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


#: Providers with a mapper. Widening this set is a migration (the CHECK) plus a
#: new mapper module; the service dispatches on this column.
#:
#: ``mycelium`` is not a vendor: it is our OWN documented event contract
#: (docs/payment-connector-contract.md), so a sender we have no adapter for can
#: integrate by implementing a published format instead of waiting for one. The
#: vendor adapters are then a convenience, not the only door.
PROVIDERS = ("stripe", "mycelium")

#: What the connector is allowed to do on its own when an event arrives.
#: ``transmit`` composes AND files with SdI, ``draft`` composes and stops (an
#: operator reviews and transmits from the SPA), ``off`` records the event and
#: parks it for a fully manual decision. Emission and credit notes carry the
#: switch independently: automating invoices while keeping storni manual is a
#: legitimate and common posture.
AUTOMATION_MODES = ("transmit", "draft", "off")

#: The single event family that triggers an emission. Exactly one per connector:
#: Stripe fires several events for the same money (an ``invoice.paid`` is also a
#: ``charge.succeeded``), so a set here would double-invoice.
EMISSION_EVENTS = (
    "invoice.paid",
    "payment_intent.succeeded",
    "checkout.session.completed",
)

#: Lifecycle of an ingested event.
#: ``pending`` due for a processing attempt; ``processing`` a worker holds the
#: lease; ``done`` produced its document (or was a settled no-op); ``ignored``
#: recognised but not actionable under this connector's configuration;
#: ``dead`` exhausted its attempts.
#:
#: The two parked states are deliberately DISTINCT, because they mean opposite
#: things about whose move it is:
#:
#: - ``no_billing_data`` -- the counterpart never supplied a complete billing
#:   block. Nothing is broken and there is nothing for an operator to decide;
#:   the customer simply has not filled the form in. These re-arm THEMSELVES the
#:   moment a customer event arrives carrying the missing data, so they are a
#:   waiting room, not a queue.
#: - ``needs_attention`` -- a genuine operational decision is pending (the
#:   automation is switched to manual, the parent invoice was scartato, the
#:   payload cannot be parsed). A human has to act.
#:
#: Collapsing them, as an earlier revision did, buries the handful of events
#: that need a person under the many that only need a customer to get round to
#: it -- which is exactly how an operational queue stops being read.
EVENT_STATUSES = (
    "pending",
    "processing",
    "done",
    "ignored",
    "no_billing_data",
    "needs_attention",
    "dead",
)

#: Outcome of one inbound HTTP delivery attempt. ``accepted`` and ``duplicate``
#: are the two success shapes (a redelivery IS a success for the sender);
#: everything else is a refusal, and the row is the evidence of it.
DELIVERY_OUTCOMES = (
    "accepted",
    "duplicate",
    "signature_invalid",
    "disabled",
    "payload_invalid",
    "too_large",
)

#: Provider object kinds we key emission idempotency on.
OBJECT_KINDS = (
    "invoice",
    "payment_intent",
    "checkout_session",
    "charge",
    "credit_note",
    "refund",
)


class PaymentConnector(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "payment_connectors"
    __table_args__ = (
        CheckConstraint(f"provider IN {_sql_in(PROVIDERS)}", name="ck_payment_connectors_provider"),
        CheckConstraint(
            f"invoice_mode IN {_sql_in(AUTOMATION_MODES)}",
            name="ck_payment_connectors_invoice_mode",
        ),
        CheckConstraint(
            f"credit_note_mode IN {_sql_in(AUTOMATION_MODES)}",
            name="ck_payment_connectors_credit_note_mode",
        ),
        CheckConstraint(
            f"emission_event IN {_sql_in(EMISSION_EVENTS)}",
            name="ck_payment_connectors_emission_event",
        ),
        CheckConstraint(
            "length(label) >= 1 AND length(label) <= 120", name="ck_payment_connectors_label_len"
        ),
        UniqueConstraint("issuer_profile_id", "label", name="uq_payment_connectors_label"),
        Index("ix_payment_connectors_issuer_profile_id", "issuer_profile_id"),
    )

    issuer_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("issuer_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False, server_default="stripe")
    #: Free label; also the natural key inside an issuer profile, so "live" and
    #: "test" Stripe accounts can coexist on the same cedente.
    label: Mapped[str] = mapped_column(String(120), nullable=False)

    # --- credentials -------------------------------------------------------
    #: Fernet ciphertext of the provider's webhook signing secret (Stripe
    #: ``whsec_...``). Reversible because verification recomputes the MAC.
    signing_secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    previous_signing_secret_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_signing_secret_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: OPTIONAL second factor on the ingress: a Mycelium-minted key the caller
    #: echoes in ``X-Connector-Api-Key``. NULL = not required. One-way peppered
    #: hash (we never need the plaintext back), shown once at mint/rotate.
    api_key_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    previous_api_key_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    previous_api_key_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- automation switches ----------------------------------------------
    enabled: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    invoice_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="transmit")
    credit_note_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="transmit"
    )
    emission_event: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="invoice.paid"
    )
    #: Mirror a provider "payment succeeded" signal onto ``payment_status``.
    #: Independent of emission: with ``invoice_mode='draft'`` the document is
    #: composed by the connector and marked paid by the same feed.
    payment_sync_enabled: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))

    # --- invoice defaults, complementary to what the provider sends --------
    #: Sezionale for provider-originated documents; NULL keeps the per-client
    #: series ``create_draft`` derives.
    series: Mapped[str | None] = mapped_column(String(20), nullable=True)
    default_purpose: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Used when the provider reports no tax on a line. NULL falls through to
    #: the issuer regime default resolved by ``invoice.add_line``.
    default_vat_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    default_vat_nature: Mapped[str | None] = mapped_column(String(4), nullable=True)
    default_line_description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: How to read a provider amount that carries no explicit tax breakdown:
    #: True = the figure already contains VAT, False = VAT is added on top.
    #: Getting this wrong is a systematically wrong invoice, so it is an
    #: explicit switch rather than a heuristic.
    amounts_include_vat: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    default_payment_conditions_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    #: MP08 (carta di pagamento) is the honest default for a card processor, but
    #: DatiPagamento is only emitted when the invoice carries it, so it stays
    #: opt-in per connector.
    default_payment_method_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    #: Fallbacks for a counterpart the provider describes incompletely.
    default_country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # There is deliberately NO default codice destinatario. ``0000000`` cannot
    # be used to actually deliver a document, so a connector-wide default for it
    # would only make an invoice LOOK emittable while producing something that
    # cannot be sent. A recipient is addressable when the counterpart supplied a
    # real codice destinatario or a PEC -- or, for a non-Italian counterpart,
    # by the FatturaPA rule that its code is ``XXXXXXX`` (see
    # ``FOREIGN_SDI_CODE``), which is prescribed rather than defaulted.

    # --- provider metadata field mapping ----------------------------------
    #: Stripe carries no FatturaPA fields natively, so the fiscal identity of a
    #: counterpart travels in customer/invoice ``metadata``. Each field is an
    #: ORDERED LIST of candidate key names, not a single name: real accounts
    #: accumulate several spellings for one field (a migration away from another
    #: e-invoicing provider leaves its keys behind, a manual entry path adds a
    #: capitalised variant with a space). First key present wins, so the list
    #: doubles as a precedence order -- current spelling first, legacy ones as a
    #: tail for records nobody has re-saved since.
    metadata_vat_keys: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        server_default=text("'{vatId,vat_number,partita_iva}'::text[]"),
    )
    metadata_tax_code_keys: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        server_default=text("'{fiscal_code,tax_code,codice_fiscale}'::text[]"),
    )
    metadata_sdi_keys: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        server_default=text("'{codice_destinatario,sdi_code,sdi}'::text[]"),
    )
    metadata_pec_keys: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default=text("'{pec}'::text[]")
    )

    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_event_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PaymentConnectorEvent(UUIDPKMixin, OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "payment_connector_events"
    __table_args__ = (
        # The whole at-least-once story: a provider redelivery, a retry after a
        # network blip, two replicas racing the same POST -- all collapse here.
        UniqueConstraint(
            "connector_id", "provider_event_id", name="uq_payment_connector_events_dedupe"
        ),
        CheckConstraint(
            f"status IN {_sql_in(EVENT_STATUSES)}", name="ck_payment_connector_events_status"
        ),
        # The drain query.
        Index(
            "ix_payment_connector_events_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # The lease-reclaim query.
        Index(
            "ix_payment_connector_events_processing",
            "last_attempt_at",
            postgresql_where=text("status = 'processing'"),
        ),
        # The operator's queue: only what a human must act on.
        Index(
            "ix_payment_connector_events_attention",
            "connector_id",
            "created_at",
            postgresql_where=text("status IN ('needs_attention','dead')"),
        ),
        # The waiting room, keyed by customer: the re-arm sweep looks up exactly
        # the events blocked on one customer's missing data.
        Index(
            "ix_payment_connector_events_awaiting",
            "connector_id",
            "provider_customer_id",
            postgresql_where=text("status = 'no_billing_data'"),
        ),
        Index("ix_payment_connector_events_connector", "connector_id", "created_at"),
    )

    connector_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payment_connectors.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The provider's own event id (Stripe ``evt_...``). Opaque to us.
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    #: The raw event exactly as received, frozen. Reprocessing is deterministic
    #: and never re-reads the provider.
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    #: The provider's own timestamp, when it sends one.
    occurred_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_attempt_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Stable slug (ADR-0017), never free prose: it is read by the SPA and by
    #: the retry decision, and it is what an operator triages on.
    last_error: Mapped[str | None] = mapped_column(String(160), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: The provider customer this event is about, recorded when the event is
    #: parked. It is what lets a later customer event re-arm exactly the
    #: payments that were waiting on THAT customer's data, instead of retrying
    #: every parked event on the connector.
    provider_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The document this event produced, once it produced one.
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="SET NULL"),
        nullable=True,
    )


class PaymentObjectLink(UUIDPKMixin, OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "payment_object_links"
    __table_args__ = (
        # Emission idempotency AND the refund -> parent resolver. Claimed with
        # INSERT ... ON CONFLICT DO NOTHING before the document is filed, so a
        # crash between the link and the dispatch resumes instead of re-emitting.
        UniqueConstraint(
            "connector_id", "object_kind", "object_id", name="uq_payment_object_links_object"
        ),
        CheckConstraint(
            f"object_kind IN {_sql_in(OBJECT_KINDS)}", name="ck_payment_object_links_kind"
        ),
        Index("ix_payment_object_links_invoice", "invoice_id"),
    )

    connector_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payment_connectors.id", ondelete="CASCADE"),
        nullable=False,
    )
    object_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: RESTRICT, not SET NULL: this row is what stops a second emission for the
    #: same money, so it must not outlive its invoice as a dangling claim.
    #: (An invoice is never hard-deleted anyway -- purge_client refuses one.)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
    )


class PaymentWebhookDelivery(UUIDPKMixin, OrgScopedMixin, TimestampMixin, Base):
    """One row per inbound HTTP delivery attempt, accepted or refused.

    ``payment_connector_events`` records what we AGREED to act on;
    this records what actually arrived. In a fiscal system the difference
    matters: "Stripe says it sent it and we have no invoice" has to be
    answerable from the database, and without this table a refused delivery
    left nothing behind but a log line on whichever pod happened to serve it.

    The body itself is NOT stored. For an accepted event the frozen payload is
    already on the event row, and for a REFUSED one the bytes are unauthenticated
    attacker-controlled data that we would be persisting on their say-so. The
    SHA-256 digest keeps the row verifiable -- anyone holding the original body
    can prove it produced this row -- at none of that cost.
    """

    __tablename__ = "payment_webhook_deliveries"
    __table_args__ = (
        CheckConstraint(
            f"outcome IN {_sql_in(DELIVERY_OUTCOMES)}", name="ck_payment_webhook_deliveries_outcome"
        ),
        Index("ix_payment_webhook_deliveries_connector", "connector_id", "received_at"),
        # The security view: everything that did not get through.
        Index(
            "ix_payment_webhook_deliveries_refused",
            "connector_id",
            "received_at",
            postgresql_where=text("outcome NOT IN ('accepted','duplicate')"),
        ),
    )

    connector_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payment_connectors.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The event this delivery produced, when it produced one. SET NULL so a
    #: retention sweep over events cannot delete the delivery evidence.
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payment_connector_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: Recorded even when the event was refused, so a sender's "I sent evt_X"
    #: can be answered without them holding the body.
    provider_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body_bytes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    body_sha256: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    signature_present: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    api_key_present: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class PaymentCustomerLink(UUIDPKMixin, OrgScopedMixin, TimestampMixin, Base):
    """Provider customer id -> client tag. A pure identity map, not a cache.

    It deliberately holds NO fiscal data. The counterpart's VAT number, codice
    destinatario, PEC and address live in ``client_profile``, which is the org's
    real anagrafica: the record the FatturaPA is built from, that an operator can
    edit, that carries RLS, versioning, audit and revisions. A second copy here
    would be a second truth to reconcile, and the first time the two disagreed
    the invoice would be built from whichever one the code happened to read.

    What the connector does instead is FEED that record: a provider customer
    event fills the client's empty fields through
    ``taxonomy.fill_client_gaps``, which can never overwrite a curated value.
    Measured on a real account before choosing this: of 649 provider customers,
    148 carried fiscal data and only 5 of those were never invoiced -- so
    registering a client as soon as its fiscal identity is known costs 5 extra
    rows, not a polluted directory, which is what made the cache unnecessary.

    This table remains because an EXTERNAL id has to map to an internal one
    somehow, and because that mapping is what makes concurrent redeliveries of
    one payment resolve to one client: ``resolve_or_create_client`` dedupes with
    a SELECT-then-INSERT and no unique constraint behind it, so without the
    UNIQUE here two workers would both miss and both insert.
    """

    __tablename__ = "payment_customer_links"
    __table_args__ = (
        UniqueConstraint(
            "connector_id", "provider_customer_id", name="uq_payment_customer_links_customer"
        ),
        Index("ix_payment_customer_links_client_tag", "client_tag_id"),
    )

    connector_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payment_connectors.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Mirrors ``Invoice.client_tag_id``: a bare UUID, no FK (a client is a Tag
    #: plus a ClientProfile satellite, and tags are purged through taxonomy).
    client_tag_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)


__all__ = [
    "AUTOMATION_MODES",
    "DELIVERY_OUTCOMES",
    "EMISSION_EVENTS",
    "EVENT_STATUSES",
    "OBJECT_KINDS",
    "PROVIDERS",
    "PaymentConnector",
    "PaymentConnectorEvent",
    "PaymentCustomerLink",
    "PaymentObjectLink",
    "PaymentWebhookDelivery",
]
