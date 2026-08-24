"""Signed invoice webhooks (task 2c23e955, ADR-0047).

Covers the whole state machine WITHOUT touching the network: the sender is a
recording fake injected via ``set_webhook_sender_override``. The load-bearing
guarantees under test are (a) an enqueue fault can NEVER abort the fiscal tx it
rides on, (b) at-least-once idempotent delivery, (c) the SSRF destination guard,
and (d) the HMAC signature.
"""

from __future__ import annotations

import datetime
import types
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from mycelium_core.config import get_settings
from mycelium_core.crypto import decrypt_secret
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import ConflictError, UnprocessableError
from mycelium_core.models.invoice import ConservationStatus
from mycelium_core.sdi_channel import IntermediaryIdentity, TransmitResult
from mycelium_core.services import invoice as inv
from mycelium_core.services import sdi_mandate
from mycelium_core.services import webhooks as svc
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput, create_client


class _SuccessCoop:
    name = "sdicoop"

    @property
    def intermediary(self) -> IntermediaryIdentity | None:
        return IntermediaryIdentity(country_code="IT", vat_number="11122233344")

    async def transmit(self, *, xml: str, invoice_id: str, filename: str) -> TransmitResult:
        return TransmitResult(
            identificativo_sdi=f"SDI{uuid.uuid4().hex[:10].upper()}",
            conservation=ConservationStatus.ade_pending,
            channel=self.name,
        )


@pytest.fixture(autouse=True)
def _enable_webhooks(monkeypatch):
    """Fail-closed by default; the tests exercise the enabled path."""
    monkeypatch.setattr(get_settings(), "webhooks_enabled", True)
    yield


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="WHK")
    return r.org_id, r.user_id


async def _issuer_and_client(org: uuid.UUID, user: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    async with tenant_session(str(org), str(user)) as s:
        prof = await inv.create_issuer_profile(
            s,
            org_id=org,
            actor_id=user,
            label="P",
            legal_name="Acme Srl",
            vat_number="01234567890",
            address="Via Roma 1",
            postal_code="00100",
            city="Roma",
            is_default=True,
        )
        client = await create_client(
            s,
            org_id=org,
            actor_id=user,
            name="Client SpA",
            profile=ClientInput(
                legal_name="Client SpA",
                country_code="IT",
                vat_number="09876543210",
                sdi_code="ABCDEFG",
                address="Via Milano 2",
                postal_code="20100",
                city="Milano",
                province="MI",
            ),
        )
        return prof.id, client.id


async def _draft(org, user, client_id) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client_id, year=2026)
        await inv.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            description="svc",
            unit_price=Decimal(100),
        )
        return d.id


class RecordingSender:
    def __init__(self, results: list[svc.SendResult] | None = None) -> None:
        self.calls: list[dict] = []
        self.results = list(results or [])

    async def send(self, *, url, headers, body, timeout_s) -> svc.SendResult:
        self.calls.append({"url": url, "headers": dict(headers), "body": body})
        return self.results.pop(0) if self.results else svc.SendResult(ok=True, status_code=200)


# --- pure units -------------------------------------------------------------


def test_sign_is_deterministic_and_timestamp_bound() -> None:
    a = svc.sign("sec", 1000, b'{"x":1}')
    assert a == svc.sign("sec", 1000, b'{"x":1}')
    assert a != svc.sign("sec", 1001, b'{"x":1}')  # timestamp bound
    assert a != svc.sign("other", 1000, b'{"x":1}')  # secret bound
    assert len(a) == 64  # sha256 hex


def test_classify_rejects_non_public() -> None:
    assert svc._classify_ok("1.1.1.1")
    assert svc._classify_ok("8.8.8.8")
    for bad in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.1.1", "::1", "0.0.0.0"):  # noqa: S104
        assert not svc._classify_ok(bad), bad


async def test_assert_safe_destination() -> None:
    await svc.assert_safe_destination("https://1.1.1.1/hook")  # ok
    with pytest.raises(UnprocessableError):
        await svc.assert_safe_destination("http://1.1.1.1/hook")  # not https
    with pytest.raises(UnprocessableError):
        await svc.assert_safe_destination("https://127.0.0.1/hook")  # loopback
    with pytest.raises(UnprocessableError):
        await svc.assert_safe_destination("https://10.0.0.1/hook")  # private


def test_backoff_grows_and_caps() -> None:
    s = get_settings()
    assert svc._backoff_seconds(1) == s.webhook_backoff_base_seconds
    assert svc._backoff_seconds(2) == s.webhook_backoff_base_seconds * 2
    assert svc._backoff_seconds(99) == s.webhook_backoff_cap_seconds


def test_build_payload_is_pii_lean() -> None:
    fake = types.SimpleNamespace(
        id=uuid.uuid4(),
        series="A",
        number=7,
        year=2026,
        document_type=None,
        state=None,
        sdi_status=None,
        identificativo_sdi="IT123",
        buyer_verdict=None,
        payment_status=None,
        total=Decimal("12.34"),
        client_tag_id=uuid.uuid4(),
        issuer_profile_id=uuid.uuid4(),
    )
    now = datetime.datetime.now(tz=datetime.UTC)
    p = svc.build_invoice_payload(fake, event_type=svc.EVENT_TRANSMITTED, occurred_at=now)
    assert p["event"] == svc.EVENT_TRANSMITTED
    assert p["invoice"]["number"] == "A-7"  # type: ignore[index]
    assert p["invoice"]["total"] == "12.34"  # type: ignore[index]
    # No address/PEC/raw-xml leak.
    assert "raw_xml" not in p and "address" not in str(p)


# --- CRUD -------------------------------------------------------------------


async def test_create_lists_and_secret_roundtrips() -> None:
    org, user = await _org()
    issuer, _ = await _issuer_and_client(org, user)
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.create_endpoint(
            s,
            org_id=org,
            actor_id=user,
            issuer_profile_id=issuer,
            name="e",
            url="https://1.1.1.1/h",
        )
        assert res.secret.startswith("whsec_")
        assert decrypt_secret(res.endpoint.secret_ciphertext) == res.secret
    async with tenant_session(str(org), str(user)) as s:
        rows = await svc.list_endpoints(s, org_id=org, issuer_profile_id=issuer)
        assert len(rows) == 1 and rows[0].url == "https://1.1.1.1/h"


async def test_create_rejects_bad_url_and_event_type() -> None:
    org, user = await _org()
    issuer, _ = await _issuer_and_client(org, user)
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(UnprocessableError):
            await svc.create_endpoint(
                s,
                org_id=org,
                actor_id=user,
                issuer_profile_id=issuer,
                name="e",
                url="http://1.1.1.1/h",
            )
        with pytest.raises(UnprocessableError):
            await svc.create_endpoint(
                s,
                org_id=org,
                actor_id=user,
                issuer_profile_id=issuer,
                name="e",
                url="https://1.1.1.1/h",
                event_types=["invoice.nope"],
            )


async def test_revoke_cancels_pending_and_purge_requires_revoked() -> None:
    org, user = await _org()
    issuer, client = await _issuer_and_client(org, user)
    invoice_id = await _draft(org, user, client)
    async with tenant_session(str(org), str(user)) as s:
        ep = (
            await svc.create_endpoint(
                s,
                org_id=org,
                actor_id=user,
                issuer_profile_id=issuer,
                name="e",
                url="https://1.1.1.1/h",
            )
        ).endpoint
        invoice = await inv.get_invoice(s, org_id=org, invoice_id=invoice_id)
        await svc.enqueue_invoice_event(
            s,
            org_id=org,
            invoice=invoice,
            event_type=svc.EVENT_TRANSMITTED,
            dedupe_key=f"t:{invoice_id}",
            occurred_at=datetime.datetime.now(tz=datetime.UTC),
        )
    async with tenant_session(str(org), str(user)) as s:
        # Cannot purge an active endpoint.
        with pytest.raises(ConflictError):
            await svc.purge_endpoint(s, org_id=org, actor_id=user, endpoint_id=ep.id)
        await svc.revoke_endpoint(s, org_id=org, actor_id=user, endpoint_id=ep.id)
    async with tenant_session(str(org), str(user)) as s:
        deliveries = await svc.list_deliveries(s, org_id=org, endpoint_id=ep.id)
        assert deliveries and all(d.status == "dead" for d in deliveries)  # pending -> cancelled
        await svc.purge_endpoint(s, org_id=org, actor_id=user, endpoint_id=ep.id)
    async with tenant_session(str(org), str(user)) as s:
        assert await svc.list_endpoints(s, org_id=org, issuer_profile_id=issuer) == []


# --- enqueue ----------------------------------------------------------------


async def test_enqueue_fans_out_filters_and_dedupes() -> None:
    org, user = await _org()
    issuer, client = await _issuer_and_client(org, user)
    invoice_id = await _draft(org, user, client)
    async with tenant_session(str(org), str(user)) as s:
        # One subscribes to ALL, one only to 'delivered' (should NOT get a transmit).
        await svc.create_endpoint(
            s,
            org_id=org,
            actor_id=user,
            issuer_profile_id=issuer,
            name="all",
            url="https://1.1.1.1/all",
        )
        await svc.create_endpoint(
            s,
            org_id=org,
            actor_id=user,
            issuer_profile_id=issuer,
            name="deliv",
            url="https://1.1.1.1/deliv",
            event_types=[svc.EVENT_DELIVERED],
        )
        invoice = await inv.get_invoice(s, org_id=org, invoice_id=invoice_id)
        n = await svc.enqueue_invoice_event(
            s,
            org_id=org,
            invoice=invoice,
            event_type=svc.EVENT_TRANSMITTED,
            dedupe_key=f"transmitted:{invoice_id}",
            occurred_at=datetime.datetime.now(tz=datetime.UTC),
        )
        assert n == 1  # only the 'all' endpoint
        # Re-enqueue same dedupe_key -> ON CONFLICT DO NOTHING.
        n2 = await svc.enqueue_invoice_event(
            s,
            org_id=org,
            invoice=invoice,
            event_type=svc.EVENT_TRANSMITTED,
            dedupe_key=f"transmitted:{invoice_id}",
            occurred_at=datetime.datetime.now(tz=datetime.UTC),
        )
        assert n2 == 0


async def test_enqueue_disabled_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "webhooks_enabled", False)
    org, user = await _org()
    issuer, client = await _issuer_and_client(org, user)
    invoice_id = await _draft(org, user, client)
    async with tenant_session(str(org), str(user)) as s:
        await svc.create_endpoint(
            s,
            org_id=org,
            actor_id=user,
            issuer_profile_id=issuer,
            name="e",
            url="https://1.1.1.1/h",
        )
        invoice = await inv.get_invoice(s, org_id=org, invoice_id=invoice_id)
        assert (
            await svc.enqueue_invoice_event(
                s,
                org_id=org,
                invoice=invoice,
                event_type=svc.EVENT_TRANSMITTED,
                dedupe_key="x",
                occurred_at=datetime.datetime.now(tz=datetime.UTC),
            )
            == 0
        )


async def test_enqueue_fault_never_aborts_the_fiscal_tx(monkeypatch) -> None:
    """The load-bearing guarantee: a webhook fault is swallowed by the SAVEPOINT
    and the surrounding fiscal write still commits."""
    org, user = await _org()
    issuer, client = await _issuer_and_client(org, user)
    invoice_id = await _draft(org, user, client)

    def _boom(*a, **k):
        raise RuntimeError("payload builder exploded")

    monkeypatch.setattr(svc, "build_invoice_payload", _boom)
    async with tenant_session(str(org), str(user)) as s:
        await svc.create_endpoint(
            s,
            org_id=org,
            actor_id=user,
            issuer_profile_id=issuer,
            name="e",
            url="https://1.1.1.1/h",
        )
        invoice = await inv.get_invoice(s, org_id=org, invoice_id=invoice_id)
        n = await svc.enqueue_invoice_event(
            s,
            org_id=org,
            invoice=invoice,
            event_type=svc.EVENT_TRANSMITTED,
            dedupe_key="x",
            occurred_at=datetime.datetime.now(tz=datetime.UTC),
        )
        assert n == 0  # swallowed
        # The session is STILL usable and a fiscal write in the same tx commits.
        await inv.mark_paid(s, org_id=org, actor_id=user, invoice_id=invoice_id)
    async with tenant_session(str(org), str(user)) as s:
        again = await inv.get_invoice(s, org_id=org, invoice_id=invoice_id)
        assert again.payment_status.value == "paid"


# --- delivery ---------------------------------------------------------------


async def _one_pending(org, user, issuer, invoice_id, *, url="https://1.1.1.1/h") -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        ep = (
            await svc.create_endpoint(
                s, org_id=org, actor_id=user, issuer_profile_id=issuer, name="e", url=url
            )
        ).endpoint
        invoice = await inv.get_invoice(s, org_id=org, invoice_id=invoice_id)
        await svc.enqueue_invoice_event(
            s,
            org_id=org,
            invoice=invoice,
            event_type=svc.EVENT_TRANSMITTED,
            dedupe_key=f"t:{invoice_id}",
            occurred_at=datetime.datetime.now(tz=datetime.UTC),
        )
    return ep.id


async def test_deliver_success_signs_and_marks_delivered() -> None:
    org, user = await _org()
    issuer, client = await _issuer_and_client(org, user)
    invoice_id = await _draft(org, user, client)
    ep_id = await _one_pending(org, user, issuer, invoice_id)
    sender = RecordingSender([svc.SendResult(ok=True, status_code=200)])
    svc.set_webhook_sender_override(lambda: sender)
    try:
        async with tenant_session(str(org), str(user)) as s:
            delivered, failed = await svc.deliver_due(s, org_id=org)
        assert (delivered, failed) == (1, 0)
        assert len(sender.calls) == 1
        h = sender.calls[0]["headers"]
        assert h["x-webhook-event"] == svc.EVENT_TRANSMITTED
        assert h["x-webhook-signature"].startswith("v1=")
        # Verify the signature over the exact body with the endpoint secret.
        async with tenant_session(str(org), str(user)) as s:
            ep = (
                await s.execute(
                    text("SELECT secret_ciphertext FROM webhook_endpoints WHERE id=:e"),
                    {"e": str(ep_id)},
                )
            ).scalar_one()
        secret = decrypt_secret(ep)
        ts = int(h["x-webhook-timestamp"])
        assert h["x-webhook-signature"] == f"v1={svc.sign(secret, ts, sender.calls[0]['body'])}"
        async with tenant_session(str(org), str(user)) as s:
            d = (await svc.list_deliveries(s, org_id=org, endpoint_id=ep_id))[0]
            assert d.status == "delivered" and d.delivered_at is not None
    finally:
        svc.set_webhook_sender_override(None)


async def test_deliver_failure_backs_off_then_dies() -> None:
    org, user = await _org()
    issuer, client = await _issuer_and_client(org, user)
    invoice_id = await _draft(org, user, client)
    ep_id = await _one_pending(org, user, issuer, invoice_id)
    sender = RecordingSender()
    sender.send = _always_fail  # type: ignore[method-assign]
    svc.set_webhook_sender_override(lambda: sender)
    try:
        # First attempt fails -> status back to 'failed', next_attempt in the future.
        async with tenant_session(str(org), str(user)) as s:
            assert await svc.deliver_due(s, org_id=org) == (0, 1)
            d = (await svc.list_deliveries(s, org_id=org, endpoint_id=ep_id))[0]
            assert d.status == "failed" and d.attempt_count == 1
            # Force it due + at the last attempt, then one more sweep -> dead.
            await s.execute(
                text(
                    "UPDATE webhook_deliveries SET next_attempt_at = now() - interval '1 minute', "
                    "attempt_count = max_attempts - 1 WHERE endpoint_id = :e"
                ),
                {"e": str(ep_id)},
            )
        async with tenant_session(str(org), str(user)) as s:
            assert await svc.deliver_due(s, org_id=org) == (0, 1)
            d = (await svc.list_deliveries(s, org_id=org, endpoint_id=ep_id))[0]
            assert d.status == "dead"
    finally:
        svc.set_webhook_sender_override(None)


async def test_deliver_reclaims_expired_lease() -> None:
    org, user = await _org()
    issuer, client = await _issuer_and_client(org, user)
    invoice_id = await _draft(org, user, client)
    ep_id = await _one_pending(org, user, issuer, invoice_id)
    async with tenant_session(str(org), str(user)) as s:
        await s.execute(
            text(
                "UPDATE webhook_deliveries SET status='delivering', "
                "last_attempt_at = now() - interval '1 hour' WHERE endpoint_id=:e"
            ),
            {"e": str(ep_id)},
        )
    sender = RecordingSender([svc.SendResult(ok=True, status_code=200)])
    svc.set_webhook_sender_override(lambda: sender)
    try:
        async with tenant_session(str(org), str(user)) as s:
            delivered, _ = await svc.deliver_due(s, org_id=org)
        assert delivered == 1  # the stuck lease was reclaimed and re-sent
    finally:
        svc.set_webhook_sender_override(None)


async def _always_fail(*, url, headers, body, timeout_s) -> svc.SendResult:
    return svc.SendResult(ok=False, status_code=500, error="boom")


# --- integration: a real transmit fires the outbox ---------------------------


async def test_real_transmit_enqueues_transmitted_event() -> None:
    org, user = await _org()
    issuer, client = await _issuer_and_client(org, user)
    async with tenant_session(str(org), str(user)) as s:
        await sdi_mandate.grant_mandate(s, org_id=org, actor_id=user, issuer_profile_id=issuer)
    invoice_id = await _draft(org, user, client)
    async with tenant_session(str(org), str(user)) as s:
        ep = (
            await svc.create_endpoint(
                s,
                org_id=org,
                actor_id=user,
                issuer_profile_id=issuer,
                name="e",
                url="https://1.1.1.1/h",
            )
        ).endpoint
    # Transmit through a successful fake channel -> the fire site enqueues.
    async with tenant_session(str(org), str(user)) as s:
        done = await inv.transmit(
            s, org_id=org, actor_id=user, invoice_id=invoice_id, channel=_SuccessCoop()
        )
        assert done.identificativo_sdi is not None
    async with tenant_session(str(org), str(user)) as s:
        deliveries = await svc.list_deliveries(s, org_id=org, endpoint_id=ep.id)
        assert len(deliveries) == 1
        assert deliveries[0].event_type == svc.EVENT_TRANSMITTED
        assert deliveries[0].invoice_id == invoice_id
        assert deliveries[0].payload_snapshot["event"] == svc.EVENT_TRANSMITTED
