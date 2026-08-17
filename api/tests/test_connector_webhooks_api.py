"""Public payment-connector ingress over HTTP (ADR-0051).

Drives ``POST /api/v1/connectors/{provider}/{connector_id}`` through the real
ASGI app. That route is where the whole refusal ladder lives -- the feature
switch, tenant resolution, the body-size cap, the MAC over the raw bytes, the
optional second factor, the enabled switch and the payload parse -- and each
rung has BOTH a distinct status code and a distinct row in
``payment_webhook_deliveries``. Asserting only the status code would leave the
ledger untested, and the ledger is the whole reason this endpoint is auditable:
"the provider says it delivered this and there is no invoice" has to be
answerable from the database rather than from whichever pod's log survived.

Covers:

- the feature switch off, an unknown connector id, a revoked connector, and
  the right connector addressed under the wrong provider path: all 404, none
  of them writes anything anywhere;
- a valid signed Stripe event: 200 ``duplicate=false``, exactly one event row
  still ``pending`` (the handler does no fiscal work), exactly one ``accepted``
  delivery whose digest and length match the exact bytes sent;
- an exact redelivery: 200 ``duplicate=true``, still ONE event row, but TWO
  delivery rows -- the second ``duplicate``;
- a forged signature, a stale timestamp, a disabled connector, a body that is
  not JSON, JSON that is not an event, and a body over the configured cap:
  each one's status code AND the ledger row that explains it, with no event
  row behind any of them;
- the optional ingress API key: required once configured, and both the old and
  the new key working inside a rotation's grace window;
- the previous SIGNING secret still verifying inside that same grace window,
  and no longer verifying once the window has closed.

The last test in the file, ``test_over_long_provider_identifiers_are_refused_not_500``,
FAILS ON PURPOSE: it pins a confirmed defect in the route (an authentically
signed event whose id overflows its column answers 5xx and leaves no ledger
row). It is left red deliberately rather than weakened.

Every request is posted with httpx's ``content=``, never ``json=`` -- see
``_signed`` for why that is load-bearing rather than stylistic.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import time
import uuid
from collections.abc import Iterator

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from mycelium_api.main import app
from mycelium_core.config import get_settings
from mycelium_core.db import tenant_session
from mycelium_core.models.payment_connector import (
    PaymentConnector,
    PaymentConnectorEvent,
    PaymentWebhookDelivery,
)
from mycelium_core.services import payment_native
from mycelium_core.services.payment_events import timestamped_mac


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _client_seeing_500s() -> AsyncClient:
    """A client that reports an unhandled server error as a 500 RESPONSE.

    ``ASGITransport`` re-raises whatever the app raised by default, which is
    the right behaviour almost everywhere: a leaked exception should fail the
    test with its own traceback. It is the wrong behaviour when the assertion
    IS about the status code a sender observes, because a provider does not
    see a Python traceback -- it sees a 5xx, and it retries it. Used only by
    the over-long-identifier test below.
    """
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
    )


@contextlib.contextmanager
def _ingress_on(**overrides: str) -> Iterator[None]:
    """Switch the public ingress on for the body of one test.

    The route reads ``get_settings().payment_connectors_enabled`` INSIDE the
    handler, and ``get_settings`` is an ``lru_cache(maxsize=1)`` singleton that
    the test process already built at import time. Setting the environment
    variable therefore changes nothing on its own: clearing the cache is the
    half that makes it observable, which is why this is a deliberate helper and
    not a bare ``monkeypatch.setenv``.

    Restoring in a ``finally`` (and clearing the cache AGAIN) is equally
    load-bearing: a module that leaked an override would silently change the
    shape of the unauthenticated route for every test that ran afterwards.

    The subsystem ships ENABLED (the fail-closed guarantee lives per connector:
    one is created disabled and cannot exist without the provider's signing
    secret), so a test that wants the fleet kill switch OFF has to say so with
    ``_ingress_on(MYCELIUM_PAYMENT_CONNECTORS_ENABLED="false")`` rather than
    rely on a default.
    """
    env = {"MYCELIUM_PAYMENT_CONNECTORS_ENABLED": "true", **overrides}
    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    get_settings.cache_clear()
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        get_settings.cache_clear()


# --- tenant / connector fixtures (module-private, one fresh tenant per test) -


async def _setup(c: AsyncClient) -> tuple[dict[str, str], str, str, str]:
    """A fresh workspace with one issuer profile.

    Returns ``(headers, issuer_profile_id, org_id, user_id)``; the last two are
    what the ledger assertions need, because ``payment_webhook_deliveries`` is
    FORCE RLS and is only readable from a tenant session.
    """
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "PC"},
        )
    ).json()
    h = {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }
    p = (
        await c.post(
            "/issuer-profiles",
            headers=h,
            json={
                "label": "P",
                "legal_name": "Acme Srl",
                "vat_number": "01234567890",
                "address": "Via Roma 1",
                "postal_code": "00100",
                "city": "Roma",
            },
        )
    ).json()
    return h, p["id"], a["workspace_id"], a["user_id"]


async def _connector(
    c: AsyncClient,
    h: dict[str, str],
    issuer: str,
    *,
    provider: str = "stripe",
    enabled: bool = True,
    with_api_key: bool = False,
) -> tuple[str, str, str | None]:
    """Create a connector through the management REST surface.

    Deliberately NOT through the service: the signing secret an integrator
    actually uses is the one this response hands back once, so signing with it
    is what proves the two halves agree about the envelope.

    Returns ``(connector_id, signing_secret, api_key)``.
    """
    r = await c.post(
        f"/issuer-profiles/{issuer}/payment-connectors",
        headers=h,
        json={
            "label": f"pc-{uuid.uuid4().hex[:6]}",
            "provider": provider,
            "enabled": enabled,
            "with_api_key": with_api_key,
            # Only the native contract lets Mycelium mint the secret; a vendor
            # adapter must be given the one the vendor issued, or the connector
            # would hold a secret the vendor has never seen.
            **(
                {}
                if provider == "mycelium"
                else {"signing_secret": f"whsec_test_{uuid.uuid4().hex}"}
            ),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["id"], body["signing_secret"], body["api_key"]


# --- request construction ---------------------------------------------------


def _url(provider: str, connector_id: str) -> str:
    return f"/api/v1/connectors/{provider}/{connector_id}"


def _signed(secret: str, body: bytes, *, timestamp: int | None = None) -> dict[str, str]:
    """Stripe's own signature header over the EXACT bytes that go on the wire.

    ``t=<unix>,v1=<hex>`` where the hex is ``HMAC-SHA256(secret, f"{t}.{body}")``.

    Every request in this module is sent as ``c.post(url, content=body, ...)``
    and never ``json=...``. That is not a style choice: ``json=`` re-serialises
    the dict with httpx's own separators, so the bytes that reach the handler
    are not the bytes we hashed, the MAC legitimately fails, and the test would
    be measuring httpx's serialiser instead of the ingress. The same trap is
    why the router verifies before parsing -- a body normalised by
    ``json.loads`` no longer hashes to what the sender signed.
    """
    t = str(int(time.time()) if timestamp is None else timestamp)
    return {
        "Stripe-Signature": f"t={t},v1={timestamped_mac(secret, t, body)}",
        "Content-Type": "application/json",
    }


def _signed_native(secret: str, body: bytes, *, timestamp: int | None = None) -> dict[str, str]:
    """The same MAC, in the header shape OUR published contract defines.

    Same construction as Stripe's (that symmetry is deliberate, see ADR-0051),
    split across two headers instead of one packed value.
    """
    t = str(int(time.time()) if timestamp is None else timestamp)
    return {
        payment_native.TIMESTAMP_HEADER: t,
        payment_native.SIGNATURE_HEADER: f"v1={timestamped_mac(secret, t, body)}",
        "Content-Type": "application/json",
    }


def _native_event(event_id: str = "ev-1") -> bytes:
    """One event of the published contract, serialised ONCE (see ``_event``)."""
    return json.dumps(
        {
            "id": event_id,
            "type": "invoice.issue",
            "created": 1_755_000_000,
            "data": {
                "reference": f"ORD-{event_id}",
                "customer": {
                    "legal_name": "Acme SpA",
                    "country_code": "IT",
                    "vat_number": "IT09876543210",
                    "address": "Via Milano",
                    "civic_number": "9",
                    "postal_code": "20100",
                    "city": "Milano",
                    "province": "MI",
                    "sdi_code": "ABCDEFG",
                },
                "lines": [{"description": "Consulenza", "unit_price": "100.00"}],
            },
        },
        separators=(",", ":"),
    ).encode()


def _event(event_id: str = "evt_1") -> bytes:
    """One Stripe ``invoice.paid`` event, serialised ONCE.

    The BYTES are the unit of work here, not the dict: the MAC covers them and
    so does ``body_sha256``, so the value has to be frozen before it is either
    signed or sent.
    """
    return json.dumps(
        {
            "id": event_id,
            "type": "invoice.paid",
            "created": 1_755_000_000,
            "data": {
                "object": {
                    "id": "in_1",
                    "object": "invoice",
                    "currency": "eur",
                    "customer": "cus_1",
                    "customer_name": "Acme SpA",
                    "customer_address": {
                        "line1": "Via Milano 9",
                        "postal_code": "20100",
                        "city": "Milano",
                        "state": "MI",
                        "country": "IT",
                    },
                    "charge": "ch_1",
                    "payment_intent": "pi_1",
                    "metadata": {"vat_number": "IT09876543210", "sdi_code": "ABCDEFG"},
                    "total": 12200,
                    "lines": {"data": [{"description": "Piano Pro", "amount": 10000}]},
                }
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


# --- ledger readers ---------------------------------------------------------


async def _events(org: str, user: str, connector_id: str) -> list[PaymentConnectorEvent]:
    async with tenant_session(org, user) as s:
        return list(
            (
                await s.execute(
                    select(PaymentConnectorEvent)
                    .where(PaymentConnectorEvent.connector_id == uuid.UUID(connector_id))
                    .order_by(PaymentConnectorEvent.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _deliveries(org: str, user: str, connector_id: str) -> list[PaymentWebhookDelivery]:
    async with tenant_session(org, user) as s:
        return list(
            (
                await s.execute(
                    select(PaymentWebhookDelivery)
                    .where(PaymentWebhookDelivery.connector_id == uuid.UUID(connector_id))
                    .order_by(PaymentWebhookDelivery.received_at)
                )
            )
            .scalars()
            .all()
        )


async def _org_delivery_count(org: str, user: str) -> int:
    """Every delivery row this WORKSPACE owns, whatever connector it names.

    Used where the assertion is "nothing was written at all": an unattributable
    request must not be able to append a row anywhere in the tenant, not merely
    under the connector the test happens to know about.
    """
    async with tenant_session(org, user) as s:
        return len(list((await s.execute(select(PaymentWebhookDelivery.id))).scalars().all()))


# --- the three ways to get a 404 --------------------------------------------


async def test_feature_switch_off_is_404_and_writes_nothing() -> None:
    """The fleet kill switch: with it off the route must be indistinguishable
    from one that never existed, and must not leave evidence of the attempt.

    It is not the shipped default any more -- a connector is armed per issuer
    profile -- but it stays the lever that takes the whole ingress down without
    editing a single connector, so it has to keep working.
    """
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, _key = await _connector(c, h, issuer)
        body = _event()

        with _ingress_on(MYCELIUM_PAYMENT_CONNECTORS_ENABLED="false"):
            r = await c.post(_url("stripe", cid), content=body, headers=_signed(secret, body))
            assert r.status_code == 404, r.text
            assert r.json()["code"] == "payment_connector.not_found"

        assert await _events(org, user, cid) == []
        assert await _deliveries(org, user, cid) == []


async def test_unknown_connector_id_is_404_and_records_nothing() -> None:
    """An id that resolves to nothing has no tenant to attribute a delivery to,
    so the ledger must stay empty -- otherwise this table would be the
    attacker-writable surface it exists to audit."""
    async with _client() as c:
        _h, _issuer, org, user = await _setup(c)
        stranger = str(uuid.uuid4())
        body = _event()

        with _ingress_on():
            r = await c.post(
                _url("stripe", stranger),
                content=body,
                headers=_signed("whsec_whatever", body),
            )
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "payment_connector.not_found"

        assert await _deliveries(org, user, stranger) == []
        assert await _org_delivery_count(org, user) == 0


async def test_connector_under_the_wrong_provider_path_is_404() -> None:
    """The provider segment is part of the routing identity. A stripe connector
    reached at /mycelium/{id} answers exactly like an unknown id, so the surface
    is not an oracle for which adapter a given uuid is configured with."""
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, _key = await _connector(c, h, issuer, provider="stripe")
        body = _event()

        with _ingress_on():
            r = await c.post(_url("mycelium", cid), content=body, headers=_signed(secret, body))
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "payment_connector.not_found"

        # Refused BEFORE the connector was accepted as the addressee, so there
        # is nothing to record even though a tenant could have been named.
        assert await _deliveries(org, user, cid) == []
        assert await _events(org, user, cid) == []


async def test_revoked_connector_is_404_and_records_nothing() -> None:
    """Revocation is the kill switch, and it must not be distinguishable from
    "never existed": the resolver returns nothing, so the ingress cannot even
    tell the caller that the id it holds used to be live."""
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, _key = await _connector(c, h, issuer)
        revoked = await c.request(
            "DELETE", f"/issuer-profiles/{issuer}/payment-connectors/{cid}", headers=h
        )
        assert revoked.status_code == 204, revoked.text

        body = _event("evt_revoked")
        with _ingress_on():
            r = await c.post(_url("stripe", cid), content=body, headers=_signed(secret, body))
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "payment_connector.not_found"

        assert await _events(org, user, cid) == []
        assert await _deliveries(org, user, cid) == []


# --- the happy path and its redelivery --------------------------------------


async def test_valid_signed_event_is_accepted_and_ledgered() -> None:
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, _key = await _connector(c, h, issuer, enabled=True)
        body = _event("evt_ok")

        with _ingress_on():
            r = await c.post(_url("stripe", cid), content=body, headers=_signed(secret, body))
        assert r.status_code == 200, r.text
        assert r.json() == {"received": True, "duplicate": False}
        assert r.headers["cache-control"] == "no-store"

        events = await _events(org, user, cid)
        assert len(events) == 1
        event = events[0]
        assert event.provider_event_id == "evt_ok"
        assert event.event_type == "invoice.paid"
        # The handler persists and answers; every fiscal decision is the
        # worker's. A document composed inline would blow the provider's
        # webhook timeout and be redelivered while the first attempt filed.
        assert event.status == "pending"
        assert event.invoice_id is None
        assert event.attempt_count == 0
        assert event.occurred_at is not None
        # The payload is frozen verbatim, so a reprocess is deterministic.
        assert event.payload == json.loads(body)

        deliveries = await _deliveries(org, user, cid)
        assert len(deliveries) == 1
        d = deliveries[0]
        assert d.outcome == "accepted"
        assert d.http_status == 200
        assert d.provider == "stripe"
        assert d.event_id == event.id
        assert d.provider_event_id == "evt_ok"
        assert d.body_bytes == len(body)
        # The digest is over the EXACT bytes sent, which is what makes the row
        # verifiable by anyone who still holds the original body.
        assert d.body_sha256 == hashlib.sha256(body).digest()
        assert d.signature_present is True
        assert d.api_key_present is False

        async with tenant_session(org, user) as s:
            row = (
                await s.execute(
                    select(PaymentConnector).where(PaymentConnector.id == uuid.UUID(cid))
                )
            ).scalar_one()
            assert row.last_event_at is not None, "an accepted event must stamp the connector"


async def test_exact_redelivery_is_a_duplicate_with_two_delivery_rows() -> None:
    """The point of splitting the two ledgers.

    ``payment_connector_events`` records what we agreed to ACT on and must not
    grow on a redelivery; ``payment_webhook_deliveries`` records what actually
    ARRIVED and must grow every single time, or the second delivery is
    invisible and the sender's "I sent it twice" is unanswerable.
    """
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, _key = await _connector(c, h, issuer)
        body = _event("evt_dup")
        headers = _signed(secret, body)

        with _ingress_on():
            # Byte-identical request, signature header included: a provider
            # retry inside the replay window. Dedup keys on the event id, not
            # on the bytes and not on the MAC.
            first = await c.post(_url("stripe", cid), content=body, headers=headers)
            second = await c.post(_url("stripe", cid), content=body, headers=headers)

        assert first.status_code == 200, first.text
        assert first.json() == {"received": True, "duplicate": False}
        # A redelivery IS a success for the sender: anything but 2xx makes the
        # provider retry a duplicate forever.
        assert second.status_code == 200, second.text
        assert second.json() == {"received": True, "duplicate": True}

        events = await _events(org, user, cid)
        assert len(events) == 1, "a redelivery must never create a second event row"

        deliveries = await _deliveries(org, user, cid)
        assert len(deliveries) == 2, "both arrivals must be on the ledger"
        assert [d.outcome for d in deliveries] == ["accepted", "duplicate"]
        assert {d.http_status for d in deliveries} == {200}
        # Both point at the one event, and both hash to the one body.
        assert {d.event_id for d in deliveries} == {events[0].id}
        assert {d.body_sha256 for d in deliveries} == {hashlib.sha256(body).digest()}
        assert {d.provider_event_id for d in deliveries} == {"evt_dup"}


# --- refusals ---------------------------------------------------------------


async def test_forged_signature_is_401_and_leaves_a_refusal_row() -> None:
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, _secret, _key = await _connector(c, h, issuer)
        body = _event("evt_forged")

        with _ingress_on():
            r = await c.post(
                _url("stripe", cid),
                content=body,
                headers=_signed("whsec_not_the_configured_secret", body),
            )
        assert r.status_code == 401, r.text
        assert r.json()["code"] == "payment_connector.signature_invalid"

        # The refusal is recorded in its OWN committed session, so it survives
        # the exception that produced the 401.
        deliveries = await _deliveries(org, user, cid)
        assert len(deliveries) == 1
        assert deliveries[0].outcome == "signature_invalid"
        assert deliveries[0].http_status == 401
        assert deliveries[0].event_id is None
        assert deliveries[0].provider_event_id is None
        assert deliveries[0].body_sha256 == hashlib.sha256(body).digest()
        assert deliveries[0].signature_present is True

        # An unauthenticated body must never reach the work queue.
        assert await _events(org, user, cid) == []


async def test_stale_timestamp_beyond_tolerance_is_refused() -> None:
    """The MAC itself is correct here; only the bound timestamp is old. Without
    the replay window a captured request would stay replayable forever."""
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, _key = await _connector(c, h, issuer)
        body = _event("evt_stale")
        stale = int(time.time()) - (get_settings().payment_connector_tolerance_seconds + 120)

        with _ingress_on():
            r = await c.post(
                _url("stripe", cid), content=body, headers=_signed(secret, body, timestamp=stale)
            )
        assert r.status_code == 401, r.text
        assert r.json()["code"] == "payment_connector.signature_invalid"

        deliveries = await _deliveries(org, user, cid)
        assert len(deliveries) == 1
        assert deliveries[0].outcome == "signature_invalid"
        assert deliveries[0].http_status == 401
        assert await _events(org, user, cid) == []


async def test_disabled_connector_with_a_valid_signature_is_403() -> None:
    """Past the signature the caller is authentic, so a precise answer costs
    nothing and tells the operator exactly why their events are bouncing."""
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, _key = await _connector(c, h, issuer, enabled=False)
        body = _event("evt_off")

        with _ingress_on():
            r = await c.post(_url("stripe", cid), content=body, headers=_signed(secret, body))
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "payment_connector.disabled"

        deliveries = await _deliveries(org, user, cid)
        assert len(deliveries) == 1
        assert deliveries[0].outcome == "disabled"
        assert deliveries[0].http_status == 403
        assert await _events(org, user, cid) == [], "a disabled connector must queue nothing"


async def test_authentic_body_that_is_not_json_is_400_payload_invalid() -> None:
    """A correctly signed body still has to BE an event. The signature proves
    who sent it, not that it parses."""
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, _key = await _connector(c, h, issuer)
        body = b"this is definitely not json"

        with _ingress_on():
            r = await c.post(_url("stripe", cid), content=body, headers=_signed(secret, body))
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "payment_connector.payload_invalid"

        deliveries = await _deliveries(org, user, cid)
        assert len(deliveries) == 1
        assert deliveries[0].outcome == "payload_invalid"
        assert deliveries[0].http_status == 400
        assert deliveries[0].body_bytes == len(body)
        assert await _events(org, user, cid) == []


async def test_authentic_json_that_is_not_an_event_is_400_payload_invalid() -> None:
    """Two shapes that parse as JSON but carry no event identity: an object
    without ``id``/``type``, and a top-level array. Both are refused, and both
    leave a row, because an integrator debugging a silent feed needs to see
    that their POST arrived and why it bounced."""
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, _key = await _connector(c, h, issuer)
        no_identity = b'{"hello":"world"}'
        not_an_object = b"[1,2,3]"

        with _ingress_on():
            first = await c.post(
                _url("stripe", cid), content=no_identity, headers=_signed(secret, no_identity)
            )
            second = await c.post(
                _url("stripe", cid), content=not_an_object, headers=_signed(secret, not_an_object)
            )

        assert first.status_code == 400, first.text
        assert first.json()["code"] == "payment_connector.payload_invalid"
        assert second.status_code == 400, second.text
        assert second.json()["code"] == "payment_connector.payload_invalid"

        deliveries = await _deliveries(org, user, cid)
        assert len(deliveries) == 2
        assert [d.outcome for d in deliveries] == ["payload_invalid", "payload_invalid"]
        assert [d.body_sha256 for d in deliveries] == [
            hashlib.sha256(no_identity).digest(),
            hashlib.sha256(not_an_object).digest(),
        ]
        assert await _events(org, user, cid) == []


async def test_body_over_the_cap_is_refused_as_too_large() -> None:
    """The cap bounds the MAC work an unauthenticated caller can force, so it
    is checked BEFORE verification: a perfectly valid signature over an
    oversized body is still refused, and the row says ``too_large`` rather than
    ``signature_invalid``.

    The cap is shrunk instead of shipping a megabyte through the suite; that
    also proves the knob is read per request rather than baked in.
    """
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, _key = await _connector(c, h, issuer)
        body = json.dumps(
            {
                "id": "evt_big",
                "type": "invoice.paid",
                "created": 1_755_000_000,
                "data": {"object": {"id": "in_big", "note": "x" * 2000}},
            },
            separators=(",", ":"),
        ).encode()

        with _ingress_on(MYCELIUM_PAYMENT_CONNECTOR_MAX_BODY_BYTES="512"):
            assert len(body) > 512
            r = await c.post(_url("stripe", cid), content=body, headers=_signed(secret, body))
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "payment_connector.payload_invalid"

        deliveries = await _deliveries(org, user, cid)
        assert len(deliveries) == 1
        assert deliveries[0].outcome == "too_large"
        assert deliveries[0].http_status == 400
        assert deliveries[0].body_bytes == len(body)
        assert await _events(org, user, cid) == []


# --- the optional second factor ---------------------------------------------


async def test_a_connector_without_a_secret_verifies_nothing() -> None:
    """A vendor connector exists before its provider has issued a secret, so
    "cannot verify yet" is a normal state -- and it must FAIL CLOSED. Anything
    else would put an empty or guessable value on the one path that has no
    other authority."""
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        r = await c.post(
            f"/issuer-profiles/{issuer}/payment-connectors",
            headers=h,
            json={"label": f"pc-{uuid.uuid4().hex[:6]}"},
        )
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        assert r.json()["signing_secret"] is None

        with _ingress_on():
            body = _event("evt_nosecret")
            # Signed with something -- anything -- since there is nothing to
            # sign with. The point is that no presented signature can pass.
            res = await c.post(
                _url("stripe", cid),
                content=body,
                headers=_signed("whsec_attacker_guess", body),
            )
        assert res.status_code == 401, res.text
        assert res.json()["code"] == "payment_connector.signature_invalid"
        assert await _events(org, user, cid) == []


async def test_the_delivery_ledger_stops_growing_under_a_flood() -> None:
    """What an unauthenticated caller who learned the URL can make us WRITE.

    Refusing costs nothing; recording the refusal costs a row, and rows are
    unbounded. Past the window's budget the refusal is unchanged -- same 401,
    nothing learned about a limit -- and only the append stops, so the ledger
    keeps the beginning of the flood (which is what an operator reads) instead
    of all of it.
    """
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, _key = await _connector(c, h, issuer)

        budget = 3
        with _ingress_on(MYCELIUM_PAYMENT_CONNECTOR_REFUSAL_BUDGET=str(budget)):
            for i in range(budget + 4):
                body = _event(f"evt_flood_{i}")
                res = await c.post(
                    _url("stripe", cid),
                    content=body,
                    headers=_signed("whsec_wrong_secret_entirely", body),
                )
                assert res.status_code == 401, res.text

        deliveries = await _deliveries(org, user, cid)
        assert len(deliveries) == budget, (
            "the ledger must stop at the budget, not grow with the flood"
        )
        assert all(d.outcome == "signature_invalid" for d in deliveries)

        # And the connector still works: the cap counts REFUSALS, so whoever
        # holds the signing secret is never affected by someone else's noise.
        with _ingress_on(MYCELIUM_PAYMENT_CONNECTOR_REFUSAL_BUDGET=str(budget)):
            good = _event("evt_legit")
            res = await c.post(_url("stripe", cid), content=good, headers=_signed(secret, good))
        assert res.status_code == 200, res.text
        assert [e.provider_event_id for e in await _events(org, user, cid)] == ["evt_legit"]


async def test_ingress_api_key_is_mandatory_once_configured() -> None:
    """With a key armed, a valid signature alone is no longer enough. The
    refusal is collapsed onto the signature answer on purpose: a caller must
    not learn WHICH factor failed, nor that a key is required at all."""
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, key = await _connector(c, h, issuer, provider="mycelium", with_api_key=True)
        assert key is not None and key.startswith("mycelium_pc_")

        with _ingress_on():
            body = _native_event("evt_nokey")
            missing = await c.post(
                _url("mycelium", cid), content=body, headers=_signed_native(secret, body)
            )
            wrong = await c.post(
                _url("mycelium", cid),
                content=body,
                headers={
                    **_signed_native(secret, body),
                    "X-Connector-Api-Key": "mycelium_pc_wrong",
                },
            )
            good_body = _native_event("evt_withkey")
            good = await c.post(
                _url("mycelium", cid),
                content=good_body,
                headers={**_signed_native(secret, good_body), "X-Connector-Api-Key": key},
            )

        assert missing.status_code == 401, missing.text
        assert missing.json()["code"] == "payment_connector.signature_invalid"
        assert wrong.status_code == 401, wrong.text
        assert good.status_code == 200, good.text
        assert good.json() == {"received": True, "duplicate": False}

        # Only the authenticated one made it into the queue.
        events = await _events(org, user, cid)
        assert [e.provider_event_id for e in events] == ["evt_withkey"]

        deliveries = await _deliveries(org, user, cid)
        assert [d.outcome for d in deliveries] == [
            "signature_invalid",
            "signature_invalid",
            "accepted",
        ]
        # The ledger still records whether a key was PRESENTED, which is what
        # separates "they forgot the header" from "their key is stale".
        assert [d.api_key_present for d in deliveries] == [False, True, True]


async def test_rotated_api_key_accepts_both_keys_in_the_grace_window() -> None:
    """A rotation must never drop an in-flight redelivery, so the previous key
    keeps working until its window expires."""
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, old_key = await _connector(
            c, h, issuer, provider="mycelium", with_api_key=True
        )
        assert old_key is not None

        rotated = await c.post(
            f"/issuer-profiles/{issuer}/payment-connectors/{cid}/rotate-api-key", headers=h
        )
        assert rotated.status_code == 200, rotated.text
        new_key = rotated.json()["api_key"]
        assert new_key is not None and new_key != old_key

        with _ingress_on():
            with_old = _native_event("evt_old_key")
            r_old = await c.post(
                _url("mycelium", cid),
                content=with_old,
                headers={**_signed_native(secret, with_old), "X-Connector-Api-Key": old_key},
            )
            with_new = _native_event("evt_new_key")
            r_new = await c.post(
                _url("mycelium", cid),
                content=with_new,
                headers={**_signed_native(secret, with_new), "X-Connector-Api-Key": new_key},
            )

        assert r_old.status_code == 200, r_old.text
        assert r_new.status_code == 200, r_new.text
        events = await _events(org, user, cid)
        assert sorted(e.provider_event_id for e in events) == ["evt_new_key", "evt_old_key"]


async def test_previous_signing_secret_still_verifies_in_the_grace_window() -> None:
    """Same guarantee on the signing side: the provider's retry queue may still
    hold events signed with the secret we just replaced."""
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, old_secret, _key = await _connector(c, h, issuer)

        # The new value is PASTED, the way a real Stripe rotation works: the
        # dashboard rolls the endpoint secret and shows it once. Mycelium
        # refuses to mint one for a vendor provider, because a secret Stripe
        # never issued would make every delivery fail its signature check.
        rotated = await c.post(
            f"/issuer-profiles/{issuer}/payment-connectors/{cid}/rotate-signing-secret",
            headers=h,
            json={"signing_secret": f"whsec_rolled_in_stripe_{uuid.uuid4().hex}"},
        )
        assert rotated.status_code == 200, rotated.text
        new_secret = rotated.json()["signing_secret"]
        assert new_secret and new_secret != old_secret

        with _ingress_on():
            queued = _event("evt_old_secret")
            r_old = await c.post(
                _url("stripe", cid), content=queued, headers=_signed(old_secret, queued)
            )
            fresh = _event("evt_new_secret")
            r_new = await c.post(
                _url("stripe", cid), content=fresh, headers=_signed(new_secret, fresh)
            )

        assert r_old.status_code == 200, r_old.text
        assert r_new.status_code == 200, r_new.text
        events = await _events(org, user, cid)
        assert sorted(e.provider_event_id for e in events) == ["evt_new_secret", "evt_old_secret"]
        assert all(d.outcome == "accepted" for d in await _deliveries(org, user, cid))


async def test_expired_grace_window_stops_honouring_the_old_secret() -> None:
    """The other half of the rotation guarantee. A grace window that never
    closed would mean a leaked secret stays valid forever, which is the whole
    reason to rotate. The expiry is evaluated in SQL by the resolver, so the
    window is aged in the row rather than waited out for a day.
    """
    async with _client() as c:
        h, issuer, org, user = await _setup(c)
        cid, old_secret, _key = await _connector(c, h, issuer)

        # The new value is PASTED, the way a real Stripe rotation works: the
        # dashboard rolls the endpoint secret and shows it once. Mycelium
        # refuses to mint one for a vendor provider, because a secret Stripe
        # never issued would make every delivery fail its signature check.
        rotated = await c.post(
            f"/issuer-profiles/{issuer}/payment-connectors/{cid}/rotate-signing-secret",
            headers=h,
            json={"signing_secret": f"whsec_rolled_in_stripe_{uuid.uuid4().hex}"},
        )
        assert rotated.status_code == 200, rotated.text
        new_secret = rotated.json()["signing_secret"]

        async with tenant_session(org, user) as s:
            await s.execute(
                update(PaymentConnector)
                .where(PaymentConnector.id == uuid.UUID(cid))
                .values(
                    previous_signing_secret_expires_at=datetime.datetime.now(tz=datetime.UTC)
                    - datetime.timedelta(hours=1)
                )
            )

        with _ingress_on():
            stale = _event("evt_after_grace")
            r_old = await c.post(
                _url("stripe", cid), content=stale, headers=_signed(old_secret, stale)
            )
            fresh = _event("evt_current_secret")
            r_new = await c.post(
                _url("stripe", cid), content=fresh, headers=_signed(new_secret, fresh)
            )

        assert r_old.status_code == 401, "an expired grace secret must stop verifying"
        assert r_old.json()["code"] == "payment_connector.signature_invalid"
        assert r_new.status_code == 200, r_new.text

        events = await _events(org, user, cid)
        assert [e.provider_event_id for e in events] == ["evt_current_secret"]


# --- KNOWN BUG (failing on purpose, see the report) --------------------------


async def test_over_long_provider_identifiers_are_refused_not_500() -> None:
    """FAILING: an authentic event whose id/type overflows its column 500s.

    ``payment_connector_events.provider_event_id`` is ``varchar(255)`` and
    ``event_type`` is ``varchar(80)``, but neither the mapper's ``identify``
    nor ``svc.ingest`` bounds what it read out of the body. A correctly SIGNED
    event carrying a longer value therefore reaches the INSERT and Postgres
    raises ``StringDataRightTruncation``, which is not a ``DomainError`` and so
    escapes every handler as a 500.

    Two things make that worse than an ugly status code:

    - the sender is a payment provider, and a 5xx is precisely the answer that
      makes it retry -- forever, on a body that can never be accepted;
    - the delivery row shares the event's transaction, so it is rolled back
      with the failed INSERT. The one situation the ledger exists to answer
      ("the provider says it sent it and there is no invoice") is exactly the
      one where nothing is written down.

    The asymmetry looks like an oversight rather than a contract: the SAME
    value is defensively truncated (``provider_event_id[:255]``) on the way
    into ``record_delivery``, one function away, and the published native
    contract (docs/payment-connector-contract.md §5) documents ``id`` as "your
    unique id for this event" with no length bound at all.

    The assertion is deliberately fix-agnostic: refusing with a 400
    ``payload_invalid`` and widening/truncating the columns are both correct
    answers. What is not correct is a 5xx with no ledger row.
    """
    async with _client_seeing_500s() as c:
        h, issuer, org, user = await _setup(c)
        cid, secret, _key = await _connector(c, h, issuer)

        long_id = json.dumps(
            {"id": "evt_" + "a" * 300, "type": "invoice.paid", "data": {"object": {"id": "in_1"}}},
            separators=(",", ":"),
        ).encode()
        long_type = json.dumps(
            {"id": "evt_lt", "type": "custom." + "b" * 200, "data": {"object": {"id": "in_2"}}},
            separators=(",", ":"),
        ).encode()

        with _ingress_on():
            r_id = await c.post(
                _url("stripe", cid), content=long_id, headers=_signed(secret, long_id)
            )
            r_type = await c.post(
                _url("stripe", cid), content=long_type, headers=_signed(secret, long_type)
            )

        assert r_id.status_code < 500, f"over-long event id answered {r_id.status_code}"
        assert r_type.status_code < 500, f"over-long event type answered {r_type.status_code}"
        assert await _deliveries(org, user, cid) != [], "a refused delivery must still be recorded"
