"""Neutral payment-event DTOs, the mapper Protocol, and the provider registry
(ADR-0051).

This is the pluggable boundary of the inbound connector, in the shape the repo
already uses for ``SdiChannel`` / ``EmailConnector`` / ``Embedder``: a Protocol,
a registry keyed on the ``provider`` column, and DTOs that carry no vendor
vocabulary. Everything downstream -- client resolution, draft composition,
credit notes, transmission -- speaks only these types, so adding a provider is
one module plus one CHECK widening and touches no fiscal code.

Two implementations ship: ``stripe`` (an adapter over someone else's event
shape) and ``mycelium`` (our OWN published contract, which any sender can
implement). The second exists so the subsystem is not a Stripe feature with a
vendor lock-in; the DTOs below ARE that contract, and the native mapper is
close to an identity function over them.

The mapper is a PURE boundary: it parses bytes and dicts and returns intents. It
performs no I/O, touches no session, and knows nothing about invoices. That is
what makes the whole event vocabulary unit-testable without a database, and it
keeps the fiscal decisions in one place (``payment_connectors``).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

# --- money -----------------------------------------------------------------

# Currencies whose minor unit is the major unit (Stripe sends them unscaled).
_ZERO_DECIMAL = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "JPY",
        "KMF",
        "KRW",
        "MGA",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)
# Currencies with three decimal places.
_THREE_DECIMAL = frozenset({"BHD", "JOD", "KWD", "OMR", "TND"})


def minor_to_decimal(amount: int, currency: str) -> Decimal:
    """Convert a provider's integer minor-unit amount to a Decimal major amount.

    Never goes through float: a cent is exact in Decimal and is not in binary
    floating point, and these numbers end up in a fiscal document.
    """
    code = (currency or "EUR").upper()
    if code in _ZERO_DECIMAL:
        return Decimal(amount)
    exponent = 3 if code in _THREE_DECIMAL else 2
    return Decimal(amount).scaleb(-exponent)


def resolve_inclusive(stated: bool | None, vat_pricing: str) -> bool:
    """Does this amount already contain VAT?

    ``stated`` is what the PAYLOAD said, or None when it said nothing. The
    distinction matters: a provider reporting a tax behaviour is describing
    money that actually moved, so obeying it is not a preference. Only silence
    is a judgement call, and under ``auto`` the judgement is "the amount is what
    was collected, therefore it is the total" -- assuming otherwise invoices
    more than the customer paid.
    """
    if vat_pricing == "gross":
        return True
    if vat_pricing == "net":
        return False
    return True if stated is None else stated


#: Widths the counterpart columns can actually hold (``client_profile``). A
#: provider string longer than these does not truncate on the way in: it raises
#: a driver-level error that is NOT a DomainError, so it escapes the event
#: runner, fails the event, and retries forever on a payload that can never
#: succeed. The mapper is the right place to make values fit -- the same reason
#: ``checked_identity`` bounds the event id.
_FIELD_LIMITS = {
    "legal_name": 200,
    "purpose": 200,
    "first_name": 60,
    "last_name": 60,
    "vat_number": 30,
    "tax_code": 30,
    "address": 200,
    "civic_number": 8,
    "postal_code": 10,
    "city": 120,
    "sdi_code": 7,
    "pec": 320,
    "email": 320,
}


def clamp_field(name: str, value: str | None) -> str | None:
    """Trim a counterpart field to what the schema can hold."""
    if value is None:
        return None
    limit = _FIELD_LIMITS.get(name)
    return value[:limit] if limit else value


#: Bounds the persisted document can hold: ``invoice_lines.quantity`` is
#: Numeric(12,4), ``unit_price`` Numeric(14,4), ``vat_rate`` Numeric(5,2), and
#: the line total lands in Numeric(14,2). Beyond them the INSERT raises a
#: numeric-overflow error, which is not a DomainError -- it escapes the runner
#: and the event retries a payload that can never succeed. Clamping money would
#: be worse than refusing it, so these are refusals.
_MAX_QUANTITY = Decimal(10) ** 8
_MAX_UNIT_PRICE = Decimal(10) ** 10
_MAX_VAT_RATE = Decimal(1000)
_MAX_LINE_TOTAL = Decimal(10) ** 12


def checked_line(
    *, description: str, quantity: Decimal, unit_price: Decimal, vat_rate: Decimal | None
) -> None:
    """Refuse line figures the document cannot hold.

    A PayloadError, so the runner parks the event as ``needs_attention`` with a
    reason an operator can read, instead of a driver error that fails the event
    with no classification and retries forever.
    """
    if quantity <= 0 or quantity >= _MAX_QUANTITY:
        raise PayloadError(f"quantity out of range for line {description!r}")
    if abs(unit_price) >= _MAX_UNIT_PRICE:
        raise PayloadError(f"unit_price out of range for line {description!r}")
    if vat_rate is not None and not (Decimal(0) <= vat_rate < _MAX_VAT_RATE):
        raise PayloadError(f"vat_rate out of range for line {description!r}")
    if abs(quantity * unit_price) >= _MAX_LINE_TOTAL:
        raise PayloadError(f"line total out of range for line {description!r}")


def country_code(value: str | None) -> str | None:
    """The two-letter ISO country, or nothing.

    Same rule and same reason as :func:`province_code`: the column holds two
    characters, and a provider (or a sender implementing our contract) that
    writes ``"ITALIA"`` must not fail the event with a truncation error. A
    country is either a code or it is absent; guessing one from a name would be
    inventing fiscal data.
    """
    if value is None:
        return None
    text = value.strip().upper()
    return text if len(text) == 2 and text.isalpha() else None


def province_code(value: str | None) -> str | None:
    """The two-letter sigla, or nothing.

    Stripe's ``address.state`` is free text and Italian records carry the region
    ("Lazio"), the province name ("Roma") or the sigla ("RM") interchangeably.
    The column holds four characters and FatturaPA wants the sigla, so anything
    that is not one is DROPPED rather than guessed at or truncated: "Lazio"
    would become "Lazi", and a wrong provincia on a fiscal document is worse
    than an absent one, which the standard permits.
    """
    if value is None:
        return None
    text = value.strip().upper()
    # "IT-RM" and "IT RM" are common ISO-3166-2 spellings.
    if len(text) == 5 and text[:2] == "IT" and text[2] in {"-", " "}:
        text = text[3:]
    return text if len(text) == 2 and text.isalpha() else None


# --- neutral DTOs ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartyIn:
    """The counterpart (cessionario/committente) as the provider describes it.

    Deliberately the same field vocabulary as ``taxonomy.ClientInput`` minus the
    billing-preferences tail, so the adapter never has to invent a translation.
    Everything is optional except the name, because providers routinely send a
    consumer with nothing but an email -- deciding whether that is enough is the
    service's job, not the mapper's.
    """

    legal_name: str
    first_name: str | None = None
    last_name: str | None = None
    country_code: str | None = None
    vat_number: str | None = None
    tax_code: str | None = None
    address: str | None = None
    civic_number: str | None = None
    postal_code: str | None = None
    city: str | None = None
    province: str | None = None
    country: str | None = None
    sdi_code: str | None = None
    pec: str | None = None
    email: str | None = None


@dataclass(frozen=True, slots=True)
class LineIn:
    """One invoice line, per unit, in major units.

    ``vat_rate`` is a percentage (22 means 22%). ``None`` means the provider
    reported no tax and the connector default (or the issuer regime) decides.

    ``price_includes_vat`` exists because providers are not consistent about
    it and guessing wrong is a systematically wrong invoice, not a rounding
    error: a Stripe *invoice line* under exclusive tax is net, a *payment
    intent* amount is always what the card was charged (gross). The mapper
    reports which one it saw; the service does the arithmetic once, after it
    has resolved the effective rate, because only then is the split defined.
    """

    description: str
    quantity: Decimal = Decimal(1)
    unit_price: Decimal = Decimal(0)
    vat_rate: Decimal | None = None
    vat_nature: str | None = None
    price_includes_vat: bool = False


#: ``(object_kind, object_id)`` -- the provider objects that name this money.
#: Every one of them is claimed in ``payment_object_links`` against the emitted
#: document, so ANY later event mentioning ANY of them resolves instead of
#: emitting a second time.
ObjectKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class EmissionIntent:
    """Emit an invoice for money that has been collected."""

    object_keys: tuple[ObjectKey, ...]
    party: PartyIn
    lines: tuple[LineIn, ...]
    currency: str = "EUR"
    customer_key: str | None = None
    purpose: str | None = None
    #: The provider says the money is in hand, so the document is born paid.
    paid: bool = True


@dataclass(frozen=True, slots=True)
class CreditNoteIntent:
    """Reverse, in whole or in part, a document we previously emitted."""

    object_keys: tuple[ObjectKey, ...]
    #: How to find the parent: any object key we may have claimed for it.
    parent_keys: tuple[ObjectKey, ...]
    #: Explicit lines win when the provider gives them (they carry the exact
    #: per-rate split). Otherwise ``amount`` drives a pro-rata reduction.
    lines: tuple[LineIn, ...] | None = None
    #: Gross amount refunded, major units. ``None`` = reverse the whole parent.
    amount: Decimal | None = None
    currency: str = "EUR"
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PaymentSyncIntent:
    """Mark an already-emitted document as paid. Carries no fiscal content."""

    parent_keys: tuple[ObjectKey, ...]


@dataclass(frozen=True, slots=True)
class CustomerProfileIntent:
    """The provider described a customer. Cache it; emit nothing.

    Exists because a Stripe webhook payload cannot be expanded: an invoice event
    names its customer by id, so the fiscal identity stored ON that customer
    never travels with the invoice. The customer events DO carry it, just at a
    different time, so the connector keeps what it learns and uses it when a
    payment for that customer finally arrives.
    """

    customer_key: str
    party: PartyIn


@dataclass(frozen=True, slots=True)
class IgnoreIntent:
    """Recognised, deliberately not actionable under this configuration."""

    reason: str


Intent = (
    EmissionIntent | CreditNoteIntent | PaymentSyncIntent | CustomerProfileIntent | IgnoreIntent
)


@dataclass(frozen=True, slots=True)
class EventIdentity:
    """What the ingress needs before it knows anything about the payload."""

    event_id: str
    event_type: str
    occurred_at: datetime.datetime | None = None


@dataclass(frozen=True, slots=True)
class MapperConfig:
    """The connector settings a mapper is allowed to see.

    A narrow projection of ``PaymentConnector`` on purpose: a mapper that could
    read the whole row would grow fiscal opinions, and mapper tests would need a
    database row to construct.
    """

    emission_event: str = "invoice.paid"
    #: Which of the provider's refund announcements to honour. One, not a set,
    #: for the same reason ``emission_event`` is one: see ``REFUND_EVENTS``.
    refund_event: str = "refund.created"
    #: Ordered candidate key names per fiscal field: first present wins. A list
    #: rather than one name because a real provider account accumulates
    #: spellings -- a migration away from another e-invoicing vendor leaves its
    #: keys on records nobody has re-saved, and manual entry paths add
    #: capitalised variants. Precedence is the list order.
    metadata_vat_keys: tuple[str, ...] = ("vatId", "vat_number", "partita_iva")
    metadata_tax_code_keys: tuple[str, ...] = ("fiscal_code", "tax_code", "codice_fiscale")
    metadata_sdi_keys: tuple[str, ...] = ("codice_destinatario", "sdi_code", "sdi")
    metadata_pec_keys: tuple[str, ...] = ("pec",)
    default_country_code: str | None = None
    default_line_description: str | None = None
    default_vat_rate: Decimal | None = None
    default_vat_nature: str | None = None
    default_purpose: str | None = None
    #: ``auto`` | ``gross`` | ``net`` -- see ``VAT_PRICING``. Under ``auto`` the
    #: payload decides and silence means VAT-inclusive; the other two force.
    #: Amounts that are unambiguous by arithmetic (a charged card total) are
    #: gross whatever this says.
    vat_pricing: str = "auto"


@dataclass(frozen=True, slots=True)
class VerificationSecrets:
    """Both live signing secrets: the current one and, during a rotation, the
    grace copy. The resolver already expired the grace one in SQL, so a mapper
    that sees it may trust it."""

    current: str
    previous: str | None = None

    def candidates(self) -> tuple[str, ...]:
        return (self.current, self.previous) if self.previous else (self.current,)


#: Hard bounds on the two identifiers the ingress persists verbatim. They match
#: ``payment_connector_events.provider_event_id`` (255) and ``event_type`` (80).
#: Enforced in the MAPPER, not at the database: an over-long id has to become a
#: 4xx the sender can act on, because a driver-level truncation error is not a
#: DomainError, so it escapes as a 500 -- which is precisely the status that
#: makes a payment provider retry a body that can never be accepted, and it
#: rolls back the delivery-ledger row that was supposed to record the refusal.
MAX_EVENT_ID_LEN = 255
MAX_EVENT_TYPE_LEN = 80


class PayloadError(ValueError):
    """The body is not a well-formed event for this provider.

    Raised by ``identify``/``to_intent``; the ingress turns it into a 400 and the
    worker into a terminal ``needs_attention``, because re-running a malformed
    payload will never produce a different answer.
    """


def checked_identity(
    event_id: str, event_type: str, occurred_at: datetime.datetime | None
) -> EventIdentity:
    """Build an :class:`EventIdentity`, refusing what the schema cannot hold."""
    if len(event_id) > MAX_EVENT_ID_LEN:
        raise PayloadError(f"event id exceeds {MAX_EVENT_ID_LEN} characters")
    if len(event_type) > MAX_EVENT_TYPE_LEN:
        raise PayloadError(f"event type exceeds {MAX_EVENT_TYPE_LEN} characters")
    return EventIdentity(event_id=event_id, event_type=event_type, occurred_at=occurred_at)


#: What a subscribed event is FOR. The SPA renders one explanation per purpose,
#: so the vocabulary lives here (a backend that shipped English prose would have
#: to be translated in two places).
EVENT_PURPOSES = ("emission", "customer", "credit_note", "payment_sync")


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    """One event the provider must be told to deliver, and why.

    Derived from a connector's own settings rather than transcribed into a
    document, because the two drift: an operator who changes ``emission_event``
    or switches credit notes to manual would otherwise keep reading yesterday's
    checklist. ``required`` marks the events without which the connector cannot
    do the job it is configured for; the rest are useful but survivable.
    """

    event_type: str
    purpose: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class SubscriptionContext:
    """The connector switches that decide which events are worth delivering.

    Separate from :class:`MapperConfig` on purpose: that one is the projection a
    mapper may consult while MAPPING a payload, and these switches are enforced
    by the service, above the mapper. Passing them into ``to_intent`` would
    invite a mapper to start deciding policy.
    """

    emission_event: str
    refund_event: str
    #: ``invoice_mode``/``credit_note_mode`` are not ``off``.
    emits: bool = True
    credit_notes: bool = True
    payment_sync: bool = True


@runtime_checkable
class PaymentEventMapper(Protocol):
    """One provider's event dialect."""

    name: str

    def verify(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        secrets: VerificationSecrets,
        tolerance_seconds: int,
        now: datetime.datetime,
    ) -> bool:
        """Constant-time signature check plus replay window. No I/O."""
        ...

    def identify(self, payload: Mapping[str, Any]) -> EventIdentity:
        """Pull the dedup key and the event type out of a parsed body."""
        ...

    def is_emission_trigger(self, event_type: str, config: MapperConfig) -> bool:
        """Whether this event type is THE one that mints a document."""
        ...

    def to_intent(self, payload: Mapping[str, Any], *, config: MapperConfig) -> Intent:
        """Translate a recognised event into a neutral intent."""
        ...

    def subscription(self, ctx: SubscriptionContext) -> tuple[ProviderEvent, ...]:
        """Exactly the events this configuration needs the provider to deliver.

        The setup instructions an operator follows, generated from the mapper
        that will actually receive the traffic. A test asserts the two agree in
        both directions: nothing advertised here may be dropped as unmapped, and
        nothing ``to_intent`` acts on may be missing from every subscription.
        """
        ...


# --- shared signature primitives -------------------------------------------


def timestamped_mac(secret: str, timestamp: str, raw_body: bytes) -> str:
    """HMAC-SHA256 over ``{timestamp}.{raw_body}``, hex.

    Deliberately byte-identical in construction to ``services.webhooks.sign``
    (our OUTBOUND scheme) and to Stripe's, so the repo has exactly one webhook
    MAC construction to reason about in both directions.
    """
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256)
    return mac.hexdigest()


def signature_matches(
    *,
    secrets: VerificationSecrets,
    timestamp: str,
    raw_body: bytes,
    provided: Sequence[str],
) -> bool:
    """True when any presented signature matches under any live secret.

    Always compares with ``hmac.compare_digest`` and never short-circuits on the
    first secret, so the answer leaks no timing information about WHICH secret
    (or how many) is live.
    """
    ok = False
    for secret in secrets.candidates():
        # Compare BYTES, never str. ``hmac.compare_digest`` raises TypeError on a
        # str containing a codepoint above U+007F, and a header value reaches us
        # as latin-1-decoded text, so any byte >= 0x80 in a forged signature
        # would escape verification as an exception instead of a refusal -- a 500
        # on the public unauthenticated ingress, with the delivery-ledger row
        # rolled back along with it. Encoding first makes the comparison total.
        expected = timestamped_mac(secret, timestamp, raw_body).encode("ascii")
        for candidate in provided:
            if hmac.compare_digest(expected, candidate.encode("utf-8", errors="replace")):
                ok = True
    return ok


def within_tolerance(timestamp: str, *, now: datetime.datetime, tolerance_seconds: int) -> bool:
    """Replay window around a unix-seconds timestamp bound into the MAC.

    Rejects the future by the same margin as the past: a forged far-future
    timestamp would otherwise make a captured request replayable forever.
    """
    try:
        sent = int(timestamp)
    except (TypeError, ValueError):
        return False
    delta = abs(int(now.timestamp()) - sent)
    return delta <= tolerance_seconds


# --- registry --------------------------------------------------------------

_REGISTRY: dict[str, PaymentEventMapper] = {}


def register_mapper(mapper: PaymentEventMapper) -> None:
    _REGISTRY[mapper.name] = mapper


def get_mapper(provider: str) -> PaymentEventMapper:
    """Resolve the adapter for a ``payment_connectors.provider`` value.

    Imports the shipped mappers lazily on first use: they import this module for
    the DTOs, so a top-level import here would be circular.
    """
    if not _REGISTRY:
        from mycelium_core.services import payment_native, payment_stripe

        register_mapper(payment_stripe.MAPPER)
        register_mapper(payment_native.MAPPER)
    try:
        return _REGISTRY[provider]
    except KeyError as exc:  # pragma: no cover - guarded by the CHECK constraint
        raise PayloadError(f"unknown provider {provider!r}") from exc


# --- small parsing helpers shared by the mappers ---------------------------


def _normalise_key(key: str) -> str:
    """Fold a metadata key to what it MEANS, not how it was typed.

    Provider metadata is written by humans and by half a dozen integrations, so
    one field arrives as ``codice_destinatario``, ``Codice Destinatario``,
    ``codiceDestinatario`` and ``CODICE-DESTINATARIO`` in the same account. The
    candidate list already handles genuinely different NAMES (``sdi_code`` vs
    ``codice_destinatario``); this handles the same name typed differently,
    which is not a naming decision anybody made and should not cost an invoice.
    """
    return "".join(ch for ch in key.lower() if ch.isalnum())


def first_present(bag: Mapping[str, str], keys: Sequence[str]) -> str | None:
    """First non-empty value among ``keys``, in order, matched insensitively.

    The order IS the precedence: put the spelling currently written first and
    keep legacy ones as a tail, so a record nobody has re-saved still resolves
    while a freshly written one wins.

    Matching ignores case and separators (see ``_normalise_key``). An exact hit
    is still preferred, so a bag holding both ``sdi_code`` and ``sdiCode``
    resolves to the one the configuration actually names rather than to
    whichever the dict happens to yield first.
    """
    folded: dict[str, str] | None = None
    for key in keys:
        value = as_str(bag.get(key))
        if value is not None:
            return value
        if folded is None:
            # Built once, and only when an exact match has already failed: the
            # common case pays nothing. First occurrence wins, so a bag with two
            # spellings of one key is resolved deterministically.
            folded = {}
            for raw, raw_value in bag.items():
                folded.setdefault(_normalise_key(raw), raw_value)
        value = as_str(folded.get(_normalise_key(key)))
        if value is not None:
            return value
    return None


def as_str(value: Any) -> str | None:
    """A trimmed string, or None. Providers send ``null``, ``""`` and absent
    interchangeably and none of them is a value."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return value.strip() or None


def as_decimal(value: Any) -> Decimal | None:
    """Decimal from a string or an int. A float is REFUSED: money that arrived
    as a float has already lost the exactness a fiscal document needs, and
    silently accepting it would hide the sender's bug in our XML."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except ArithmeticError:
            return None
    return None


def as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def as_sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


__all__ = [
    "EVENT_PURPOSES",
    "CreditNoteIntent",
    "CustomerProfileIntent",
    "EmissionIntent",
    "EventIdentity",
    "IgnoreIntent",
    "Intent",
    "LineIn",
    "MapperConfig",
    "ObjectKey",
    "PartyIn",
    "PayloadError",
    "PaymentEventMapper",
    "PaymentSyncIntent",
    "ProviderEvent",
    "SubscriptionContext",
    "VerificationSecrets",
    "as_decimal",
    "as_mapping",
    "as_sequence",
    "as_str",
    "checked_identity",
    "checked_line",
    "clamp_field",
    "country_code",
    "first_present",
    "get_mapper",
    "minor_to_decimal",
    "province_code",
    "register_mapper",
    "resolve_inclusive",
    "signature_matches",
    "timestamped_mac",
    "within_tolerance",
]
