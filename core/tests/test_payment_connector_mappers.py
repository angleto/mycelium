"""Payment-connector mapper unit tests (ADR-0051).

Pure unit tests over the one part of the subsystem that is a PURE boundary: a
mapper parses bytes and dicts and returns intents, performs no I/O, touches no
session and knows nothing about invoices. That is exactly what makes the whole
event vocabulary testable without a database, and this module spends that
property: nothing here opens a session.
``core/tests/test_payment_connectors_service.py`` covers what the runner DOES
with an intent; this module covers whether the intent says the right thing in
the first place, which is where a wrong invoice is born.

Covered:

- signature verification for BOTH mappers: a correct signature, a wrong secret,
  a body tampered with after signing, a stale timestamp, a FUTURE timestamp
  (the replay guard, without which a forged far-future timestamp would make a
  captured request replayable forever), a rotation window where the previous
  secret is still live, and every malformed header shape -- each of which must
  be a refusal and never an exception, because this code runs before
  authentication on a public unauthenticated endpoint;
- the Stripe dialect: the inclusive/exclusive/absent tax split on an invoice
  line (getting it wrong is a systematically wrong invoice, not a rounding
  error), which provider ids are claimed for one payment (the ledger that stops
  a second document for the same money), where the counterpart's VAT number is
  read from, the payment-intent and checkout-session emission shapes, and both
  ways Stripe reports a reversal;
- the native published contract: the three event types, the decimal-string
  money rule, the required references, and the connector defaults;
- the money primitives: minor-unit conversion for 2-, 0- and 3-decimal
  currencies, the largest-remainder allocator, and the gross -> net division.

THREE tests are marked KNOWN BUG. They assert a guarantee the modules document
but do not keep, and are deliberately left failing rather than weakened; see the
comment on each for the failure mode and the report that accompanies this file.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from mycelium_core.services import payment_connectors as svc
from mycelium_core.services import payment_native, payment_stripe
from mycelium_core.services.payment_events import (
    CreditNoteIntent,
    EmissionIntent,
    IgnoreIntent,
    Intent,
    LineIn,
    MapperConfig,
    PayloadError,
    PaymentSyncIntent,
    VerificationSecrets,
    get_mapper,
    minor_to_decimal,
    timestamped_mac,
)

# --- signature fixtures ----------------------------------------------------

#: A frozen "now" so the replay window is a pure function of the payload. Real
#: clocks make a tolerance test flaky by construction.
_NOW = datetime.datetime(2026, 8, 13, 12, 0, 0, tzinfo=datetime.UTC)
_NOW_UNIX = int(_NOW.timestamp())
_TS = str(_NOW_UNIX)
_TOLERANCE = 300

_SECRET = "whsec_current_ce6f9d2b"
_PREVIOUS = "whsec_previous_1a7c40de"
_WRONG = "whsec_attacker_9f00baad"

#: The bytes are what is signed, so every test signs THESE and any test that
#: alters them must fail to verify.
_BODY = b'{"id":"evt_1","type":"invoice.paid","data":{"object":{"id":"in_1"}}}'

_PROVIDERS = ("stripe", "mycelium")


def _secrets(*, previous: str | None = None) -> VerificationSecrets:
    return VerificationSecrets(current=_SECRET, previous=previous)


def _signed(
    provider: str, *, secret: str = _SECRET, timestamp: str = _TS, body: bytes = _BODY
) -> dict[str, str]:
    """The headers a compliant sender would put on the wire for ``provider``.

    Built with ``timestamped_mac`` rather than a hand-rolled HMAC so the test
    fails loudly if the shared MAC construction ever changes shape, instead of
    quietly testing a signer that no longer matches the outbound one.
    """
    mac = timestamped_mac(secret, timestamp, body)
    if provider == "stripe":
        return {payment_stripe.SIGNATURE_HEADER: f"t={timestamp},v1={mac}"}
    return {
        payment_native.TIMESTAMP_HEADER: timestamp,
        payment_native.SIGNATURE_HEADER: f"v1={mac}",
    }


def _verify(
    provider: str,
    headers: Mapping[str, str],
    *,
    body: bytes = _BODY,
    secrets: VerificationSecrets | None = None,
    now: datetime.datetime = _NOW,
    tolerance: int = _TOLERANCE,
) -> bool:
    """Run one mapper's verifier the way the ingress does: lower-cased headers
    (Starlette hands them over that way) and an explicit ``now``."""
    return get_mapper(provider).verify(
        headers={k.lower(): v for k, v in headers.items()},
        raw_body=body,
        secrets=secrets if secrets is not None else _secrets(),
        tolerance_seconds=tolerance,
        now=now,
    )


# --- signature verification, both mappers ----------------------------------


def test_a_correctly_signed_request_verifies() -> None:
    for provider in _PROVIDERS:
        assert _verify(provider, _signed(provider)) is True, provider


def test_a_signature_made_with_another_secret_is_refused() -> None:
    """The whole authority of the endpoint: the connector id in the URL is a
    routing selector, so anyone who can guess it must still be stopped here."""
    for provider in _PROVIDERS:
        assert _verify(provider, _signed(provider, secret=_WRONG)) is False, provider


def test_a_body_tampered_with_after_signing_is_refused() -> None:
    """One byte flipped in the payload, the signature left untouched.

    This is the reason the ingress verifies the RAW bytes before parsing: a body
    normalised by ``json.loads`` no longer hashes to what the sender signed.
    """
    tampered = _BODY.replace(b'"in_1"', b'"in_2"')
    assert tampered != _BODY and len(tampered) == len(_BODY)
    for provider in _PROVIDERS:
        headers = _signed(provider)
        assert _verify(provider, headers, body=tampered) is False, provider
        # ... and the untampered body under the same header still verifies, so
        # the refusal above is about the payload and not about the fixture.
        assert _verify(provider, headers) is True, provider


def test_a_timestamp_older_than_the_tolerance_is_refused() -> None:
    """A captured request replayed later. The timestamp is bound INTO the MAC,
    so an attacker cannot move it without invalidating the signature."""
    for provider in _PROVIDERS:
        stale = str(_NOW_UNIX - _TOLERANCE - 1)
        assert _verify(provider, _signed(provider, timestamp=stale)) is False, provider
        # The boundary itself is still inside the window: "older than" is
        # strict, otherwise a sender exactly at the edge of a legitimate
        # network delay would be refused.
        edge = str(_NOW_UNIX - _TOLERANCE)
        assert _verify(provider, _signed(provider, timestamp=edge)) is True, provider


def test_a_timestamp_in_the_future_is_refused() -> None:
    """THE replay guard. Without a symmetric future bound, a sender (or anyone
    holding the secret for a moment) could mint a request stamped years ahead
    and replay it forever, because it would never fall out of the window."""
    for provider in _PROVIDERS:
        ahead = str(_NOW_UNIX + _TOLERANCE + 1)
        assert _verify(provider, _signed(provider, timestamp=ahead)) is False, provider
        assert _verify(provider, _signed(provider, timestamp=str(_NOW_UNIX + _TOLERANCE))) is True

    # A far-future stamp is refused by the same rule, which is the case that
    # actually matters: it is the one an attacker would choose.
    for provider in _PROVIDERS:
        far = str(_NOW_UNIX + 10 * 365 * 24 * 3600)
        assert _verify(provider, _signed(provider, timestamp=far)) is False, provider


def test_both_secrets_verify_during_a_rotation() -> None:
    """A rotation must not drop the events already queued in the provider's
    retry buffer, which are signed with the secret that is being replaced.
    The resolver has already expired the grace copy in SQL, so a mapper that
    is handed a ``previous`` may trust it."""
    rotating = _secrets(previous=_PREVIOUS)
    for provider in _PROVIDERS:
        assert _verify(provider, _signed(provider), secrets=rotating) is True, provider
        assert _verify(provider, _signed(provider, secret=_PREVIOUS), secrets=rotating) is True, (
            provider
        )
        # Once the grace window closes the resolver stops handing the old
        # secret over, and the same request stops verifying.
        assert _verify(provider, _signed(provider, secret=_PREVIOUS)) is False, provider


def test_a_missing_or_garbled_signature_header_is_refused_not_raised() -> None:
    """Every malformed shape a hostile or broken caller can send must come back
    as a plain False. An exception here would be a 500 on an unauthenticated
    endpoint (and, per the ingress, no delivery row recording the refusal)."""
    mac = timestamped_mac(_SECRET, _TS, _BODY)
    stripe_headers: list[dict[str, str]] = [
        {},  # no signature at all
        {payment_stripe.SIGNATURE_HEADER: ""},
        {payment_stripe.SIGNATURE_HEADER: "garbage"},
        {payment_stripe.SIGNATURE_HEADER: f"t={_TS}"},  # no v1 candidate
        {payment_stripe.SIGNATURE_HEADER: f"v1={mac}"},  # no timestamp
        {payment_stripe.SIGNATURE_HEADER: f"t=not-a-number,v1={mac}"},
        {payment_stripe.SIGNATURE_HEADER: f"t={_TS},v1="},
        {payment_stripe.SIGNATURE_HEADER: f"t={_TS},v1={mac[:-1]}"},  # truncated
        {payment_stripe.SIGNATURE_HEADER: f"t={_TS},,v1"},
    ]
    for headers in stripe_headers:
        assert _verify("stripe", headers) is False, headers

    native_headers: list[dict[str, str]] = [
        {},
        {payment_native.TIMESTAMP_HEADER: _TS},  # no signature
        {payment_native.SIGNATURE_HEADER: f"v1={mac}"},  # no timestamp
        {payment_native.TIMESTAMP_HEADER: _TS, payment_native.SIGNATURE_HEADER: ""},
        {payment_native.TIMESTAMP_HEADER: _TS, payment_native.SIGNATURE_HEADER: ",,,"},
        {payment_native.TIMESTAMP_HEADER: "", payment_native.SIGNATURE_HEADER: f"v1={mac}"},
        {
            payment_native.TIMESTAMP_HEADER: "yesterday",
            payment_native.SIGNATURE_HEADER: f"v1={mac}",
        },
        {payment_native.TIMESTAMP_HEADER: _TS, payment_native.SIGNATURE_HEADER: "v1="},
    ]
    for headers in native_headers:
        assert _verify("mycelium", headers) is False, headers


def test_a_non_ascii_signature_is_refused_not_raised() -> None:
    """KNOWN BUG (left failing on purpose).

    A header value reaches us latin-1 decoded (that is what the HTTP spec and
    Starlette do), so any byte >= 0x80 in the signature header arrives as a
    non-ASCII ``str``. ``hmac.compare_digest`` raises TypeError on non-ASCII
    strings, and ``payment_events.signature_matches`` compares without guarding
    for it, so the exception escapes ``verify`` -- on the PUBLIC unauthenticated
    ingress, where it becomes a 500 (and, because the handler only records a
    refusal on the paths it controls, no ``payment_webhook_deliveries`` row).

    A garbled signature is not a special case here: it is the single most likely
    thing an unauthenticated caller sends.
    """
    garbled = "é" * 64
    assert _verify("stripe", {payment_stripe.SIGNATURE_HEADER: f"t={_TS},v1={garbled}"}) is False
    assert (
        _verify(
            "mycelium",
            {
                payment_native.TIMESTAMP_HEADER: _TS,
                payment_native.SIGNATURE_HEADER: f"v1={garbled}",
            },
        )
        is False
    )


def test_stripe_accepts_any_matching_v1_candidate_and_ignores_v0() -> None:
    """Stripe sends several ``v1`` values while an endpoint has more than one
    signing secret, plus a ``v0`` scheme that is not ours to check."""
    good = timestamped_mac(_SECRET, _TS, _BODY)
    decoy = timestamped_mac(_WRONG, _TS, _BODY)

    header = f"t={_TS},v1={decoy},v1={good}"
    assert _verify("stripe", {payment_stripe.SIGNATURE_HEADER: header}) is True
    # Order must not matter: the match is searched, not positional.
    header = f"t={_TS},v1={good},v1={decoy}"
    assert _verify("stripe", {payment_stripe.SIGNATURE_HEADER: header}) is True

    # A v0 that happens to carry a valid v1 digest must NOT be accepted: we do
    # not implement that scheme, and honouring it would authenticate a request
    # under a construction we never verified.
    header = f"t={_TS},v0={good}"
    assert _verify("stripe", {payment_stripe.SIGNATURE_HEADER: header}) is False
    # An unknown future scheme alongside a good v1 is skipped, not rejected.
    header = f"t={_TS},v2=whatever,v1={good}"
    assert _verify("stripe", {payment_stripe.SIGNATURE_HEADER: header}) is True


def test_native_accepts_a_prefixed_or_bare_hex_signature() -> None:
    """The contract documents ``v1=<hex>``; a bare digest is accepted too so an
    integrator who signs with a one-liner is not tripped by a prefix."""
    mac = timestamped_mac(_SECRET, _TS, _BODY)
    prefixed = {
        payment_native.TIMESTAMP_HEADER: _TS,
        payment_native.SIGNATURE_HEADER: f"v1={mac}",
    }
    bare = {payment_native.TIMESTAMP_HEADER: _TS, payment_native.SIGNATURE_HEADER: mac}
    both = {
        payment_native.TIMESTAMP_HEADER: _TS,
        payment_native.SIGNATURE_HEADER: f"v1={timestamped_mac(_WRONG, _TS, _BODY)}, {mac}",
    }
    assert _verify("mycelium", prefixed) is True
    assert _verify("mycelium", bare) is True
    # A sender rotating its OWN secret may present both; one match is enough.
    assert _verify("mycelium", both) is True


def test_the_registry_resolves_the_two_shipped_mappers() -> None:
    assert get_mapper("stripe") is payment_stripe.MAPPER
    assert get_mapper("mycelium") is payment_native.MAPPER
    with pytest.raises(PayloadError):
        get_mapper("paypal")


# --- Stripe payload fixtures -----------------------------------------------


def _event(event_type: str, obj: dict[str, Any], *, event_id: str = "evt_1") -> dict[str, Any]:
    return {"id": event_id, "type": event_type, "created": 1_755_000_000, "data": {"object": obj}}


def _tax(*, amount: int, rate: float | str | int = 22.0, inclusive: bool = False) -> dict[str, Any]:
    return {"amount": amount, "inclusive": inclusive, "tax_rate": {"percentage": rate}}


def _invoice_obj(**overrides: Any) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": "in_1",
        "object": "invoice",
        "currency": "eur",
        "customer": "cus_1",
        "customer_name": "Acme SpA",
        "customer_email": "amministrazione@acme.test",
        "customer_address": {
            "line1": "Via Milano 9",
            "postal_code": "20100",
            "city": "Milano",
            "state": "MI",
            "country": "IT",
        },
        "charge": "ch_1",
        "payment_intent": "pi_1",
        "description": "Abbonamento marzo",
        "metadata": {"vat_number": "IT09876543210"},
        "total": 12200,
        "lines": {
            "data": [
                {
                    "description": "Piano Pro",
                    "quantity": 1,
                    "amount": 10000,
                    "tax_amounts": [_tax(amount=2200)],
                }
            ]
        },
    }
    obj.update(overrides)
    return obj


def _to_intent(payload: Mapping[str, Any], *, provider: str = "stripe", **cfg: Any) -> Intent:
    return get_mapper(provider).to_intent(payload, config=MapperConfig(**cfg))


def _as_emission(intent: Intent) -> EmissionIntent:
    assert isinstance(intent, EmissionIntent), f"expected an emission, got {intent!r}"
    return intent


def _as_credit_note(intent: Intent) -> CreditNoteIntent:
    assert isinstance(intent, CreditNoteIntent), f"expected a credit note, got {intent!r}"
    return intent


# --- Stripe: emission from an invoice --------------------------------------


def test_stripe_invoice_with_exclusive_tax_yields_a_net_line() -> None:
    """Under exclusive tax a Stripe line amount is NET, and the mapper must say
    so: the service divides gross figures by (1 + rate) and would strip a
    non-existent 22% off this one, invoicing 81.97 for a 100.00 sale."""
    intent = _as_emission(_to_intent(_event("invoice.paid", _invoice_obj())))

    assert len(intent.lines) == 1
    line = intent.lines[0]
    assert line.description == "Piano Pro"
    assert line.quantity == Decimal(1)
    assert line.unit_price == Decimal("100.00")
    assert line.vat_rate == Decimal(22)
    assert line.price_includes_vat is False
    assert line.vat_nature is None
    assert intent.currency == "eur"
    assert intent.paid is True
    assert intent.purpose == "Abbonamento marzo"
    # End to end with the service's own arithmetic: 100.00 net stays 100.00.
    assert svc.net_unit_price(line, fallback_rate=None) == Decimal("100.00")


def test_stripe_invoice_with_inclusive_tax_yields_a_gross_line() -> None:
    obj = _invoice_obj(
        lines={
            "data": [
                {
                    "description": "Piano Pro",
                    "quantity": 1,
                    "amount": 12200,
                    "tax_amounts": [_tax(amount=2200, inclusive=True)],
                }
            ]
        }
    )
    line = _as_emission(_to_intent(_event("invoice.paid", obj))).lines[0]

    assert line.unit_price == Decimal("122.00")
    assert line.vat_rate == Decimal(22)
    assert line.price_includes_vat is True, "an inclusive Stripe line is gross"
    # The same 100.00 of taxable base as the exclusive case, reached the other
    # way round. This is the assertion that makes the flag worth carrying.
    assert svc.net_unit_price(line, fallback_rate=None) == Decimal("100.00")


def test_stripe_invoice_without_tax_amounts_follows_the_connector_switch() -> None:
    """No tax breakdown at all: only the integrator knows how their catalogue
    is quoted, so the connector's explicit switch decides -- never a guess."""
    obj = _invoice_obj(
        lines={"data": [{"description": "Consulenza", "quantity": 1, "amount": 12200}]}
    )
    event = _event("invoice.paid", obj)

    inclusive = _as_emission(
        _to_intent(event, amounts_include_vat=True, default_vat_rate=Decimal(22))
    ).lines[0]
    assert inclusive.price_includes_vat is True
    assert inclusive.vat_rate == Decimal(22), "the connector default fills the missing rate"
    assert svc.net_unit_price(inclusive, fallback_rate=Decimal(22)) == Decimal("100.00")

    exclusive = _as_emission(
        _to_intent(event, amounts_include_vat=False, default_vat_rate=Decimal(22))
    ).lines[0]
    assert exclusive.price_includes_vat is False
    assert svc.net_unit_price(exclusive, fallback_rate=Decimal(22)) == Decimal("122.00")


def test_stripe_invoice_claims_every_object_that_names_the_money() -> None:
    """The claim set IS the no-double-filing guarantee: any later event
    mentioning any of these ids resolves to the document instead of minting a
    second one. A missing key here is a duplicate fiscal number."""
    intent = _as_emission(_to_intent(_event("invoice.paid", _invoice_obj())))

    assert set(intent.object_keys) == {
        ("invoice", "in_1"),
        ("payment_intent", "pi_1"),
        ("charge", "ch_1"),
    }
    assert intent.customer_key == "cus_1"

    # An unexpanded reference that Stripe sends as a nested object is still
    # claimed by its id, not stringified into a bogus key.
    expanded = _as_emission(
        _to_intent(_event("invoice.paid", _invoice_obj(charge={"id": "ch_9", "object": "charge"})))
    )
    assert ("charge", "ch_9") in expanded.object_keys
    # And a null reference contributes no key at all.
    partial = _as_emission(
        _to_intent(_event("invoice.paid", _invoice_obj(charge=None, payment_intent=None)))
    )
    assert set(partial.object_keys) == {("invoice", "in_1")}


def test_stripe_invoice_with_no_lines_falls_back_to_the_gross_total() -> None:
    obj = _invoice_obj(lines={"data": []}, total=12200)
    intent = _as_emission(_to_intent(_event("invoice.paid", obj), default_vat_rate=Decimal(22)))

    assert len(intent.lines) == 1
    assert intent.lines[0].unit_price == Decimal("122.00")
    assert intent.lines[0].price_includes_vat is True, "an invoice total is gross"
    assert svc.net_unit_price(intent.lines[0], fallback_rate=None) == Decimal("100.00")


# --- Stripe: the counterpart -----------------------------------------------


def test_stripe_reads_the_vat_number_from_the_configured_metadata_key() -> None:
    """Stripe carries no FatturaPA fields, so the fiscal identity travels in
    metadata under whatever key the integrator already picked.

    The setting is an ORDERED LIST, not one name: a real account accumulates
    spellings for the same field (a migration away from another e-invoicing
    vendor leaves its keys on records nobody has re-saved). Order IS precedence.
    """
    obj = _invoice_obj(metadata={"partita_iva": "IT09876543210", "vat_number": "IT00000000000"})
    intent = _as_emission(
        _to_intent(_event("invoice.paid", obj), metadata_vat_keys=("partita_iva", "vat_number"))
    )

    assert intent.party.vat_number == "IT09876543210", "the FIRST listed key must win"
    assert intent.party.legal_name == "Acme SpA"
    assert intent.party.address == "Via Milano 9"
    assert intent.party.postal_code == "20100"
    assert intent.party.city == "Milano"
    assert intent.party.province == "MI"
    assert intent.party.country == "IT"
    assert intent.party.email == "amministrazione@acme.test"

    # The tail of the list is what keeps a legacy record resolving.
    legacy = _invoice_obj(metadata={"vat_number": "IT00000000000"})
    fallback = _as_emission(
        _to_intent(_event("invoice.paid", legacy), metadata_vat_keys=("partita_iva", "vat_number"))
    )
    assert fallback.party.vat_number == "IT00000000000", "a later key still resolves"


def test_stripe_falls_back_to_the_customer_tax_id_when_metadata_has_none() -> None:
    obj = _invoice_obj(
        metadata={},
        customer_tax_ids=[
            {"type": "us_ein", "value": "12-3456789"},
            {"type": "eu_vat", "value": "IT09876543210"},
        ],
    )
    intent = _as_emission(_to_intent(_event("invoice.paid", obj)))

    assert intent.party.vat_number == "IT09876543210", "a tax-id type we know is used"


def test_stripe_metadata_wins_over_the_customer_tax_id() -> None:
    """Metadata is the more specific source: it is the only place an integrator
    can put a codice fiscale or a codice destinatario, so a connector that has
    been configured to carry the fiscal identity there must not be overridden by
    whatever Stripe happens to hold on the customer."""
    obj = _invoice_obj(
        metadata={"vat_number": "IT11111111111", "tax_code": "rssmra80a01h501u", "pec": "a@pec.it"},
        customer_tax_ids=[{"type": "eu_vat", "value": "IT99999999999"}],
    )
    intent = _as_emission(_to_intent(_event("invoice.paid", obj)))

    assert intent.party.vat_number == "IT11111111111"
    assert intent.party.tax_code == "rssmra80a01h501u"
    assert intent.party.pec == "a@pec.it"


def test_stripe_reads_metadata_from_an_expanded_customer_but_the_invoice_wins() -> None:
    obj = _invoice_obj(
        metadata={"vat_number": "IT11111111111"},
        customer={
            "id": "cus_9",
            "name": "Acme SpA",
            "metadata": {"vat_number": "IT99999999999", "sdi_code": "ABCDEFG"},
        },
    )
    intent = _as_emission(_to_intent(_event("invoice.paid", obj)))

    assert intent.party.vat_number == "IT11111111111", "the invoice's own bag is consulted first"
    assert intent.party.sdi_code == "ABCDEFG", "and the customer's fills what it does not carry"
    assert intent.customer_key == "cus_9"


def test_stripe_uses_the_connector_defaults_for_what_the_provider_cannot_send() -> None:
    obj = _invoice_obj(metadata={}, customer_address=None)
    intent = _as_emission(
        _to_intent(
            _event("invoice.paid", obj),
            default_country_code="IT",
            default_purpose="Vendita online",
        )
    )

    assert intent.party.country_code == "IT"
    # The mapper reports what the PROVIDER said and nothing else. There is no
    # connector-wide codice destinatario to fall back on: 0000000 cannot be used
    # to send, so a document is only addressable when the counterpart supplied a
    # real code or a PEC (or is foreign, which the service resolves by rule).
    assert intent.party.sdi_code is None
    # ``purpose`` prefers what the provider actually said.
    assert intent.purpose == "Abbonamento marzo"
    silent = _as_emission(
        _to_intent(
            _event("invoice.paid", _invoice_obj(description=None)), default_purpose="Vendita"
        )
    )
    assert silent.purpose == "Vendita"


# --- Stripe: emission from a payment intent / checkout session -------------


def test_stripe_payment_intent_yields_one_gross_line_and_both_keys() -> None:
    """A PaymentIntent amount is what the card was charged: gross, always, with
    no line detail to contradict it."""
    obj = {
        "id": "pi_9",
        "object": "payment_intent",
        "currency": "eur",
        "amount": 20000,
        "amount_received": 12200,
        "latest_charge": "ch_9",
        "customer": "cus_9",
        "description": "Ordine 42",
        "metadata": {"vat_number": "IT09876543210"},
    }
    intent = _as_emission(
        _to_intent(
            _event("payment_intent.succeeded", obj),
            emission_event="payment_intent.succeeded",
            default_vat_rate=Decimal(22),
        )
    )

    assert len(intent.lines) == 1
    line = intent.lines[0]
    assert line.unit_price == Decimal("122.00"), "amount_received is preferred over amount"
    assert line.price_includes_vat is True
    assert line.vat_rate == Decimal(22)
    assert svc.net_unit_price(line, fallback_rate=None) == Decimal("100.00")
    assert set(intent.object_keys) == {("payment_intent", "pi_9"), ("charge", "ch_9")}
    assert intent.customer_key == "cus_9"
    assert intent.party.vat_number == "IT09876543210"
    assert line.description == "Ordine 42"


def test_stripe_payment_intent_accepts_the_legacy_charges_shape() -> None:
    """Pre-2022 API versions put the charge under ``charges.data[0]`` instead of
    ``latest_charge``; an integrator pinned to an old version must still get the
    charge claimed, or a later refund cannot find its parent."""
    obj = {
        "id": "pi_legacy",
        "object": "payment_intent",
        "currency": "eur",
        "amount": 12200,
        "charges": {"data": [{"id": "ch_legacy", "object": "charge"}]},
    }
    intent = _as_emission(
        _to_intent(
            _event("payment_intent.succeeded", obj), emission_event="payment_intent.succeeded"
        )
    )

    assert set(intent.object_keys) == {
        ("payment_intent", "pi_legacy"),
        ("charge", "ch_legacy"),
    }


def test_stripe_payment_intent_takes_the_party_from_an_expanded_charge() -> None:
    obj = {
        "id": "pi_exp",
        "object": "payment_intent",
        "currency": "eur",
        "amount_received": 12200,
        "latest_charge": {
            "id": "ch_exp",
            "object": "charge",
            "billing_details": {
                "name": "Beta SRL",
                "email": "info@beta.test",
                "address": {
                    "line1": "Corso Italia 3",
                    "postal_code": "10100",
                    "city": "Torino",
                    "state": "TO",
                    "country": "IT",
                },
            },
            "metadata": {"vat_number": "IT12312312311"},
        },
    }
    intent = _as_emission(
        _to_intent(
            _event("payment_intent.succeeded", obj), emission_event="payment_intent.succeeded"
        )
    )

    assert intent.party.legal_name == "Beta SRL"
    assert intent.party.city == "Torino"
    assert intent.party.vat_number == "IT12312312311"
    assert ("charge", "ch_exp") in intent.object_keys


def test_stripe_checkout_session_derives_the_exact_rate() -> None:
    """With both halves known the rate is a fact, not a default, and the
    subtotal is the taxable base."""
    obj = {
        "id": "cs_1",
        "object": "checkout.session",
        "currency": "eur",
        "amount_subtotal": 10000,
        "amount_total": 12200,
        "total_details": {"amount_tax": 2200},
        "payment_intent": "pi_cs",
        "invoice": "in_cs",
        "customer": "cus_cs",
        "customer_details": {
            "name": "Gamma SNC",
            "email": "g@example.test",
            "address": {
                "line1": "Via Verdi 2",
                "postal_code": "50100",
                "city": "Firenze",
                "state": "FI",
                "country": "IT",
            },
        },
        "metadata": {"vat_number": "IT55555555555"},
    }
    intent = _as_emission(
        _to_intent(
            _event("checkout.session.completed", obj), emission_event="checkout.session.completed"
        )
    )

    assert len(intent.lines) == 1
    line = intent.lines[0]
    assert line.unit_price == Decimal("100.00")
    assert line.vat_rate == Decimal("22.00")
    assert line.price_includes_vat is False
    assert set(intent.object_keys) == {
        ("checkout_session", "cs_1"),
        ("payment_intent", "pi_cs"),
        ("invoice", "in_cs"),
    }
    assert intent.party.legal_name == "Gamma SNC"
    assert intent.party.vat_number == "IT55555555555"


def test_stripe_checkout_session_without_a_tax_split_falls_back_to_the_total() -> None:
    obj = {
        "id": "cs_2",
        "object": "checkout.session",
        "currency": "eur",
        "amount_subtotal": 12200,
        "amount_total": 12200,
        "total_details": {"amount_tax": 0},
        "customer_details": {"name": "Delta SRL"},
    }
    intent = _as_emission(
        _to_intent(
            _event("checkout.session.completed", obj),
            emission_event="checkout.session.completed",
            default_vat_rate=Decimal(22),
        )
    )

    line = intent.lines[0]
    assert line.unit_price == Decimal("122.00")
    assert line.price_includes_vat is True, "a charged total is gross"
    assert line.vat_rate == Decimal(22)


# --- Stripe: reversals -----------------------------------------------------


def test_stripe_credit_note_claims_the_note_and_every_refund_it_settles() -> None:
    """This is what stops a double TD04. A dashboard refund fires BOTH
    ``credit_note.created`` and ``charge.refunded``/``refund.created``, and
    Stripe guarantees no order between them; the note claiming the refund ids it
    settles is what makes whichever arrives second resolve instead of reversing
    the invoice a second time."""
    obj = {
        "id": "cn_1",
        "object": "credit_note",
        "currency": "eur",
        "invoice": "in_1",
        "charge": "ch_1",
        "total": 12200,
        "refund": "re_1",
        "refunds": [{"refund": "re_2", "amount_refunded": 1000}, "re_3"],
        "reason": "duplicate",
        "lines": {
            "data": [
                {
                    "description": "Storno Piano Pro",
                    "quantity": 1,
                    "amount": 10000,
                    "tax_amounts": [_tax(amount=2200)],
                }
            ]
        },
    }
    intent = _as_credit_note(_to_intent(_event("credit_note.created", obj)))

    assert set(intent.object_keys) == {
        ("credit_note", "cn_1"),
        ("refund", "re_1"),
        ("refund", "re_2"),
        ("refund", "re_3"),
    }
    assert intent.parent_keys == (("invoice", "in_1"), ("charge", "ch_1"))
    assert intent.amount == Decimal("122.00")
    assert intent.reason == "duplicate"
    assert intent.lines is not None
    assert intent.lines[0].unit_price == Decimal("100.00")
    assert intent.lines[0].vat_rate == Decimal(22)
    assert intent.lines[0].price_includes_vat is False


def test_stripe_charge_refunded_keys_on_the_newest_refund_only() -> None:
    """``charge.refunded`` fires again on every partial and the charge carries
    the WHOLE refund history, so keying on the cumulative figure would credit
    the first refund twice and the second one never."""
    obj = {
        "id": "ch_1",
        "object": "charge",
        "currency": "eur",
        "payment_intent": "pi_1",
        "invoice": "in_1",
        "amount_refunded": 5000,
        "refunds": {
            "data": [
                {"id": "re_2", "amount": 2000, "reason": "requested_by_customer"},
                {"id": "re_1", "amount": 3000, "reason": "duplicate"},
            ]
        },
    }
    intent = _as_credit_note(_to_intent(_event("charge.refunded", obj)))

    assert intent.object_keys == (("refund", "re_2"),), "Stripe lists refunds newest first"
    assert intent.amount == Decimal("20.00"), "this refund, not the cumulative 50.00"
    assert set(intent.parent_keys) == {
        ("charge", "ch_1"),
        ("payment_intent", "pi_1"),
        ("invoice", "in_1"),
    }
    assert intent.reason == "requested_by_customer"


def test_stripe_charge_refunded_without_refund_detail_keys_on_the_charge() -> None:
    """Documented fallback: with nothing expanded there is no refund id to key
    on, so the cumulative figure and a charge-derived key at least keep a
    redelivery of this event from reversing twice."""
    obj = {
        "id": "ch_7",
        "object": "charge",
        "currency": "eur",
        "payment_intent": "pi_7",
        "amount_refunded": 4500,
        "refunds": {"data": []},
    }
    intent = _as_credit_note(_to_intent(_event("charge.refunded", obj)))

    assert intent.object_keys == (("refund", "ch_7:refunded"),)
    assert intent.amount == Decimal("45.00")
    assert ("charge", "ch_7") in intent.parent_keys


def test_stripe_refund_created_keys_on_the_refund() -> None:
    obj = {
        "id": "re_9",
        "object": "refund",
        "currency": "eur",
        "amount": 6100,
        "charge": "ch_1",
        "payment_intent": "pi_1",
        "reason": "requested_by_customer",
    }
    intent = _as_credit_note(_to_intent(_event("refund.created", obj)))

    assert intent.object_keys == (("refund", "re_9"),)
    assert intent.parent_keys == (("charge", "ch_1"), ("payment_intent", "pi_1"))
    assert intent.amount == Decimal("61.00")
    assert intent.reason == "requested_by_customer"


# --- Stripe: reconciliation, ignoring, malformed ---------------------------


def test_stripe_payment_events_reconcile_without_minting() -> None:
    """With ``invoice.paid`` as the trigger, the sibling events Stripe fires for
    the same money must carry no fiscal content at all."""
    charge = _to_intent(
        _event(
            "charge.succeeded",
            {"id": "ch_1", "object": "charge", "payment_intent": "pi_1", "invoice": "in_1"},
        )
    )
    assert isinstance(charge, PaymentSyncIntent)
    assert set(charge.parent_keys) == {
        ("charge", "ch_1"),
        ("payment_intent", "pi_1"),
        ("invoice", "in_1"),
    }

    # An invoice.* payment event reads its keys from the invoice shape instead.
    inv = _to_intent(
        _event(
            "invoice.payment_succeeded",
            {"id": "in_1", "object": "invoice", "payment_intent": "pi_1", "charge": "ch_1"},
        )
    )
    assert isinstance(inv, PaymentSyncIntent)
    assert set(inv.parent_keys) == {
        ("invoice", "in_1"),
        ("payment_intent", "pi_1"),
        ("charge", "ch_1"),
    }

    # And a payment_intent.succeeded that is NOT the configured trigger is a
    # reconciliation too -- otherwise one sale would be invoiced twice.
    pi = _to_intent(
        _event(
            "payment_intent.succeeded",
            {"id": "pi_1", "object": "payment_intent", "latest_charge": "ch_1"},
        )
    )
    assert isinstance(pi, PaymentSyncIntent)
    assert ("payment_intent", "pi_1") in pi.parent_keys


def test_stripe_unmapped_event_type_is_ignored() -> None:
    intent = _to_intent(_event("customer.subscription.updated", {"id": "sub_1"}))
    assert isinstance(intent, IgnoreIntent)
    assert intent.reason == "event_type_not_mapped"


def test_stripe_malformed_payloads_raise_payload_error() -> None:
    """A malformed body is terminal, not retryable: the frozen payload will not
    parse differently on the tenth attempt. The ingress turns this into a 400
    and the worker into ``needs_attention``."""
    with pytest.raises(PayloadError):
        _to_intent({"type": "invoice.paid", "data": {"object": {"id": "in_1"}}})  # no id
    with pytest.raises(PayloadError):
        _to_intent({"id": "evt_1", "data": {"object": {"id": "in_1"}}})  # no type
    with pytest.raises(PayloadError):
        _to_intent({"id": "evt_1", "type": "invoice.paid"})  # no data
    with pytest.raises(PayloadError):
        _to_intent({"id": "evt_1", "type": "invoice.paid", "data": {}})  # no data.object
    with pytest.raises(PayloadError):
        _to_intent(_event("invoice.paid", {}))  # empty object
    with pytest.raises(PayloadError):
        # An invoice with neither lines nor a total is not an amount of money.
        _to_intent(_event("invoice.paid", _invoice_obj(lines={"data": []}, total=None)))
    with pytest.raises(PayloadError):
        _to_intent(
            _event("payment_intent.succeeded", {"id": "pi_1", "currency": "eur"}),
            emission_event="payment_intent.succeeded",
        )


def test_stripe_identify_extracts_the_dedup_key_and_the_timestamp() -> None:
    identity = payment_stripe.MAPPER.identify(_event("invoice.paid", _invoice_obj()))
    assert identity.event_id == "evt_1"
    assert identity.event_type == "invoice.paid"
    assert identity.occurred_at == datetime.datetime.fromtimestamp(1_755_000_000, tz=datetime.UTC)
    # A provider that sends no usable timestamp is fine; the row keeps NULL.
    assert (
        payment_stripe.MAPPER.identify({"id": "e", "type": "t", "created": "nope"}).occurred_at
        is None
    )


def test_stripe_is_emission_trigger_honours_the_connector_setting() -> None:
    """Exactly ONE event type mints a document. Stripe fires several for the
    same money, so a set here would double-invoice."""
    mapper = payment_stripe.MAPPER
    for configured in ("invoice.paid", "payment_intent.succeeded", "checkout.session.completed"):
        config = MapperConfig(emission_event=configured)
        assert mapper.is_emission_trigger(configured, config) is True
        others = {"invoice.paid", "payment_intent.succeeded", "checkout.session.completed"} - {
            configured
        }
        for other in others:
            assert mapper.is_emission_trigger(other, config) is False, (configured, other)


# --- the native contract ---------------------------------------------------


def _native(event_type: str, data: dict[str, Any], *, event_id: str = "ev-1") -> dict[str, Any]:
    return {"id": event_id, "type": event_type, "created": 1_755_000_000, "data": data}


def _native_customer() -> dict[str, Any]:
    return {
        "legal_name": "Acme SpA",
        "country_code": "IT",
        "vat_number": "IT09876543210",
        "address": "Via Milano",
        "civic_number": "9",
        "postal_code": "20100",
        "city": "Milano",
        "province": "MI",
        "sdi_code": "ABCDEFG",
        "email": "amministrazione@acme.test",
    }


def test_native_issue_maps_to_an_emission() -> None:
    data = {
        "reference": "ORD-2026-0001",
        "customer_reference": "cust-77",
        "currency": "eur",
        "purpose": "Consulenza marzo",
        "paid": True,
        "customer": _native_customer(),
        "lines": [
            {
                "description": "Consulenza",
                "quantity": "2",
                "unit_price": "100.00",
                "vat_rate": "22",
            }
        ],
    }
    intent = _as_emission(_to_intent(_native("invoice.issue", data), provider="mycelium"))

    assert intent.object_keys == (("invoice", "ORD-2026-0001"),)
    assert intent.customer_key == "cust-77"
    assert intent.currency == "EUR", "the contract normalises the currency code"
    assert intent.purpose == "Consulenza marzo"
    assert intent.paid is True
    assert intent.party.legal_name == "Acme SpA"
    assert intent.party.vat_number == "IT09876543210"
    assert intent.party.civic_number == "9"
    assert intent.party.sdi_code == "ABCDEFG"
    assert len(intent.lines) == 1
    assert intent.lines[0].quantity == Decimal(2)
    assert intent.lines[0].unit_price == Decimal("100.00")
    assert intent.lines[0].vat_rate == Decimal(22)
    assert intent.lines[0].price_includes_vat is False


def test_native_credit_maps_to_a_credit_note() -> None:
    data = {
        "reference": "NC-1",
        "parent_reference": "ORD-2026-0001",
        "amount": "61.00",
        "currency": "eur",
        "reason": "Reso parziale",
    }
    intent = _as_credit_note(_to_intent(_native("invoice.credit", data), provider="mycelium"))

    assert intent.object_keys == (("credit_note", "NC-1"),)
    assert intent.parent_keys == (("invoice", "ORD-2026-0001"),)
    assert intent.amount == Decimal("61.00")
    assert intent.currency == "EUR"
    assert intent.reason == "Reso parziale"
    assert intent.lines is None, "no explicit lines means a pro-rata reduction"


def test_native_payment_maps_to_a_payment_sync() -> None:
    intent = _to_intent(
        _native("invoice.payment", {"reference": "ORD-2026-0001"}), provider="mycelium"
    )
    assert isinstance(intent, PaymentSyncIntent)
    assert intent.parent_keys == (("invoice", "ORD-2026-0001"),)


def test_native_refuses_a_float_unit_price() -> None:
    """The contract's headline rule: money is a decimal STRING.

    A JSON number is a float in almost every parser and has already lost the
    exactness a fiscal document needs by the time it reaches us. Refusing is the
    documented behaviour, because silently absorbing the sender's precision bug
    would put it in our XML.
    """
    data = {
        "reference": "ORD-1",
        "customer": _native_customer(),
        "lines": [{"description": "Consulenza", "unit_price": 100.00}],
    }
    with pytest.raises(PayloadError):
        _to_intent(_native("invoice.issue", data), provider="mycelium")

    # A bare int is exact, so it is accepted (100 == Decimal(100)).
    data["lines"] = [{"description": "Consulenza", "unit_price": 100}]
    intent = _as_emission(_to_intent(_native("invoice.issue", data), provider="mycelium"))
    assert intent.lines[0].unit_price == Decimal(100)


def test_native_float_amount_on_a_credit_is_refused() -> None:
    """KNOWN BUG (left failing on purpose).

    ``payment_native`` documents "A float amount is refused rather than
    rounded". For a line's ``unit_price`` it is (test above). For the CREDIT
    NOTE's ``amount`` it is not: ``as_decimal`` returns None for a float and the
    None is then indistinguishable from an absent amount, which the runner reads
    as "reverse the whole parent" (``_apply_partial``: ``if intent.amount is
    None ... return  # full reversal``).

    So a sender asking to credit 61.00 of a 12200.00 invoice with a JSON number
    instead of a string gets a TD04 for the ENTIRE invoice. The sender's
    precision bug does not get absorbed, it gets amplified into a full reversal,
    which is the worst available outcome and the exact opposite of what the
    module promises.
    """
    data = {"reference": "NC-1", "parent_reference": "ORD-1", "amount": 61.00}
    with pytest.raises(PayloadError):
        _to_intent(_native("invoice.credit", data), provider="mycelium")


def test_native_float_line_fields_are_not_silently_absorbed() -> None:
    """KNOWN BUG (left failing on purpose).

    Same root cause as the credit ``amount``: ``as_decimal`` returns None for a
    float, and every caller reads None as "the sender said nothing" instead of
    "the sender said something we refuse to use".

    - ``quantity: 2.5`` silently becomes 1, so a 250.00 sale is invoiced at
      100.00;
    - ``vat_rate: 4.0`` silently becomes the connector default (22 here), so a
      4% line is filed at 22%.

    Either the value is refused (PayloadError, like ``unit_price``) or it is
    used exactly; silently substituting a different number is the one option
    that produces a wrong document nobody notices. Both fields are checked in
    one test so a single failure reports the whole family.
    """
    config = MapperConfig(default_vat_rate=Decimal(22))
    wrong: list[str] = []

    quantity_data = {
        "reference": "ORD-1",
        "customer": _native_customer(),
        "lines": [{"description": "Consulenza", "unit_price": "100.00", "quantity": 2.5}],
    }
    try:
        line = _as_emission(
            payment_native.MAPPER.to_intent(_native("invoice.issue", quantity_data), config=config)
        ).lines[0]
    except PayloadError:
        pass
    else:
        if line.quantity != Decimal("2.5"):
            wrong.append(f"quantity 2.5 -> {line.quantity}")

    rate_data = {
        "reference": "ORD-2",
        "customer": _native_customer(),
        "lines": [{"description": "Consulenza", "unit_price": "100.00", "vat_rate": 4.0}],
    }
    try:
        line = _as_emission(
            payment_native.MAPPER.to_intent(_native("invoice.issue", rate_data), config=config)
        ).lines[0]
    except PayloadError:
        pass
    else:
        if line.vat_rate != Decimal(4):
            wrong.append(f"vat_rate 4.0 -> {line.vat_rate}")

    assert wrong == [], f"float fields silently replaced by another value: {wrong}"


def test_native_requires_its_references() -> None:
    """The reference IS the idempotency key (it becomes the claimed object id),
    so a sender that omits it cannot be given one: two events would collapse
    onto one document, or one payment would be invoiced twice."""
    with pytest.raises(PayloadError):
        _to_intent(
            _native("invoice.issue", {"customer": _native_customer(), "lines": []}),
            provider="mycelium",
        )
    with pytest.raises(PayloadError):
        _to_intent(_native("invoice.issue", {"reference": "   ", "lines": []}), provider="mycelium")
    with pytest.raises(PayloadError):
        # A credit note without a parent has nothing to reverse.
        _to_intent(_native("invoice.credit", {"reference": "NC-1"}), provider="mycelium")
    with pytest.raises(PayloadError):
        _to_intent(_native("invoice.payment", {}), provider="mycelium")
    with pytest.raises(PayloadError):
        # An emission with no lines is not an amount of money.
        _to_intent(
            _native("invoice.issue", {"reference": "ORD-1", "lines": []}), provider="mycelium"
        )
    with pytest.raises(PayloadError):
        _to_intent({"id": "ev-1", "type": "invoice.issue"}, provider="mycelium")
    with pytest.raises(PayloadError):
        _to_intent({"type": "invoice.issue", "data": {"reference": "x"}}, provider="mycelium")


def test_native_lines_fall_back_to_the_connector_defaults() -> None:
    data = {
        "reference": "ORD-3",
        "customer": {"first_name": "Mario", "last_name": "Rossi", "tax_code": "RSSMRA80A01H501U"},
        "lines": [{"unit_price": "100.00"}],
    }
    intent = _as_emission(
        _to_intent(
            _native("invoice.issue", data),
            provider="mycelium",
            default_vat_rate=Decimal("10"),
            default_vat_nature="N2.2",
            default_line_description="Servizio digitale",
            default_country_code="IT",
            default_purpose="Vendita",
        )
    )

    line = intent.lines[0]
    assert line.vat_rate == Decimal(10), "a line with no rate takes the connector default"
    assert line.vat_nature == "N2.2"
    assert line.description == "Servizio digitale"
    assert line.quantity == Decimal(1), "an omitted quantity is one unit"
    assert intent.purpose == "Vendita"
    assert intent.party.legal_name == "Mario Rossi", "a natural person is named from both halves"
    assert intent.party.country_code == "IT"
    # The mapper reports what the PROVIDER said and nothing else. There is no
    # connector-wide codice destinatario to fall back on: 0000000 cannot be used
    # to send, so a document is only addressable when the counterpart supplied a
    # real code or a PEC (or is foreign, which the service resolves by rule).
    assert intent.party.sdi_code is None

    # An explicit rate beats the default, including an explicit zero.
    data["lines"] = [{"unit_price": "100.00", "vat_rate": "0"}]
    zero = _as_emission(
        _to_intent(
            _native("invoice.issue", data), provider="mycelium", default_vat_rate=Decimal("10")
        )
    )
    assert zero.lines[0].vat_rate == Decimal(0)


def test_native_unknown_event_type_is_ignored() -> None:
    """A closed vocabulary: a sender's typo shows up in the connector's event
    list as ignored instead of silently doing nothing."""
    intent = _to_intent(_native("invoice.void", {"reference": "ORD-1"}), provider="mycelium")
    assert isinstance(intent, IgnoreIntent)
    assert intent.reason == "event_type_not_mapped"


def test_native_is_emission_trigger_ignores_the_configured_event() -> None:
    """The contract defines exactly one emission type, so unlike Stripe there is
    nothing ambiguous to configure: ``emission_event`` (a Stripe vocabulary the
    column is constrained to) must not be able to switch it off."""
    mapper = payment_native.MAPPER
    for configured in ("invoice.paid", "payment_intent.succeeded", "checkout.session.completed"):
        config = MapperConfig(emission_event=configured)
        assert mapper.is_emission_trigger("invoice.issue", config) is True, configured
        assert mapper.is_emission_trigger(configured, config) is False, configured
        assert mapper.is_emission_trigger("invoice.credit", config) is False
        assert mapper.is_emission_trigger("invoice.payment", config) is False


# --- money -----------------------------------------------------------------


def test_minor_units_convert_exactly_for_every_currency_shape() -> None:
    """Providers send integer minor units and the exponent is per-currency:
    a JPY 1234 is 1234 yen, not 12.34, and reading it wrong is a 100x invoice."""
    assert minor_to_decimal(1234, "EUR") == Decimal("12.34")
    assert minor_to_decimal(1234, "eur") == Decimal("12.34"), "providers send lowercase codes"
    assert minor_to_decimal(1234, "JPY") == Decimal("1234"), "a zero-decimal currency"
    assert minor_to_decimal(1234, "BHD") == Decimal("1.234"), "a three-decimal currency"
    assert minor_to_decimal(1234, "") == Decimal("12.34"), "an absent code defaults to EUR"
    assert minor_to_decimal(1234, "XYZ") == Decimal("12.34"), "an unknown code assumes 2 decimals"
    assert minor_to_decimal(-6100, "EUR") == Decimal("-61.00"), "refunds are negative"
    assert minor_to_decimal(0, "EUR") == Decimal(0)


def test_minor_units_never_go_through_a_float() -> None:
    """The exactness claim, asserted rather than trusted: these identities hold
    in Decimal and fail in binary floating point, which is why a cent may never
    take that route on its way into a fiscal document."""
    cent = minor_to_decimal(1, "EUR")
    assert isinstance(cent, Decimal)
    assert cent * 3 == Decimal("0.03"), "0.1 + 0.2 arithmetic would drift here"
    assert sum((minor_to_decimal(1, "EUR") for _ in range(10)), Decimal(0)) == Decimal("0.10")
    assert minor_to_decimal(1234567890123, "EUR") == Decimal("12345678901.23")
    # A float route would produce 12.339999999999999857891452847979962825775146484375.
    assert str(minor_to_decimal(1234, "EUR")) == "12.34"
    assert minor_to_decimal(1234, "BHD") == Decimal(1234).scaleb(-3)


# --- allocate_partial ------------------------------------------------------


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


def test_allocate_partial_sums_exactly_to_the_target() -> None:
    """Naive per-line rounding drifts: 10.00 split three ways is 3.33 each and
    3.33*3 is 9.99, a cent short of the refund the customer actually got. The
    largest-remainder pass exists to close that gap, and a credit note that is a
    cent off its refund is a reconciliation the accountant has to chase."""
    a, b, c = _ids(3)
    allocation = svc.allocate_partial(
        [(a, Decimal("10.00")), (b, Decimal("10.00")), (c, Decimal("10.00"))], Decimal("10.00")
    )
    assert sum(allocation.values(), Decimal(0)) == Decimal("10.00")
    assert sorted(allocation.values()) == [Decimal("3.33"), Decimal("3.33"), Decimal("3.34")]

    # Seven lines into 5.00: the same drift, seven times over.
    ids = _ids(7)
    allocation = svc.allocate_partial([(i, Decimal("1.00")) for i in ids], Decimal("5.00"))
    assert sum(allocation.values(), Decimal(0)) == Decimal("5.00")
    assert all(value.as_tuple().exponent == -2 for value in allocation.values()), "cents only"

    # And a target that divides cleanly is left alone.
    x, y = _ids(2)
    allocation = svc.allocate_partial(
        [(x, Decimal("100.00")), (y, Decimal("100.00"))], Decimal("100.00")
    )
    assert allocation[x] == Decimal("50.00")
    assert allocation[y] == Decimal("50.00")


def test_allocate_partial_preserves_the_proportions() -> None:
    """Pro-rata is the point: each line keeps its own aliquota, so the split has
    to follow the line totals rather than being flattened onto one line."""
    small_a, small_b, big = _ids(3)
    lines = [
        (small_a, Decimal("0.05")),
        (small_b, Decimal("0.05")),
        (big, Decimal("0.90")),
    ]
    allocation = svc.allocate_partial(lines, Decimal("0.50"))

    assert sum(allocation.values(), Decimal(0)) == Decimal("0.50")
    # The big line keeps its exact 90% share; the odd cent goes to a line whose
    # remainder was largest (0.025 -> 0.02 + 0.005), never to the biggest line.
    assert allocation[big] == Decimal("0.45")
    assert sorted(allocation.values()) == [Decimal("0.02"), Decimal("0.03"), Decimal("0.45")]

    # Every allocation stays within one cent of its exact pro-rata share, which
    # is the strongest statement largest-remainder can make.
    total = Decimal("1.00")
    for line_id, amount in lines:
        exact = amount * Decimal("0.50") / total
        assert abs(allocation[line_id] - exact) <= Decimal("0.01"), line_id


def test_allocate_partial_returns_nothing_for_a_non_positive_total() -> None:
    """No lines, or lines that sum to zero or less, is not a division by zero
    and not a crash: there is simply nothing to scale."""
    a, b = _ids(2)
    assert svc.allocate_partial([], Decimal("10.00")) == {}
    assert svc.allocate_partial([(a, Decimal("0.00"))], Decimal("10.00")) == {}
    assert svc.allocate_partial([(a, Decimal("-5.00")), (b, Decimal("1.00"))], Decimal("10")) == {}


# --- net_unit_price --------------------------------------------------------


def test_net_unit_price_divides_out_an_included_vat() -> None:
    """``invoice.add_line`` treats unit_price as NET and adds VAT on top, so a
    gross figure has to be divided out first or the customer is billed 22% too
    much."""
    line = LineIn(
        description="Piano Pro",
        unit_price=Decimal("122.00"),
        vat_rate=Decimal(22),
        price_includes_vat=True,
    )
    assert svc.net_unit_price(line, fallback_rate=None) == Decimal("100.00")

    # The line's own rate wins; the connector default is only a fallback.
    assert svc.net_unit_price(line, fallback_rate=Decimal(10)) == Decimal("100.00")
    no_rate = LineIn(description="Piano Pro", unit_price=Decimal("122.00"), price_includes_vat=True)
    assert svc.net_unit_price(no_rate, fallback_rate=Decimal(22)) == Decimal("100.00")

    # Four decimals are kept: 10.00 gross at 22% is 8.1967, and quantising to
    # cents here would lose the precision FatturaPA allows on a unit price.
    small = LineIn(
        description="x", unit_price=Decimal("10.00"), vat_rate=Decimal(22), price_includes_vat=True
    )
    assert svc.net_unit_price(small, fallback_rate=None) == Decimal("8.1967")


def test_net_unit_price_passes_a_net_price_through() -> None:
    line = LineIn(description="x", unit_price=Decimal("100.00"), vat_rate=Decimal(22))
    assert line.price_includes_vat is False
    assert svc.net_unit_price(line, fallback_rate=Decimal(22)) == Decimal("100.00")


def test_net_unit_price_leaves_a_gross_price_alone_without_a_rate() -> None:
    """Documented behaviour: with no resolvable rate the figure passes through
    unchanged. Inventing a rate would be worse than letting the issuer's regime
    decide, and a forfettario issuer has no split to make in the first place."""
    line = LineIn(description="x", unit_price=Decimal("122.00"), price_includes_vat=True)
    assert svc.net_unit_price(line, fallback_rate=None) == Decimal("122.00")

    zero = LineIn(
        description="x", unit_price=Decimal("122.00"), vat_rate=Decimal(0), price_includes_vat=True
    )
    assert svc.net_unit_price(zero, fallback_rate=Decimal(22)) == Decimal("122.00")
    assert svc.net_unit_price(line, fallback_rate=Decimal(0)) == Decimal("122.00")
