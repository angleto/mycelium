"""Stripe adapter for the inbound payment connector (ADR-0051).

Pure translation: Stripe's event vocabulary in, :mod:`payment_events` DTOs out.
No network, no session, no ``stripe`` SDK. The SDK is deliberately not a
dependency -- everything we need from it here is an HMAC over the raw body and
some dictionary reading, and taking the package would add an untyped import, a
release cadence and a second HTTP client to a path that already has one.

WHICH EVENT MINTS A DOCUMENT. Stripe fires several events for one payment: an
``invoice.paid`` is also a ``charge.succeeded`` and a
``payment_intent.succeeded``. Emitting on all of them would file three invoices
for one sale, so exactly ONE type is the trigger (``connector.emission_event``)
and the others are demoted to payment reconciliation. ``invoice.paid`` is the
default because a Stripe Invoice is the only one of the three that carries line
items, a per-line tax breakdown and the customer's tax ids -- everything a
FatturaPA needs -- while a PaymentIntent carries a single gross number.

REFUNDS. Stripe models a reversal twice: ``credit_note.created`` (the document)
and ``charge.refunded`` / ``refund.created`` (the money). Refunding from the
dashboard with a credit note fires both. Rather than pick one and lose the
other, both are mapped, and the CREDIT NOTE additionally claims the refund ids
it settles. Whichever event we process first claims the shared refund id in
``payment_object_links``, and the other one then resolves to the document that
already exists instead of filing a second TD04 -- so the outcome does not depend
on delivery order, which Stripe does not guarantee.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from mycelium_core.services.payment_events import (
    CreditNoteIntent,
    CustomerProfileIntent,
    EmissionIntent,
    EventIdentity,
    IgnoreIntent,
    Intent,
    LineIn,
    MapperConfig,
    ObjectKey,
    PartyDigest,
    PartyIn,
    PayloadError,
    PaymentSyncIntent,
    ProviderEvent,
    SubscriptionContext,
    VerificationSecrets,
    as_decimal,
    as_mapping,
    as_sequence,
    as_str,
    checked_identity,
    checked_line,
    clamp_field,
    country_code,
    first_present,
    minor_to_decimal,
    province_code,
    resolve_inclusive,
    signature_matches,
    within_tolerance,
)

SIGNATURE_HEADER = "stripe-signature"

#: Events that reconcile payment state without minting anything.
_PAYMENT_EVENTS = frozenset(
    {"charge.succeeded", "payment_intent.succeeded", "invoice.payment_succeeded", "invoice.paid"}
)
#: Events that reverse a document.
_REFUND_EVENTS = frozenset({"charge.refunded", "refund.created"})
_CREDIT_NOTE_EVENTS = frozenset({"credit_note.created"})
#: Events that DESCRIBE a customer. They mint nothing; they are the only place
#: the counterpart's fiscal identity actually travels, because a Stripe webhook
#: payload cannot be expanded and an invoice event names its customer by id.
_CUSTOMER_EVENTS = frozenset({"customer.created", "customer.updated"})


def _parse_signature_header(raw: str) -> tuple[str | None, list[str]]:
    """Split ``t=...,v1=...,v1=...`` into the timestamp and every v1 candidate.

    Stripe sends more than one ``v1`` while an endpoint has multiple signing
    secrets, and a ``v0`` scheme that is not ours to check. Unknown keys are
    skipped rather than rejected, so a future scheme addition cannot break
    verification of the one we do support.
    """
    timestamp: str | None = None
    signatures: list[str] = []
    for part in raw.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value.strip()
        elif key == "v1":
            signatures.append(value.strip())
    return timestamp, signatures


def _event_object(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return as_mapping(as_mapping(payload.get("data")).get("object"))


def _metadata(*sources: Mapping[str, Any]) -> dict[str, str]:
    """Merge the ``metadata`` bags of several objects, earlier wins.

    Integrators put the fiscal identity on whichever object their code already
    had a handle on, so the invoice's own bag is consulted before the customer's.
    """
    merged: dict[str, str] = {}
    for src in reversed(sources):
        for key, value in as_mapping(src.get("metadata")).items():
            text = as_str(value)
            if text is not None:
                merged[key] = text
    return merged


def _address(node: Mapping[str, Any]) -> Mapping[str, Any]:
    return as_mapping(node.get("address"))


def _vat_from_tax_ids(tax_ids: Sequence[Any]) -> str | None:
    """First usable VAT number from a Stripe tax-id list.

    Stripe's ``eu_vat`` values carry the country as a prefix (``IT0112...``);
    ``normalize_vat`` downstream splits it. Types we do not understand are
    skipped rather than guessed at.
    """
    for entry in tax_ids:
        node = as_mapping(entry)
        kind = as_str(node.get("type")) or ""
        value = as_str(node.get("value"))
        if value is None:
            continue
        if kind in {"eu_vat", "gb_vat", "ch_vat", "no_vat", "eu_oss_vat"} or kind.endswith("_vat"):
            return value
    return None


def _party(
    node: Mapping[str, Any],
    *,
    metadata: Mapping[str, str],
    config: MapperConfig,
    fallback_name: str,
) -> PartyIn:
    """Build the counterpart from whichever shape Stripe used.

    ``node`` is a Stripe object exposing ``name``/``email``/``address`` -- an
    Invoice (``customer_name``, normalised by the caller), a Checkout Session's
    ``customer_details``, or a Charge's ``billing_details``. Metadata always
    wins over the derived value: it is the only place an integrator can put a
    codice fiscale or a codice destinatario, so it is the more specific source.
    """
    addr = _address(node)
    tax_ids = as_sequence(node.get("tax_ids")) or as_sequence(node.get("customer_tax_ids"))
    vat_from_ids = _vat_from_tax_ids(tax_ids)

    name = as_str(node.get("name")) or fallback_name
    # A code or nothing: the column holds two characters and Stripe's country
    # is free text on a manually-created customer.
    country = country_code(as_str(addr.get("country"))) or config.default_country_code
    # Every field is clamped to what the counterpart columns hold. A provider
    # string that is too long does not truncate on the way in, it raises a
    # driver error that is not a DomainError -- so it escapes the runner, fails
    # the event, and retries a payload that can never succeed.
    return PartyIn(
        legal_name=clamp_field("legal_name", name) or fallback_name,
        # The VAT's country. A VIES-prefixed number (IT0112...) overrides this
        # downstream in ``normalize_vat``; this is the fallback for a bare one.
        country_code=country,
        vat_number=clamp_field(
            "vat_number", first_present(metadata, config.metadata_vat_keys) or vat_from_ids
        ),
        tax_code=clamp_field("tax_code", first_present(metadata, config.metadata_tax_code_keys)),
        address=clamp_field("address", as_str(addr.get("line1"))),
        postal_code=clamp_field("postal_code", as_str(addr.get("postal_code"))),
        city=clamp_field("city", as_str(addr.get("city"))),
        # NOT clamped: ``state`` is free text and Italian records carry the
        # region ("Lazio") as often as the sigla. Truncating would invent "LAZI".
        province=province_code(as_str(addr.get("state"))),
        country=country,
        sdi_code=clamp_field("sdi_code", first_present(metadata, config.metadata_sdi_keys)),
        pec=clamp_field("pec", first_present(metadata, config.metadata_pec_keys)),
        email=clamp_field("email", as_str(node.get("email"))),
    )


def _expanded_charge(obj: Mapping[str, Any]) -> Mapping[str, Any]:
    """The Charge inlined in a PaymentIntent, under either API-version spelling.

    Distinct from ``_first_charge``, which wants the charge's ID for an object
    key. Here only an EXPANDED charge is of any use: an id is a string and
    carries no billing block. Absent expansion this is empty, which is the
    honest answer.
    """
    latest = obj.get("latest_charge")
    if isinstance(latest, Mapping):
        return as_mapping(latest)
    rows = as_sequence(as_mapping(obj.get("charges")).get("data"))
    first = as_mapping(rows[0]) if rows else {}
    return first


def _counterpart_nodes(obj: Mapping[str, Any], *, is_customer: bool) -> tuple[Any, ...]:
    """Every place a Stripe object may spell the counterpart, best first.

    One list rather than a branch per event type, for the same reason ``_party``
    is one function: the shapes differ in WHERE the pair lives, not in what it
    means, and a per-event branch would have to be revisited for every event
    type a future connector subscribes to.

    ``is_customer`` gates reading the object's own ``name``: on a Customer that
    field IS the counterpart, and on a Product or a Plan it is the article's
    name. Guessing wrong here would print a subscription's name in the column
    that answers "whose payment is this?".
    """
    charge = _expanded_charge(obj)
    customer = obj.get("customer")
    return (
        # A Checkout Session states the identity it collected at the till.
        as_mapping(obj.get("customer_details")),
        # An Invoice spells the counterpart with a ``customer_`` prefix.
        {"name": obj.get("customer_name"), "email": obj.get("customer_email")},
        # A Customer object spells it plainly.
        obj if is_customer else {},
        # ... and an event whose sender expanded ``customer`` inlines the same.
        as_mapping(customer) if isinstance(customer, Mapping) else {},
        # A Charge carries what the payment form collected.
        as_mapping(obj.get("billing_details")),
        as_mapping(charge.get("billing_details")),
        # Last resort: the address Stripe would mail the receipt to. A name is
        # never derived from it -- the local part of an address is a guess, and
        # a wrong name reads as a fact.
        {"email": obj.get("receipt_email") or charge.get("receipt_email")},
    )


#: Rates that reproduce a reported tax EXACTLY are preferred over the raw
#: quotient. Stripe's newer payloads report a tax amount and its taxable base
#: but identify the rate only by id (``txr_...``), and 451 / 2049 is 22.0107 --
#: an aliquota that is arithmetically defensible and fiscally wrong. Italy's
#: statutory rates plus the zero case cover every document this connector can
#: legitimately produce; anything else falls back to the quotient.
_CANDIDATE_RATES = (Decimal(22), Decimal(10), Decimal(5), Decimal(4), Decimal(0))


def _rate_from_amounts(tax_minor: Decimal, taxable_minor: Decimal) -> Decimal | None:
    """Recover the aliquota from a tax and its base.

    Prefers the statutory rate that REPRODUCES the reported tax after rounding
    -- that is a verification, not a guess: 2049 x 22% = 450.78 -> 451, so 22 is
    the rate Stripe applied. Only when no statutory rate reproduces it (a
    foreign rate, a bespoke one) does it fall back to the quotient, rounded to
    the two decimals FatturaPA allows.
    """
    if taxable_minor <= 0:
        return None
    for candidate in _CANDIDATE_RATES:
        if (taxable_minor * candidate / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP) == (
            tax_minor
        ):
            return candidate
    return (tax_minor / taxable_minor * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _tax_entries(node: Mapping[str, Any]) -> Sequence[Any]:
    """The line's tax breakdown, under whichever name this API version uses.

    ``tax_amounts`` up to the 2026-07-29 API, ``taxes`` from it. Reading only
    the old name does not degrade gracefully: the line looks untaxed, its gross
    amount is taken for a net one, and the connector default is added on top --
    a 25.00 charge becomes a 30.50 invoice.
    """
    entries = as_sequence(node.get("tax_amounts"))
    return entries if entries else as_sequence(node.get("taxes"))


def _line_tax(
    tax_amounts: Sequence[Any], *, config: MapperConfig
) -> tuple[Decimal | None, Decimal, bool | None]:
    """Resolve (rate, tax_amount_minor, inclusive) for one line.

    Handles BOTH shapes Stripe uses. The older one carries an expanded
    ``tax_rate`` with a ``percentage`` and a boolean ``inclusive``; the one
    introduced with the 2026-07-29 API carries ``tax_behavior`` and identifies
    the rate only by id, giving ``amount`` against ``taxable_amount`` instead.

    Reading only the old shape is not a missing feature, it is a WRONG INVOICE:
    a 25.00 charge with 22% inclusive tax comes through as "25.00 net, no rate",
    the connector default is then added on top, and the document says 30.50 for
    money that was 25.00. So the newer shape is read for what it states, and the
    aliquota is recovered from the amounts it does state.

    Only the FIRST rate is used when a line carries several: FatturaPA has one
    aliquota per line, and a genuinely multi-rate line has to be split by the
    sender, not silently averaged here.
    """
    for entry in tax_amounts:
        node = as_mapping(entry)
        rate_node = as_mapping(node.get("tax_rate"))
        percentage = rate_node.get("percentage")
        rate = as_decimal(percentage)
        if rate is None and isinstance(percentage, float):
            # Stripe sends percentages as JSON numbers; ``as_decimal`` refuses
            # floats for money, but a RATE is a small exact decimal and going
            # through str() keeps it exact (22.0 -> "22.0").
            rate = Decimal(str(percentage))
        amount = node.get("amount")
        tax_minor = Decimal(amount) if isinstance(amount, int) else Decimal(0)
        # ``inclusive`` (old) or ``tax_behavior`` (2026-07-29+), as a TRI-STATE:
        # an entry stating neither has said nothing about the behaviour, which
        # is not the same as saying "exclusive". The caller resolves silence,
        # and under ``auto`` silence means the amount is the total collected.
        stated: bool | None = None
        if isinstance(node.get("inclusive"), bool):
            stated = bool(node.get("inclusive"))
        behaviour = as_str(node.get("tax_behavior"))
        if behaviour in {"inclusive", "exclusive"}:
            stated = behaviour == "inclusive"
        if rate is None:
            taxable = node.get("taxable_amount")
            if isinstance(taxable, int):
                rate = _rate_from_amounts(tax_minor, Decimal(taxable))
        if rate is not None:
            return rate, tax_minor, stated
    return config.default_vat_rate, Decimal(0), None


def _invoice_lines(invoice: Mapping[str, Any], *, config: MapperConfig) -> tuple[LineIn, ...]:
    currency = as_str(invoice.get("currency")) or "EUR"
    rows = as_sequence(as_mapping(invoice.get("lines")).get("data"))
    out: list[LineIn] = []
    for row in rows:
        node = as_mapping(row)
        amount = node.get("amount")
        if not isinstance(amount, int):
            continue
        quantity = node.get("quantity")
        qty = Decimal(quantity) if isinstance(quantity, int) and quantity > 0 else Decimal(1)
        rate, _tax_minor, stated = _line_tax(_tax_entries(node), config=config)
        # A Stripe line amount is net under exclusive tax and gross under
        # inclusive tax, and the payload says which whenever tax was computed.
        # When it says nothing, ``auto`` reads the figure as the total
        # collected -- adding VAT on top of money already taken would invoice
        # more than the customer paid.
        includes_vat = resolve_inclusive(stated, config.vat_pricing)
        gross_or_net = minor_to_decimal(amount, currency)
        description = (
            as_str(node.get("description"))
            or as_str(as_mapping(node.get("price")).get("nickname"))
            or config.default_line_description
            or "Servizio"
        )
        checked_line(
            description=description, quantity=qty, unit_price=gross_or_net / qty, vat_rate=rate
        )
        out.append(
            LineIn(
                description=description[:1000],
                quantity=qty,
                unit_price=gross_or_net / qty,
                vat_rate=rate,
                vat_nature=config.default_vat_nature if rate in (None, Decimal(0)) else None,
                price_includes_vat=includes_vat,
            )
        )
    return tuple(out)


def _single_line(
    *, amount_minor: int, currency: str, description: str, config: MapperConfig, gross: bool
) -> tuple[LineIn, ...]:
    return (
        LineIn(
            description=description[:1000],
            quantity=Decimal(1),
            unit_price=minor_to_decimal(amount_minor, currency),
            vat_rate=config.default_vat_rate,
            vat_nature=config.default_vat_nature,
            price_includes_vat=gross,
        ),
    )


def _keys(*pairs: tuple[str, Any]) -> tuple[ObjectKey, ...]:
    """Keep the (kind, id) pairs whose id is actually a non-empty string.

    Stripe expands references inconsistently: ``charge`` is a bare id on one
    object and a nested dict on another. A dict is skipped here rather than
    stringified into a bogus key.
    """
    out: list[ObjectKey] = []
    for kind, value in pairs:
        ident = as_str(value)
        if ident is None and isinstance(value, Mapping):
            ident = as_str(value.get("id"))
        if ident is not None:
            out.append((kind, ident))
    return tuple(out)


def _epoch(value: object) -> datetime.datetime | None:
    """A Stripe unix timestamp as an aware datetime, or nothing.

    Total by design: a payload that omits the field, or carries a null or a
    string where a number belongs, must not fail an event over a date that is
    decoration on the document.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return datetime.datetime.fromtimestamp(value, tz=datetime.UTC)


class StripeMapper:
    name = "stripe"

    def verify(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        secrets: VerificationSecrets,
        tolerance_seconds: int,
        now: datetime.datetime,
    ) -> bool:
        raw = headers.get(SIGNATURE_HEADER) or headers.get("Stripe-Signature")
        if not raw:
            return False
        timestamp, signatures = _parse_signature_header(raw)
        if timestamp is None or not signatures:
            return False
        if not within_tolerance(timestamp, now=now, tolerance_seconds=tolerance_seconds):
            return False
        return signature_matches(
            secrets=secrets, timestamp=timestamp, raw_body=raw_body, provided=signatures
        )

    def identify(self, payload: Mapping[str, Any]) -> EventIdentity:
        event_id = as_str(payload.get("id"))
        event_type = as_str(payload.get("type"))
        if event_id is None or event_type is None:
            raise PayloadError("a Stripe event needs both 'id' and 'type'")
        created = payload.get("created")
        occurred = (
            datetime.datetime.fromtimestamp(created, tz=datetime.UTC)
            if isinstance(created, int)
            else None
        )
        return checked_identity(event_id, event_type, occurred)

    def is_emission_trigger(self, event_type: str, config: MapperConfig) -> bool:
        return event_type == config.emission_event

    def subscription(self, ctx: SubscriptionContext) -> tuple[ProviderEvent, ...]:
        events: list[ProviderEvent] = []
        if ctx.emits:
            events.append(ProviderEvent(ctx.emission_event, "emission"))
        # Not optional, and the least obvious entry on the list. A Stripe webhook
        # payload cannot be expanded, so an invoice event names its customer by
        # bare id: the VAT number, codice fiscale, codice destinatario and PEC
        # sitting on the Stripe customer never travel with the money. These two
        # events are the ONLY channel through which a counterpart's fiscal
        # identity reaches us. Omit them and the connector still verifies, still
        # ingests, still queues -- and parks nearly everything as
        # ``no_billing_data`` for reasons no log will explain.
        events.append(ProviderEvent("customer.created", "customer"))
        events.append(ProviderEvent("customer.updated", "customer"))
        if ctx.credit_notes:
            events.append(ProviderEvent("credit_note.created", "credit_note"))
            events.append(ProviderEvent(ctx.refund_event, "credit_note"))
        if ctx.payment_sync:
            # Only worth delivering when it is not already the emission trigger:
            # that one arrives anyway, and the emission path marks the document
            # paid itself.
            for candidate in ("charge.succeeded", "invoice.payment_succeeded"):
                if candidate != ctx.emission_event:
                    events.append(ProviderEvent(candidate, "payment_sync", required=False))
                    break
        return tuple(events)

    def describe_counterpart(self, payload: Mapping[str, Any]) -> PartyDigest:
        """Who this event is about, for the triage list. Never raises.

        The name and the email are resolved INDEPENDENTLY, first node wins for
        each: a Stripe invoice routinely carries ``customer_email`` and a null
        ``customer_name`` (the address is collected at checkout, the name is
        typed later, if ever), and stopping at the first node that has anything
        would throw away a name the expanded customer does carry.
        """
        obj = _event_object(payload)
        if not obj:
            return PartyDigest()
        # A Customer event's object IS the counterpart. The ``object``
        # discriminator is the authority when Stripe sends one; the event type
        # is the fallback, because it is what routes every other decision here
        # and it is present on every event.
        event_type = as_str(payload.get("type")) or ""
        is_customer = as_str(obj.get("object")) == "customer" or event_type.startswith("customer.")
        name: str | None = None
        email: str | None = None
        for node in _counterpart_nodes(obj, is_customer=is_customer):
            mapping = as_mapping(node)
            name = name or as_str(mapping.get("name"))
            email = email or as_str(mapping.get("email"))
            if name and email:
                break
        # Clamped like everything the mapper emits: a provider string of
        # unbounded length must not travel into a response unchecked.
        return PartyDigest(name=clamp_field("legal_name", name), email=clamp_field("email", email))

    def to_intent(self, payload: Mapping[str, Any], *, config: MapperConfig) -> Intent:
        identity = self.identify(payload)
        event_type = identity.event_type
        obj = _event_object(payload)
        if not obj:
            raise PayloadError("event carries no data.object")

        if self.is_emission_trigger(event_type, config):
            return self._emission(event_type, obj, config=config)
        if event_type in _CUSTOMER_EVENTS:
            return self._customer_profile(obj, config=config)
        if event_type in _CREDIT_NOTE_EVENTS:
            return self._credit_note(obj, config=config)
        if event_type in _REFUND_EVENTS:
            # One refund, two announcements. Honour only the configured one:
            # they agree while the charge carries expanded ``refunds.data`` (both
            # key the claim on the refund id) and diverge when it does not, which
            # would file the same refund twice. Ignoring by NAME rather than
            # silently keeps the reason visible in the ingress ledger, so an
            # operator who subscribed the other event sees why nothing happened
            # instead of an empty credit-note list.
            if event_type != config.refund_event:
                return IgnoreIntent(reason="refund_event_not_selected")
            return self._refund(event_type, obj)
        if event_type in _PAYMENT_EVENTS:
            # A charge event IS the charge object, so the instrument is right
            # here. An invoice event carries the charge as a bare id and this
            # reads nothing, which is correct: it states no fact it does not
            # have rather than guessing one.
            details = as_mapping(obj.get("payment_method_details"))
            return PaymentSyncIntent(
                parent_keys=self._money_keys(event_type, obj),
                method_type=as_str(details.get("type")),
                customer_key=as_str(obj.get("customer")),
            )
        return IgnoreIntent(reason="event_type_not_mapped")

    # --- emission ---------------------------------------------------------

    def _emission(self, event_type: str, obj: Mapping[str, Any], *, config: MapperConfig) -> Intent:
        if event_type == "invoice.paid":
            return self._from_invoice(obj, config=config)
        if event_type == "payment_intent.succeeded":
            return self._from_payment_intent(obj, config=config)
        if event_type == "checkout.session.completed":
            return self._from_checkout_session(obj, config=config)
        return IgnoreIntent(reason="emission_event_not_mapped")

    def _from_invoice(self, inv: Mapping[str, Any], *, config: MapperConfig) -> Intent:
        currency = as_str(inv.get("currency")) or "EUR"
        customer = (
            as_mapping(inv.get("customer")) if isinstance(inv.get("customer"), Mapping) else {}
        )
        metadata = _metadata(inv, customer)
        # An Invoice spells the counterpart with a ``customer_`` prefix; adapt it
        # to the common shape so one _party() serves every event.
        party_node: dict[str, Any] = {
            "name": inv.get("customer_name") or customer.get("name"),
            "email": inv.get("customer_email") or customer.get("email"),
            "address": inv.get("customer_address") or customer.get("address"),
            "tax_ids": inv.get("customer_tax_ids") or customer.get("tax_ids"),
        }
        lines = _invoice_lines(inv, config=config)
        if not lines:
            total = inv.get("total")
            if not isinstance(total, int):
                raise PayloadError("invoice has neither lines nor a total")
            lines = _single_line(
                amount_minor=total,
                currency=currency,
                description=config.default_line_description or "Servizio",
                config=config,
                gross=True,
            )
        return EmissionIntent(
            object_keys=_keys(
                ("invoice", inv.get("id")),
                ("payment_intent", inv.get("payment_intent")),
                ("charge", inv.get("charge")),
            ),
            party=_party(party_node, metadata=metadata, config=config, fallback_name="Cliente"),
            lines=lines,
            currency=currency,
            customer_key=as_str(inv.get("customer")) or as_str(customer.get("id")),
            # NOT the Stripe ``description``. That field is the dashboard "Memo",
            # written by the merchant TO THE CUSTOMER (Stripe renders it on the
            # hosted invoice and on the PDF, twice, alongside the identical
            # ``footer``), and it becomes <Causale> on a document that is read
            # by SdI, by the customer's commercialista and by an auditor years
            # later. A live account had it holding onboarding copy -- "vada su
            # <site>, Login, Configura" -- which was then filed as the fiscal
            # causale. ADR-0051 already refused to write a shadow-run marker
            # into ``purpose`` for the same reason; this closes the other door.
            # The causale is what the operator configured knowing it would be
            # one. The provider's own free text still describes the supply,
            # where it belongs: the line <Descrizione>.
            purpose=config.default_purpose,
            provider_number=as_str(inv.get("number")),
            paid=True,
            settled_at=_epoch(as_mapping(inv.get("status_transitions")).get("paid_at")),
        )

    def _from_payment_intent(self, pi: Mapping[str, Any], *, config: MapperConfig) -> Intent:
        currency = as_str(pi.get("currency")) or "EUR"
        amount = pi.get("amount_received")
        if not isinstance(amount, int) or amount == 0:
            amount = pi.get("amount")
        if not isinstance(amount, int):
            raise PayloadError("payment_intent carries no amount")
        charge = pi.get("latest_charge") or _first_charge(pi)
        charge_node = as_mapping(charge) if isinstance(charge, Mapping) else {}
        billing = as_mapping(charge_node.get("billing_details"))
        metadata = _metadata(pi, charge_node)
        return EmissionIntent(
            object_keys=_keys(
                ("payment_intent", pi.get("id")),
                ("charge", charge),
            ),
            party=_party(
                billing or {"name": None}, metadata=metadata, config=config, fallback_name="Cliente"
            ),
            # A PaymentIntent amount is what the card was charged: gross, always.
            lines=_single_line(
                amount_minor=amount,
                currency=currency,
                description=as_str(pi.get("description"))
                or config.default_line_description
                or "Servizio",
                config=config,
                gross=True,
            ),
            currency=currency,
            customer_key=as_str(pi.get("customer")),
            # Same rule as ``_from_invoice``: a provider's free text is not a
            # fiscal causale. Here it still describes the supply, on the line.
            purpose=config.default_purpose,
            paid=True,
        )

    def _from_checkout_session(self, session: Mapping[str, Any], *, config: MapperConfig) -> Intent:
        currency = as_str(session.get("currency")) or "EUR"
        details = as_mapping(session.get("customer_details"))
        metadata = _metadata(session)
        subtotal = session.get("amount_subtotal")
        total = session.get("amount_total")
        tax = as_mapping(session.get("total_details")).get("amount_tax")
        lines: tuple[LineIn, ...]
        if isinstance(subtotal, int) and isinstance(tax, int) and subtotal > 0 and tax > 0:
            # Both halves are known, so the rate is a fact rather than a default.
            rate = (Decimal(tax) / Decimal(subtotal) * 100).quantize(Decimal("0.01"))
            lines = (
                LineIn(
                    description=(config.default_line_description or "Servizio")[:1000],
                    quantity=Decimal(1),
                    unit_price=minor_to_decimal(subtotal, currency),
                    vat_rate=rate,
                    vat_nature=None,
                    price_includes_vat=False,
                ),
            )
        else:
            amount = total if isinstance(total, int) else subtotal
            if not isinstance(amount, int):
                raise PayloadError("checkout session carries no amount")
            lines = _single_line(
                amount_minor=amount,
                currency=currency,
                description=config.default_line_description or "Servizio",
                config=config,
                gross=True,
            )
        return EmissionIntent(
            object_keys=_keys(
                ("checkout_session", session.get("id")),
                ("payment_intent", session.get("payment_intent")),
                ("invoice", session.get("invoice")),
            ),
            party=_party(details, metadata=metadata, config=config, fallback_name="Cliente"),
            lines=lines,
            currency=currency,
            customer_key=as_str(session.get("customer")),
            purpose=config.default_purpose,
            paid=True,
        )

    def _customer_profile(self, cust: Mapping[str, Any], *, config: MapperConfig) -> Intent:
        """A Stripe Customer object -> the cached counterpart.

        Unlike an invoice, this object carries its own metadata bag and its own
        address, which is exactly the data the invoice event lacks.
        """
        customer_key = as_str(cust.get("id"))
        if customer_key is None:
            raise PayloadError("a customer event carries no id")
        metadata = _metadata(cust)
        # Stripe's Customer spells the name plainly; reuse the shared projection
        # so the cached party and the invoice-derived one cannot drift.
        return CustomerProfileIntent(
            customer_key=customer_key,
            party=_party(cust, metadata=metadata, config=config, fallback_name="Cliente"),
        )

    # --- reversal ---------------------------------------------------------

    def _credit_note(self, note: Mapping[str, Any], *, config: MapperConfig) -> Intent:
        currency = as_str(note.get("currency")) or "EUR"
        rows = as_sequence(as_mapping(note.get("lines")).get("data"))
        lines: list[LineIn] = []
        for row in rows:
            node = as_mapping(row)
            amount = node.get("amount")
            if not isinstance(amount, int):
                continue
            quantity = node.get("quantity")
            qty = Decimal(quantity) if isinstance(quantity, int) and quantity > 0 else Decimal(1)
            rate, _tax_minor, stated = _line_tax(_tax_entries(node), config=config)
            includes_vat = resolve_inclusive(stated, config.vat_pricing)
            lines.append(
                LineIn(
                    description=(
                        as_str(node.get("description"))
                        or config.default_line_description
                        or "Storno"
                    )[:1000],
                    quantity=qty,
                    unit_price=minor_to_decimal(amount, currency) / qty,
                    vat_rate=rate,
                    vat_nature=config.default_vat_nature if rate in (None, Decimal(0)) else None,
                    price_includes_vat=includes_vat,
                )
            )
        total = note.get("total")
        amount = minor_to_decimal(total, currency) if isinstance(total, int) else None
        # Claim the refunds this note settles as well as the note itself: the
        # matching charge.refunded/refund.created then dedups against it.
        refund_keys = _keys(("refund", note.get("refund")))
        for entry in as_sequence(note.get("refunds")):
            refund_keys += _keys(("refund", as_mapping(entry).get("refund") or entry))
        return CreditNoteIntent(
            object_keys=_keys(("credit_note", note.get("id"))) + refund_keys,
            parent_keys=_keys(
                ("invoice", note.get("invoice")),
                ("charge", note.get("charge")),
            ),
            lines=tuple(lines) or None,
            amount=amount,
            currency=currency,
            # ``memo`` is free text of unbounded length and lands in a
            # varchar(200) as the credit note's purpose. Unclamped it raises a
            # driver error, which is not a DomainError, so it escapes the event
            # runner and retries a payload that can never succeed
            # (payment_events._FIELD_LIMITS documents the same trap).
            reason=clamp_field("purpose", as_str(note.get("reason")) or as_str(note.get("memo"))),
        )

    def _refund(self, event_type: str, obj: Mapping[str, Any]) -> Intent:
        currency = as_str(obj.get("currency")) or "EUR"
        if event_type == "refund.created":
            amount = obj.get("amount")
            return CreditNoteIntent(
                object_keys=_keys(("refund", obj.get("id"))),
                parent_keys=_keys(
                    ("charge", obj.get("charge")),
                    ("payment_intent", obj.get("payment_intent")),
                ),
                amount=minor_to_decimal(amount, currency) if isinstance(amount, int) else None,
                currency=currency,
                reason=as_str(obj.get("reason")),
            )
        # charge.refunded: the charge carries the whole refund history and fires
        # again on every partial. Take the NEWEST refund (Stripe lists them
        # reverse-chronologically) so each partial produces its own TD04, keyed
        # on its own refund id.
        refunds = as_sequence(as_mapping(obj.get("refunds")).get("data"))
        newest = as_mapping(refunds[0]) if refunds else {}
        refund_id = as_str(newest.get("id"))
        amount_minor = newest.get("amount")
        if refund_id is None:
            # No refund detail expanded: fall back to the cumulative figure and
            # key on the charge, which still dedups a redelivery of this event.
            refunded = obj.get("amount_refunded")
            return CreditNoteIntent(
                object_keys=_keys(("refund", f"{as_str(obj.get('id'))}:refunded")),
                parent_keys=self._money_keys(event_type, obj),
                amount=(
                    minor_to_decimal(refunded, currency) if isinstance(refunded, int) else None
                ),
                currency=currency,
                reason="refund",
            )
        return CreditNoteIntent(
            object_keys=_keys(("refund", refund_id)),
            parent_keys=self._money_keys(event_type, obj),
            amount=(
                minor_to_decimal(amount_minor, currency) if isinstance(amount_minor, int) else None
            ),
            currency=currency,
            reason=as_str(newest.get("reason")) or "refund",
        )

    # --- shared -----------------------------------------------------------

    def _money_keys(self, event_type: str, obj: Mapping[str, Any]) -> tuple[ObjectKey, ...]:
        """Every provider id on this object that may already be linked."""
        if event_type.startswith("invoice."):
            return _keys(
                ("invoice", obj.get("id")),
                ("payment_intent", obj.get("payment_intent")),
                ("charge", obj.get("charge")),
            )
        if event_type.startswith("payment_intent."):
            return _keys(
                ("payment_intent", obj.get("id")),
                ("charge", obj.get("latest_charge") or _first_charge(obj)),
                ("invoice", obj.get("invoice")),
            )
        return _keys(
            ("charge", obj.get("id")),
            ("payment_intent", obj.get("payment_intent")),
            ("invoice", obj.get("invoice")),
        )


def _first_charge(pi: Mapping[str, Any]) -> Any:
    """The charge of a PaymentIntent on pre-2022 API versions, where it lived
    under ``charges.data[0]`` instead of ``latest_charge``."""
    rows = as_sequence(as_mapping(pi.get("charges")).get("data"))
    return as_mapping(rows[0]).get("id") if rows else None


MAPPER = StripeMapper()

__all__ = ["MAPPER", "SIGNATURE_HEADER", "StripeMapper"]
