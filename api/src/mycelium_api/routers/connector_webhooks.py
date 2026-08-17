"""The public inbound webhook for payment connectors (ADR-0051).

One unauthenticated route. Everything about it is shaped by two facts: the
caller is a payment provider whose retry logic reads our status code, and the
connector id in the path is a ROUTING SELECTOR, not a credential -- authority
comes from a MAC over the raw request body under a per-connector secret.

Order of operations is load-bearing:

1. resolve the tenant with the SECURITY DEFINER function (no session, no
   bearer, so no ``app.current_org`` exists yet);
2. verify the signature over the RAW BYTES, before any parsing -- a body
   normalised by ``json.loads`` no longer hashes to what the sender signed, and
   parsing before authenticating hands an unauthenticated caller our parser;
3. verify the optional ingress key;
4. only then parse, identify and persist.

EVERY outcome against a resolved connector is appended to
``payment_webhook_deliveries``, refusals included. A fiscal integration has to
be able to answer "the provider says it delivered this and there is no invoice"
from the database; a rejected signature that left nothing but a log line on one
pod cannot answer it. The recording is best-effort in one direction only: it
never breaks the ingress, but for an ACCEPTED event it shares the event's
transaction, so an event can never exist without its delivery row.

The handler does no fiscal work whatsoever. An SdI dispatch is allowed 120
seconds and every provider's webhook timeout is far shorter, so composing an
invoice inline would be reported as a failure and redelivered while the first
attempt was still filing. Persist, answer 2xx, let the worker do the rest.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import AuthError, DomainError, ForbiddenError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.payment_connector import PaymentConnectorEvent
from mycelium_core.security_events import emit as emit_security_event
from mycelium_core.services import payment_connectors as svc
from mycelium_core.services.payment_events import PayloadError, get_mapper

_log = logging.getLogger("mycelium.api.connector_webhooks")

router = APIRouter(prefix="/api/v1/connectors", tags=["payment-connectors"])

API_KEY_HEADER = "X-Connector-Api-Key"


class ConnectorReceiptOut(BaseModel):
    """Deliberately opaque. A provider needs to know we took custody, nothing
    else; echoing what we decided would leak tenant state to an endpoint whose
    only credential is a shared secret."""

    received: bool = True
    duplicate: bool = False


async def _record_refusal(
    resolved: svc.ResolvedConnector,
    *,
    outcome: str,
    http_status: int,
    raw: bytes,
    signature_present: bool,
    api_key_present: bool,
    provider_event_id: str | None = None,
) -> None:
    """Append a refused delivery, then let the caller raise.

    Runs in its own committed session BEFORE the exception propagates, because
    raising inside the session would roll the evidence back with the request.
    Swallows its own errors: an audit write must never turn a clean 401 into a
    500, which would make the provider retry a request we correctly refused.
    """
    try:
        async with tenant_session(
            str(resolved.org_id),
            str(resolved.connector_id),
            actor_kind="payment_connector",
            actor_subject_id=str(resolved.connector_id),
        ) as session:
            # The ledger is the cost an unauthenticated caller can impose: one
            # appended row per refused request, for anyone who learned the URL.
            # Past the window's budget the refusal still happens -- the caller is
            # turned away exactly as before -- but it stops being written down.
            # The first refusals of the window are what an operator would read
            # anyway; the thousandth adds nothing but storage.
            if not await svc.note_refusal(
                session, org_id=resolved.org_id, connector_id=resolved.connector_id
            ):
                return
            await svc.record_delivery(
                session,
                org_id=resolved.org_id,
                connector_id=resolved.connector_id,
                provider=resolved.provider,
                outcome=outcome,
                http_status=http_status,
                raw_body=raw,
                signature_present=signature_present,
                api_key_present=api_key_present,
                provider_event_id=provider_event_id,
            )
    except Exception:
        _log.exception(
            "failed to record refused delivery connector=%s outcome=%s",
            resolved.connector_id,
            outcome,
        )


@router.post("/{provider}/{connector_id}", response_model=ConnectorReceiptOut)
async def receive(
    provider: str,
    connector_id: uuid.UUID,
    request: Request,
    response: Response,
    x_connector_api_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
) -> ConnectorReceiptOut:
    settings = get_settings()
    if not settings.payment_connectors_enabled:
        # Fail closed: an unconfigured deploy is indistinguishable from one that
        # never had this connector.
        raise NotFoundError(MessageCode.PAYMENT_CONNECTOR_NOT_FOUND)

    raw = await request.body()
    async with admin_session() as probe:
        resolved = await svc.resolve_for_ingress(probe, connector_id=connector_id)
    if resolved is None or resolved.provider != provider:
        # Unknown, revoked, or addressed under the wrong provider path: one
        # answer for all three, so the surface is not a connector-existence
        # oracle for anyone who can guess a uuid. Nothing is recorded -- there
        # is no tenant to attribute an unattributable request to.
        emit_security_event(
            "payment_connector.unresolved",
            connector_id=str(connector_id),
            provider=provider,
        )
        raise NotFoundError(MessageCode.PAYMENT_CONNECTOR_NOT_FOUND)

    mapper = get_mapper(resolved.provider)
    headers = {k.lower(): v for k, v in request.headers.items()}
    signature_present = any(k.endswith("-signature") for k in headers)
    api_key_present = x_connector_api_key is not None

    if len(raw) > settings.payment_connector_max_body_bytes:
        # Bound the MAC work an unauthenticated caller can force. Checked on the
        # real length, not on Content-Length, which the caller controls.
        await _record_refusal(
            resolved,
            outcome="too_large",
            http_status=400,
            raw=raw,
            signature_present=signature_present,
            api_key_present=api_key_present,
        )
        raise DomainError(MessageCode.PAYMENT_CONNECTOR_PAYLOAD_INVALID, detail="body too large")

    # No secret installed yet (a vendor connector created before its endpoint
    # was registered) means we cannot verify, which is a refusal. Collapsed into
    # the same branch as a bad signature on purpose: an unauthenticated caller
    # must not be able to tell a misconfigured connector from a wrong key.
    secrets = resolved.secrets()
    verified = secrets is not None and mapper.verify(
        headers=headers,
        raw_body=raw,
        secrets=secrets,
        tolerance_seconds=settings.payment_connector_tolerance_seconds,
        now=datetime.datetime.now(tz=datetime.UTC),
    )
    if not verified or not resolved.api_key_matches(x_connector_api_key):
        # Collapsed on purpose: a caller must not learn WHICH factor failed, nor
        # whether the connector requires a key at all.
        emit_security_event(
            "payment_connector.signature_rejected",
            connector_id=str(connector_id),
            provider=provider,
        )
        await _record_refusal(
            resolved,
            outcome="signature_invalid",
            http_status=401,
            raw=raw,
            signature_present=signature_present,
            api_key_present=api_key_present,
        )
        raise AuthError(MessageCode.PAYMENT_CONNECTOR_SIGNATURE_INVALID)

    if not resolved.enabled:
        # Past the signature, so the caller is authentic and a precise answer
        # costs nothing: it tells the operator's dashboard exactly why their
        # events are bouncing instead of showing an opaque 404.
        await _record_refusal(
            resolved,
            outcome="disabled",
            http_status=403,
            raw=raw,
            signature_present=signature_present,
            api_key_present=api_key_present,
        )
        raise ForbiddenError(MessageCode.PAYMENT_CONNECTOR_DISABLED)

    identity = None
    detail: str | None = None
    try:
        payload: Any = json.loads(raw)
        if not isinstance(payload, dict):
            raise PayloadError("not a JSON object")
        identity = mapper.identify(payload)
    except ValueError as exc:  # PayloadError is a ValueError subclass
        detail = "not JSON" if not isinstance(exc, PayloadError) else str(exc)
    if identity is None:
        await _record_refusal(
            resolved,
            outcome="payload_invalid",
            http_status=400,
            raw=raw,
            signature_present=signature_present,
            api_key_present=api_key_present,
        )
        raise DomainError(MessageCode.PAYMENT_CONNECTOR_PAYLOAD_INVALID, detail=detail or "invalid")

    async with tenant_session(
        str(resolved.org_id),
        str(resolved.connector_id),
        actor_kind="payment_connector",
        actor_subject_id=str(resolved.connector_id),
    ) as session:
        created = await svc.ingest(
            session,
            org_id=resolved.org_id,
            connector_id=resolved.connector_id,
            provider_event_id=identity.event_id,
            event_type=identity.event_type,
            payload=payload,
            occurred_at=identity.occurred_at,
        )
        event_id = (
            await session.execute(
                select(PaymentConnectorEvent.id).where(
                    PaymentConnectorEvent.connector_id == resolved.connector_id,
                    PaymentConnectorEvent.provider_event_id == identity.event_id,
                )
            )
        ).scalar_one_or_none()
        # Same transaction as the ingest: an event must never exist without the
        # delivery row that explains where it came from.
        await svc.record_delivery(
            session,
            org_id=resolved.org_id,
            connector_id=resolved.connector_id,
            provider=resolved.provider,
            outcome="accepted" if created else "duplicate",
            http_status=200,
            raw_body=raw,
            signature_present=signature_present,
            api_key_present=api_key_present,
            provider_event_id=identity.event_id,
            event_id=event_id,
        )

    # A redelivery is a success from the sender's point of view: answering
    # anything but 2xx would make the provider retry a duplicate forever.
    response.headers["Cache-Control"] = "no-store"
    return ConnectorReceiptOut(received=True, duplicate=not created)
