"""Signed outbound webhooks on invoice state changes (task 2c23e955, ADR-0047).

Design invariants (see the ADR + the design review folded into it):

- **Fiscal safety first.** :func:`enqueue_invoice_event` runs inside a SAVEPOINT
  and swallows every error: a webhook fan-out can NEVER abort the transmit /
  ingest transaction it rides on. A bare INSERT that errors would otherwise poison
  the whole Postgres transaction and roll back the fiscal write.
- **Transactional outbox.** The emit only INSERTs ``webhook_deliveries`` rows
  (frozen payload snapshot) in the same tx as the state change; a decoupled worker
  does the network I/O. So the delivery is durable iff the state change committed,
  and a slow/hostile receiver never touches the request path.
- **At-least-once, idempotent.** ``UNIQUE(endpoint_id, dedupe_key)`` +
  ``ON CONFLICT DO NOTHING`` means the double-fire paths (SdI redelivery, lost-ACK
  reconcile) enqueue once; the receiver dedupes on the stable ``X-Webhook-Id``.
- **Signed + SSRF-guarded.** Each POST carries an HMAC-SHA256 over
  ``{timestamp}.{body}``; the destination is re-resolved and re-classified at send
  time (not just at create) so a public host cannot rebind to a private address.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.crypto import decrypt_secret, encrypt_secret
from mycelium_core.errors import ConflictError, NotFoundError, UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.invoice import Invoice
from mycelium_core.models.membership import Role
from mycelium_core.models.webhook import WebhookDelivery, WebhookEndpoint
from mycelium_core.services import audit
from mycelium_core.services.rbac import require_role

_log = logging.getLogger("mycelium.webhooks")

# --- event vocabulary -------------------------------------------------------

EVENT_TRANSMITTED = "invoice.transmitted"
EVENT_DELIVERED = "invoice.delivered"
EVENT_ACCEPTED = "invoice.accepted"
EVENT_REJECTED = "invoice.rejected"
EVENT_DEEMED_ACCEPTED = "invoice.deemed_accepted"
EVENT_PAYMENT_RECORDED = "invoice.payment_recorded"
VALID_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_TRANSMITTED,
        EVENT_DELIVERED,
        EVENT_ACCEPTED,
        EVENT_REJECTED,
        EVENT_DEEMED_ACCEPTED,
        EVENT_PAYMENT_RECORDED,
    }
)

PAYLOAD_SCHEMA_VERSION = 1
_SECRET_ENTROPY_BYTES = 32
_SECRET_PREFIX = "whsec_"  # noqa: S105 - a public prefix, not a secret


# --- secret + signature -----------------------------------------------------


def _generate_secret() -> str:
    import secrets

    return f"{_SECRET_PREFIX}{secrets.token_urlsafe(_SECRET_ENTROPY_BYTES)}"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 hex over ``{timestamp}.{body}`` -- the timestamp is bound in
    so a captured body cannot be replayed under a fresh signature, and the
    receiver rejects a skewed timestamp."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return mac.hexdigest()


# --- SSRF destination guard -------------------------------------------------


def _classify_ok(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # Reject anything that is not a routable public unicast address.
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def assert_safe_destination(url: str) -> None:
    """Fail-closed URL guard, run at BOTH create and send. HTTPS only, a real
    host, and EVERY resolved address must be public unicast (a single private
    answer rejects the whole name, defeating DNS-rebinding). Raises
    :class:`UnprocessableError` (create) -- the worker catches it as a failed
    attempt."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UnprocessableError(MessageCode.WEBHOOK_URL_INVALID, detail="must be https")
    host = parts.hostname
    if not host:
        raise UnprocessableError(MessageCode.WEBHOOK_URL_INVALID, detail="missing host")
    # A bare IP literal is classified directly (no DNS).
    try:
        ipaddress.ip_address(host)
        literals = [host]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, parts.port or 443, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise UnprocessableError(
                MessageCode.WEBHOOK_URL_INVALID, detail=f"cannot resolve host: {exc}"
            ) from exc
        literals = [str(info[4][0]) for info in infos]
    if not literals or not all(_classify_ok(a) for a in literals):
        raise UnprocessableError(
            MessageCode.WEBHOOK_URL_INVALID, detail="resolves to a non-public address"
        )


# --- sender seam ------------------------------------------------------------


@dataclass(frozen=True)
class SendResult:
    ok: bool
    status_code: int | None = None
    excerpt: str | None = None
    error: str | None = None


@runtime_checkable
class WebhookSender(Protocol):
    async def send(
        self, *, url: str, headers: dict[str, str], body: bytes, timeout_s: float
    ) -> SendResult: ...


class DefaultSender:
    """Reference sender (real httpx). CI injects a recording fake via
    :func:`set_webhook_sender_override`; the network path is not exercised in
    tests, the claim/backoff/dedupe state machine is."""

    async def send(
        self, *, url: str, headers: dict[str, str], body: bytes, timeout_s: float
    ) -> SendResult:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
                resp = await client.post(url, content=body, headers=headers)
            excerpt = resp.text[:512] if resp.text else None
            return SendResult(
                ok=200 <= resp.status_code < 300,
                status_code=resp.status_code,
                excerpt=excerpt,
            )
        except Exception as exc:
            return SendResult(ok=False, error=str(exc)[:512])


_override: Callable[[], WebhookSender] | None = None


def set_webhook_sender_override(fn: Callable[[], WebhookSender] | None) -> None:
    global _override
    _override = fn


def get_sender() -> WebhookSender:
    return _override() if _override is not None else DefaultSender()


# --- payload ----------------------------------------------------------------


def _v(value: object) -> object:
    """Enum -> its value; Decimal/UUID/datetime -> str; else passthrough."""
    if value is None:
        return None
    if hasattr(value, "value") and not isinstance(value, (str, int, bool)):
        return value.value
    if isinstance(value, (uuid.UUID, datetime.datetime)):
        return str(value)
    import decimal

    if isinstance(value, decimal.Decimal):
        return str(value)
    return value


def build_invoice_payload(
    invoice: Invoice, *, event_type: str, occurred_at: datetime.datetime
) -> dict[str, object]:
    """Whitelisted, PII-lean snapshot. No raw XML, no cessionario address/PEC;
    the receiver correlates on ``client_tag_id`` and pulls detail from the REST
    API under its own key if it needs more."""
    number = f"{invoice.series}-{invoice.number}" if invoice.number is not None else None
    return {
        "event": event_type,
        "occurred_at": occurred_at.astimezone(datetime.UTC).isoformat(),
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "invoice": {
            "id": str(invoice.id),
            "number": number,
            "series": invoice.series,
            "year": invoice.year,
            "document_type": _v(invoice.document_type),
            "state": _v(invoice.state),
            "sdi_status": _v(invoice.sdi_status),
            "identificativo_sdi": invoice.identificativo_sdi,
            "buyer_verdict": _v(invoice.buyer_verdict),
            "payment_status": _v(invoice.payment_status),
            "total": _v(invoice.total),
            "client_tag_id": _v(invoice.client_tag_id),
            "issuer_profile_id": str(invoice.issuer_profile_id),
        },
    }


# --- endpoint CRUD (owner-gated) --------------------------------------------


@dataclass(frozen=True)
class EndpointWithSecret:
    endpoint: WebhookEndpoint
    secret: str  # shown exactly once


def _normalize_event_types(event_types: Sequence[str] | None) -> list[str]:
    if not event_types:
        return []
    unknown = set(event_types) - VALID_EVENT_TYPES
    if unknown:
        raise UnprocessableError(
            MessageCode.WEBHOOK_EVENT_TYPE_INVALID, detail=", ".join(sorted(unknown))
        )
    return sorted(set(event_types))


async def create_endpoint(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    issuer_profile_id: uuid.UUID,
    name: str,
    url: str,
    event_types: Sequence[str] | None = None,
) -> EndpointWithSecret:
    """Owner-gated. Validates the URL (https + public unicast), Fernet-envelopes
    a fresh signing secret, returns the raw secret exactly once."""
    await require_role(session, org_id, actor_id, Role.owner)
    await assert_safe_destination(url)
    events = _normalize_event_types(event_types)
    secret = _generate_secret()
    row = WebhookEndpoint(
        org_id=org_id,
        issuer_profile_id=issuer_profile_id,
        created_by=actor_id,
        name=name,
        url=url,
        secret_ciphertext=encrypt_secret(secret),
        event_types=events,
        active=True,
    )
    session.add(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="webhook_endpoint",
        entity_id=row.id,
        action="create",
        diff={"url": url, "event_types": events},
    )
    return EndpointWithSecret(endpoint=row, secret=secret)


async def list_endpoints(
    session: AsyncSession, *, org_id: uuid.UUID, issuer_profile_id: uuid.UUID | None = None
) -> list[WebhookEndpoint]:
    stmt = select(WebhookEndpoint).where(WebhookEndpoint.org_id == org_id)
    if issuer_profile_id is not None:
        stmt = stmt.where(WebhookEndpoint.issuer_profile_id == issuer_profile_id)
    stmt = stmt.order_by(WebhookEndpoint.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def _get(
    session: AsyncSession, *, org_id: uuid.UUID, endpoint_id: uuid.UUID
) -> WebhookEndpoint:
    row = (
        await session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.id == endpoint_id, WebhookEndpoint.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.WEBHOOK_ENDPOINT_NOT_FOUND)
    return row


async def rotate_secret(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    endpoint_id: uuid.UUID,
    grace_seconds: int = 0,
) -> EndpointWithSecret:
    """Owner-gated. Mint a new signing secret; the previous one keeps verifying
    (dual-sign not needed -- the receiver accepts either) until
    ``previous_secret_expires_at``. A grace of 0 kills the old secret at once."""
    await require_role(session, org_id, actor_id, Role.owner)
    row = await _get(session, org_id=org_id, endpoint_id=endpoint_id)
    new_secret = _generate_secret()
    if grace_seconds > 0:
        row.previous_secret_ciphertext = row.secret_ciphertext
        row.previous_secret_expires_at = datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(seconds=grace_seconds)
    else:
        row.previous_secret_ciphertext = None
        row.previous_secret_expires_at = None
    row.secret_ciphertext = encrypt_secret(new_secret)
    row.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="webhook_endpoint",
        entity_id=row.id,
        action="rotate_secret",
    )
    return EndpointWithSecret(endpoint=row, secret=new_secret)


async def update_endpoint(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    endpoint_id: uuid.UUID,
    name: str | None = None,
    url: str | None = None,
    event_types: Sequence[str] | None = None,
    active: bool | None = None,
) -> WebhookEndpoint:
    await require_role(session, org_id, actor_id, Role.owner)
    row = await _get(session, org_id=org_id, endpoint_id=endpoint_id)
    if url is not None and url != row.url:
        await assert_safe_destination(url)
        row.url = url
    if name is not None:
        row.name = name
    if event_types is not None:
        row.event_types = _normalize_event_types(event_types)
    if active is not None:
        row.active = active
    row.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="webhook_endpoint",
        entity_id=row.id,
        action="update",
    )
    return row


async def revoke_endpoint(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, endpoint_id: uuid.UUID
) -> None:
    """Owner-gated. Deactivate + stamp ``revoked_at`` (idempotent). Pending
    deliveries for this endpoint are cancelled so a revoked endpoint stops
    receiving."""
    await require_role(session, org_id, actor_id, Role.owner)
    row = await _get(session, org_id=org_id, endpoint_id=endpoint_id)
    if row.revoked_at is not None:
        return
    row.active = False
    row.revoked_at = datetime.datetime.now(tz=datetime.UTC)
    row.version += 1
    await session.execute(
        text(
            "UPDATE webhook_deliveries SET status='dead', last_error='endpoint revoked' "
            "WHERE endpoint_id = :eid AND status IN ('pending','failed')"
        ),
        {"eid": str(endpoint_id)},
    )
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="webhook_endpoint",
        entity_id=row.id,
        action="revoke",
    )


async def purge_endpoint(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, endpoint_id: uuid.UUID
) -> None:
    """Owner-gated HARD delete of an ALREADY-revoked endpoint (mirrors the
    issuer-key purge). Fail-closed on an active endpoint. Deliveries cascade."""
    await require_role(session, org_id, actor_id, Role.owner)
    row = (
        await session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.id == endpoint_id, WebhookEndpoint.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return
    if row.revoked_at is None:
        raise ConflictError(MessageCode.WEBHOOK_ENDPOINT_NOT_REVOKED)
    await session.delete(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="webhook_endpoint",
        entity_id=endpoint_id,
        action="purge",
    )


async def list_deliveries(
    session: AsyncSession, *, org_id: uuid.UUID, endpoint_id: uuid.UUID, limit: int = 50
) -> list[WebhookDelivery]:
    rows = (
        (
            await session.execute(
                select(WebhookDelivery)
                .where(WebhookDelivery.org_id == org_id, WebhookDelivery.endpoint_id == endpoint_id)
                .order_by(WebhookDelivery.created_at.desc())
                .limit(max(1, min(limit, 200)))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# --- emit (transactional outbox, fiscal-safe) -------------------------------


async def enqueue_invoice_event(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    invoice: Invoice,
    event_type: str,
    dedupe_key: str,
    occurred_at: datetime.datetime,
) -> int:
    """Fan an invoice event out to every active, subscribed endpoint by
    INSERTing frozen ``webhook_deliveries`` rows in the caller's transaction.

    Wrapped in a SAVEPOINT and swallows ALL errors: a webhook fault must NEVER
    abort the fiscal transmit/ingest write it rides on. Returns the number of
    delivery rows enqueued (0 when disabled, no endpoints, or on any error).
    """
    if not get_settings().webhooks_enabled:
        return 0
    try:
        async with session.begin_nested():
            endpoints = (
                (
                    await session.execute(
                        select(WebhookEndpoint).where(
                            WebhookEndpoint.org_id == org_id,
                            WebhookEndpoint.issuer_profile_id == invoice.issuer_profile_id,
                            WebhookEndpoint.active.is_(True),
                            WebhookEndpoint.revoked_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not endpoints:
                return 0
            payload = build_invoice_payload(invoice, event_type=event_type, occurred_at=occurred_at)
            payload_json = json.dumps(payload)
            max_attempts = get_settings().webhook_max_attempts
            count = 0
            for ep in endpoints:
                if ep.event_types and event_type not in ep.event_types:
                    continue
                res = await session.execute(
                    text(
                        "INSERT INTO webhook_deliveries "
                        "(id, org_id, endpoint_id, issuer_profile_id, event_type, invoice_id, "
                        " payload_snapshot, payload_schema_version, dedupe_key, status, "
                        " attempt_count, max_attempts, next_attempt_at) "
                        "VALUES (gen_random_uuid(), :org, :eid, :iss, :evt, :inv, "
                        " CAST(:payload AS jsonb), :schema, :dedupe, 'pending', 0, :maxa, now()) "
                        "ON CONFLICT (endpoint_id, dedupe_key) DO NOTHING"
                    ),
                    {
                        "org": str(org_id),
                        "eid": str(ep.id),
                        "iss": str(invoice.issuer_profile_id),
                        "evt": event_type,
                        "inv": str(invoice.id),
                        "payload": payload_json,
                        "schema": PAYLOAD_SCHEMA_VERSION,
                        "dedupe": dedupe_key,
                        "maxa": max_attempts,
                    },
                )
                count += int(res.rowcount or 0)  # type: ignore[attr-defined]
            return count
    except Exception:
        _log.exception(
            "webhook enqueue failed for invoice=%s event=%s (fiscal tx preserved)",
            invoice.id,
            event_type,
        )
        return 0


# --- delivery worker --------------------------------------------------------


def _backoff_seconds(attempt: int) -> int:
    s = get_settings()
    delay = s.webhook_backoff_base_seconds * int(2 ** max(0, attempt - 1))
    return min(delay, s.webhook_backoff_cap_seconds)


async def deliver_due(
    session: AsyncSession, *, org_id: uuid.UUID, batch: int = 20
) -> tuple[int, int]:
    """One drain pass for a workspace. Reclaims expired leases, then claims a
    bounded batch of due rows (``FOR UPDATE SKIP LOCKED``), sends each, and
    records the outcome with exponential backoff. Returns ``(delivered,
    failed)``. Never raises: a per-row error becomes a failed attempt."""
    s = get_settings()
    now = datetime.datetime.now(tz=datetime.UTC)
    lease_cutoff = now - datetime.timedelta(seconds=s.webhook_delivery_lease_seconds)
    # 1) Reclaim crashed in-flight leases back to retryable.
    await session.execute(
        text(
            "UPDATE webhook_deliveries SET status='failed', "
            "last_error='lease expired; reclaimed' "
            "WHERE org_id = :org AND status='delivering' AND last_attempt_at < :cut"
        ),
        {"org": str(org_id), "cut": lease_cutoff},
    )
    # 2) Claim a batch of due rows.
    claimed = (
        (
            await session.execute(
                select(WebhookDelivery)
                .where(
                    WebhookDelivery.org_id == org_id,
                    WebhookDelivery.status.in_(("pending", "failed")),
                    WebhookDelivery.next_attempt_at <= now,
                )
                .order_by(WebhookDelivery.next_attempt_at)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not claimed:
        return (0, 0)
    # Load the endpoints once (secret + url).
    ep_ids = {d.endpoint_id for d in claimed}
    endpoints = {
        e.id: e
        for e in (
            await session.execute(select(WebhookEndpoint).where(WebhookEndpoint.id.in_(ep_ids)))
        )
        .scalars()
        .all()
    }
    sender = get_sender()
    delivered = 0
    failed = 0
    for d in claimed:
        ep = endpoints.get(d.endpoint_id)
        d.attempt_count += 1
        d.last_attempt_at = now
        if ep is None or ep.revoked_at is not None or not ep.active:
            d.status = "dead"
            d.last_error = "endpoint gone"
            failed += 1
            continue
        d.status = "delivering"
        result = await _attempt(sender, ep, d, timeout_s=float(s.webhook_delivery_timeout_seconds))
        if result.ok:
            d.status = "delivered"
            d.delivered_at = datetime.datetime.now(tz=datetime.UTC)
            d.response_code = result.status_code
            d.response_excerpt = result.excerpt
            d.last_error = None
            delivered += 1
        else:
            d.response_code = result.status_code
            d.response_excerpt = result.excerpt
            d.last_error = result.error or (f"HTTP {result.status_code}")
            if d.attempt_count >= d.max_attempts:
                d.status = "dead"
            else:
                d.status = "failed"
                d.next_attempt_at = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(
                    seconds=_backoff_seconds(d.attempt_count)
                )
            failed += 1
    await session.flush()
    return (delivered, failed)


async def _attempt(
    sender: WebhookSender, ep: WebhookEndpoint, d: WebhookDelivery, *, timeout_s: float
) -> SendResult:
    # Re-classify at send time (DNS-rebinding guard).
    try:
        await assert_safe_destination(ep.url)
    except Exception as exc:
        return SendResult(ok=False, error=f"unsafe destination: {exc}"[:512])
    body = json.dumps(d.payload_snapshot, separators=(",", ":")).encode()
    ts = int(datetime.datetime.now(tz=datetime.UTC).timestamp())
    secret = decrypt_secret(ep.secret_ciphertext)
    headers = {
        "content-type": "application/json",
        "user-agent": "mycelium-webhooks/1",
        "x-webhook-id": str(d.id),
        "x-webhook-event": d.event_type,
        "x-webhook-timestamp": str(ts),
        "x-webhook-signature": f"v1={sign(secret, ts, body)}",
    }
    try:
        result = await asyncio.wait_for(
            sender.send(url=ep.url, headers=headers, body=body, timeout_s=timeout_s),
            timeout=timeout_s + 2,
        )
    except TimeoutError:
        return SendResult(ok=False, error="delivery timed out")
    except Exception as exc:
        return SendResult(ok=False, error=str(exc)[:512])
    return result


async def purge_expired_deliveries(session: AsyncSession, *, org_id: uuid.UUID) -> int:
    """Retention: drop terminal (delivered/dead) rows past the window so the
    payload snapshots do not linger. Returns rows deleted."""
    cutoff = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(
        days=get_settings().webhook_delivery_retention_days
    )
    res = await session.execute(
        text(
            "DELETE FROM webhook_deliveries WHERE org_id = :org "
            "AND status IN ('delivered','dead') AND created_at < :cut"
        ),
        {"org": str(org_id), "cut": cutoff},
    )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


__all__ = [
    "EVENT_ACCEPTED",
    "EVENT_DEEMED_ACCEPTED",
    "EVENT_DELIVERED",
    "EVENT_PAYMENT_RECORDED",
    "EVENT_REJECTED",
    "EVENT_TRANSMITTED",
    "VALID_EVENT_TYPES",
    "EndpointWithSecret",
    "SendResult",
    "WebhookSender",
    "assert_safe_destination",
    "build_invoice_payload",
    "create_endpoint",
    "deliver_due",
    "enqueue_invoice_event",
    "get_sender",
    "list_deliveries",
    "list_endpoints",
    "purge_endpoint",
    "purge_expired_deliveries",
    "revoke_endpoint",
    "rotate_secret",
    "set_webhook_sender_override",
    "sign",
    "update_endpoint",
]
