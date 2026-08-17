"""The native Mycelium payment-event contract (ADR-0051).

This is not an adapter over somebody else's API: it is OUR published format, so
a sender we ship no adapter for can still drive electronic invoicing by
implementing a documented contract instead of waiting for us to write their
integration. The wire format is a thin JSON projection of the neutral DTOs in
:mod:`payment_events`, which keeps this mapper close to an identity function and
means the contract cannot silently drift from what the engine actually consumes.

The full specification, with worked examples, is docs/payment-connector-contract.md.

Wire shape::

    POST /api/v1/connectors/mycelium/{connector_id}
    X-Mycelium-Timestamp: 1755043200
    X-Mycelium-Signature: v1=<hex>
    X-Connector-Api-Key: <optional, when the connector requires one>

    {"id": "<sender-unique event id>",
     "type": "invoice.issue" | "invoice.credit" | "invoice.payment",
     "created": 1755043200,
     "data": { ... }}

The signature is ``HMAC-SHA256(secret, "{timestamp}.{raw_body}")`` in hex --
deliberately the same construction as our OUTBOUND webhook signer
(``services.webhooks.sign``, ADR-0047) and as Stripe's, so an integrator who has
already implemented one of the three has implemented all three, and this repo
has exactly one MAC construction to audit.

MONEY IS A STRING. ``"12.34"``, never ``12.34``. A JSON number is a float in
almost every parser, and a float has already lost the exactness a fiscal
document needs by the time it reaches us. A float amount is refused rather than
rounded, because silently absorbing the sender's precision bug would put it in
our XML.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from mycelium_core.services.payment_events import (
    CreditNoteIntent,
    EmissionIntent,
    EventIdentity,
    IgnoreIntent,
    Intent,
    LineIn,
    MapperConfig,
    ObjectKey,
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
    signature_matches,
    within_tolerance,
)

SIGNATURE_HEADER = "x-mycelium-signature"
TIMESTAMP_HEADER = "x-mycelium-timestamp"

EVENT_ISSUE = "invoice.issue"
EVENT_CREDIT = "invoice.credit"
EVENT_PAYMENT = "invoice.payment"

#: The contract's event vocabulary. Closed on purpose: an unknown type is
#: reported as ignored rather than guessed at, so a sender's typo shows up in
#: the connector's event list instead of silently doing nothing.
EVENT_TYPES = (EVENT_ISSUE, EVENT_CREDIT, EVENT_PAYMENT)


def _party(node: Mapping[str, Any], *, config: MapperConfig) -> PartyIn:
    name = (
        as_str(node.get("legal_name"))
        or " ".join(p for p in (as_str(node.get("first_name")), as_str(node.get("last_name"))) if p)
        or "Cliente"
    )
    return PartyIn(
        legal_name=name,
        first_name=as_str(node.get("first_name")),
        last_name=as_str(node.get("last_name")),
        country_code=as_str(node.get("country_code")) or config.default_country_code,
        vat_number=as_str(node.get("vat_number")),
        tax_code=as_str(node.get("tax_code")),
        address=as_str(node.get("address")),
        civic_number=as_str(node.get("civic_number")),
        postal_code=as_str(node.get("postal_code")),
        city=as_str(node.get("city")),
        province=as_str(node.get("province")),
        country=as_str(node.get("country")) or as_str(node.get("country_code")),
        sdi_code=as_str(node.get("sdi_code")),
        pec=as_str(node.get("pec")),
        email=as_str(node.get("email")),
    )


def _lines(raw: Any, *, config: MapperConfig) -> tuple[LineIn, ...]:
    out: list[LineIn] = []
    for entry in as_sequence(raw):
        node = as_mapping(entry)
        unit_price = _decimal_field(node, "unit_price")
        if unit_price is None:
            raise PayloadError("every line needs a decimal-string 'unit_price'")
        quantity = _decimal_field(node, "quantity")
        description = (
            as_str(node.get("description")) or config.default_line_description or "Servizio"
        )
        vat_rate = _decimal_field(node, "vat_rate")
        out.append(
            LineIn(
                description=description[:1000],
                quantity=quantity if quantity is not None and quantity > 0 else Decimal(1),
                unit_price=unit_price,
                vat_rate=vat_rate if vat_rate is not None else config.default_vat_rate,
                vat_nature=as_str(node.get("vat_nature")) or config.default_vat_nature,
                price_includes_vat=bool(node.get("price_includes_vat", config.amounts_include_vat)),
            )
        )
    return tuple(out)


def _decimal_field(node: Mapping[str, Any], key: str) -> Decimal | None:
    """A decimal field: absent, or exact. Never silently something else.

    ``as_decimal`` refuses a JSON number (money that arrived as a float has
    already lost the exactness a fiscal document needs) and returns ``None`` --
    but ``None`` is also what an ABSENT field returns, and the two mean opposite
    things to every caller here. Collapsing them is how ``2.5`` became a
    quantity of 1, ``4.0`` became the connector's default rate, and a partial
    credit of ``61.00`` became a full reversal: a wrong fiscal document, filed,
    with nothing logged anywhere. So: present-but-unparseable is a hard refusal,
    absent stays absent.
    """
    if key not in node or node[key] is None:
        return None
    value = as_decimal(node[key])
    if value is None:
        raise PayloadError(f"'{key}' must be a decimal STRING (e.g. \"12.34\"), not a JSON number")
    return value


def _reference(data: Mapping[str, Any], field: str) -> str:
    ref = as_str(data.get(field))
    if ref is None:
        raise PayloadError(f"'{field}' is required and must be a non-empty string")
    return ref


class NativeMapper:
    name = "mycelium"

    def verify(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        secrets: VerificationSecrets,
        tolerance_seconds: int,
        now: datetime.datetime,
    ) -> bool:
        timestamp = headers.get(TIMESTAMP_HEADER)
        raw = headers.get(SIGNATURE_HEADER)
        if not timestamp or not raw:
            return False
        if not within_tolerance(timestamp, now=now, tolerance_seconds=tolerance_seconds):
            return False
        # Accept "v1=<hex>" and a bare hex digest, and tolerate a comma-separated
        # list so a sender rotating its own secret can present both.
        provided: list[str] = []
        for part in raw.split(","):
            candidate = part.strip()
            if candidate.startswith("v1="):
                provided.append(candidate[3:].strip())
            elif candidate:
                provided.append(candidate)
        if not provided:
            return False
        return signature_matches(
            secrets=secrets, timestamp=timestamp, raw_body=raw_body, provided=provided
        )

    def identify(self, payload: Mapping[str, Any]) -> EventIdentity:
        event_id = as_str(payload.get("id"))
        event_type = as_str(payload.get("type"))
        if event_id is None or event_type is None:
            raise PayloadError("an event needs both 'id' and 'type'")
        created = payload.get("created")
        occurred = (
            datetime.datetime.fromtimestamp(created, tz=datetime.UTC)
            if isinstance(created, int)
            else None
        )
        return checked_identity(event_id, event_type, occurred)

    def is_emission_trigger(self, event_type: str, config: MapperConfig) -> bool:
        # The contract defines exactly one emission type, so unlike Stripe there
        # is nothing ambiguous to configure and ``emission_event`` is ignored.
        return event_type == EVENT_ISSUE

    def subscription(self, ctx: SubscriptionContext) -> tuple[ProviderEvent, ...]:
        # This contract was designed rather than reverse-engineered, so there is
        # exactly one event per outcome and nothing to choose: ``ctx`` only
        # decides which of them the connector will act on. There is no customer
        # event because the counterpart's fiscal block travels INSIDE the issue
        # event -- the whole asymmetry with Stripe in one line.
        events: list[ProviderEvent] = []
        if ctx.emits:
            events.append(ProviderEvent(EVENT_ISSUE, "emission"))
        if ctx.credit_notes:
            events.append(ProviderEvent(EVENT_CREDIT, "credit_note"))
        if ctx.payment_sync:
            events.append(ProviderEvent(EVENT_PAYMENT, "payment_sync", required=False))
        return tuple(events)

    def to_intent(self, payload: Mapping[str, Any], *, config: MapperConfig) -> Intent:
        identity = self.identify(payload)
        data = as_mapping(payload.get("data"))
        if not data:
            raise PayloadError("event carries no 'data' object")

        if identity.event_type == EVENT_ISSUE:
            reference = _reference(data, "reference")
            lines = _lines(data.get("lines"), config=config)
            if not lines:
                raise PayloadError("'lines' must contain at least one line")
            customer = as_mapping(data.get("customer"))
            keys: tuple[ObjectKey, ...] = (("invoice", reference),)
            return EmissionIntent(
                object_keys=keys,
                party=_party(customer, config=config),
                lines=lines,
                currency=(as_str(data.get("currency")) or "EUR").upper(),
                customer_key=as_str(data.get("customer_reference")),
                purpose=as_str(data.get("purpose")) or config.default_purpose,
                paid=bool(data.get("paid", True)),
            )

        if identity.event_type == EVENT_CREDIT:
            reference = _reference(data, "reference")
            parent = _reference(data, "parent_reference")
            lines = _lines(data.get("lines"), config=config)
            return CreditNoteIntent(
                object_keys=(("credit_note", reference),),
                parent_keys=(("invoice", parent),),
                lines=lines or None,
                amount=_decimal_field(data, "amount"),
                currency=(as_str(data.get("currency")) or "EUR").upper(),
                reason=as_str(data.get("reason")),
            )

        if identity.event_type == EVENT_PAYMENT:
            reference = _reference(data, "reference")
            return PaymentSyncIntent(parent_keys=(("invoice", reference),))

        return IgnoreIntent(reason="event_type_not_mapped")


MAPPER = NativeMapper()

__all__ = [
    "EVENT_CREDIT",
    "EVENT_ISSUE",
    "EVENT_PAYMENT",
    "EVENT_TYPES",
    "MAPPER",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "NativeMapper",
]
