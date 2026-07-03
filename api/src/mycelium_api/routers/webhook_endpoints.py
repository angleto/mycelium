"""Signed-webhook endpoint management router (task 2c23e955, ADR-0047).

REST + GUI only (never MCP -- configuring a delivery transport is the same
carve-out as issuer keys / agent tokens). Thin adapter over
``mycelium_core.services.webhooks``; create / rotate / update / revoke / purge
are owner-gated INSIDE the service. Nested under the issuer profile the
endpoint binds to.

- GET    .../webhook-endpoints                 -> [WebhookEndpointOut]
- POST   .../webhook-endpoints                 -> WebhookEndpointCreateOut (secret once)
- PATCH  .../webhook-endpoints/{eid}           -> WebhookEndpointOut
- POST   .../webhook-endpoints/{eid}/rotate-secret -> WebhookEndpointCreateOut (secret once)
- DELETE .../webhook-endpoints/{eid}           -> 204 (revoke; ?hard=true purge)
- GET    .../webhook-endpoints/{eid}/deliveries -> [WebhookDeliveryOut]
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_api.schemas import (
    WebhookDeliveryOut,
    WebhookEndpointCreateOut,
    WebhookEndpointIn,
    WebhookEndpointOut,
    WebhookEndpointUpdateIn,
)
from mycelium_core.errors import NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.webhook import WebhookEndpoint
from mycelium_core.services import webhooks as svc

router = APIRouter(tags=["webhook-endpoints"])


def _out(e: WebhookEndpoint) -> WebhookEndpointOut:
    return WebhookEndpointOut(
        id=e.id,
        issuer_profile_id=e.issuer_profile_id,
        name=e.name,
        url=e.url,
        event_types=list(e.event_types),
        active=e.active,
        created_at=e.created_at,
        revoked_at=e.revoked_at,
    )


def _create_out(e: WebhookEndpoint, secret: str) -> WebhookEndpointCreateOut:
    return WebhookEndpointCreateOut(**_out(e).model_dump(), secret=secret)


async def _assert_in_issuer(
    ctx: TenantCtx, issuer_profile_id: uuid.UUID, endpoint_id: uuid.UUID
) -> None:
    """The nested endpoint_id must belong to the path's issuer profile (RLS
    already confines to the org). 404 -- never confirm an endpoint elsewhere."""
    found = (
        await ctx.session.execute(
            select(WebhookEndpoint.id).where(
                WebhookEndpoint.id == endpoint_id,
                WebhookEndpoint.issuer_profile_id == issuer_profile_id,
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotFoundError(MessageCode.WEBHOOK_ENDPOINT_NOT_FOUND)


@router.get(
    "/issuer-profiles/{issuer_profile_id}/webhook-endpoints",
    response_model=list[WebhookEndpointOut],
)
async def list_endpoints(
    issuer_profile_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[WebhookEndpointOut]:
    """List the issuer's endpoints, newest first (active + revoked; the UI
    distinguishes via ``revoked_at``). No secret is shown."""
    rows = await svc.list_endpoints(
        ctx.session, org_id=ctx.org_id, issuer_profile_id=issuer_profile_id
    )
    return [_out(e) for e in rows]


@router.post(
    "/issuer-profiles/{issuer_profile_id}/webhook-endpoints",
    response_model=WebhookEndpointCreateOut,
)
async def create_endpoint(
    issuer_profile_id: uuid.UUID,
    body: WebhookEndpointIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> WebhookEndpointCreateOut:
    """Create an endpoint (owner-gated). ``secret`` is the only time the signing
    secret is sent -- store it now."""
    res = await svc.create_endpoint(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        issuer_profile_id=issuer_profile_id,
        name=body.name,
        url=body.url,
        event_types=body.event_types,
    )
    return _create_out(res.endpoint, res.secret)


@router.patch(
    "/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}",
    response_model=WebhookEndpointOut,
)
async def update_endpoint(
    issuer_profile_id: uuid.UUID,
    endpoint_id: uuid.UUID,
    body: WebhookEndpointUpdateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> WebhookEndpointOut:
    """Edit name / url / subscribed events / active (owner-gated). A changed
    URL is re-validated (https + public unicast)."""
    await _assert_in_issuer(ctx, issuer_profile_id, endpoint_id)
    row = await svc.update_endpoint(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        endpoint_id=endpoint_id,
        name=body.name,
        url=body.url,
        event_types=body.event_types,
        active=body.active,
    )
    return _out(row)


@router.post(
    "/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}/rotate-secret",
    response_model=WebhookEndpointCreateOut,
)
async def rotate_secret(
    issuer_profile_id: uuid.UUID,
    endpoint_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    grace_seconds: Annotated[int, Query(ge=0)] = 0,
) -> WebhookEndpointCreateOut:
    """Issue a new signing secret (owner-gated). ``grace_seconds`` keeps the
    previous secret verifying so a receiver can roll over without missed events."""
    await _assert_in_issuer(ctx, issuer_profile_id, endpoint_id)
    res = await svc.rotate_secret(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        endpoint_id=endpoint_id,
        grace_seconds=grace_seconds,
    )
    return _create_out(res.endpoint, res.secret)


@router.delete(
    "/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_endpoint(
    issuer_profile_id: uuid.UUID,
    endpoint_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    hard: Annotated[bool, Query()] = False,
) -> None:
    """Owner-gated. Default: REVOKE (deactivate + cancel pending deliveries;
    idempotent). ``hard=true``: PURGE an already-revoked endpoint (409
    webhook_endpoint.not_revoked on an active one -- revoke first)."""
    await _assert_in_issuer(ctx, issuer_profile_id, endpoint_id)
    if hard:
        await svc.purge_endpoint(
            ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, endpoint_id=endpoint_id
        )
    else:
        await svc.revoke_endpoint(
            ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, endpoint_id=endpoint_id
        )


@router.get(
    "/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}/deliveries",
    response_model=list[WebhookDeliveryOut],
)
async def list_deliveries(
    issuer_profile_id: uuid.UUID,
    endpoint_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[WebhookDeliveryOut]:
    """Recent delivery attempts for the endpoint (newest first) -- the SPA's
    activity view for debugging a receiver."""
    await _assert_in_issuer(ctx, issuer_profile_id, endpoint_id)
    rows = await svc.list_deliveries(
        ctx.session, org_id=ctx.org_id, endpoint_id=endpoint_id, limit=limit
    )
    return [
        WebhookDeliveryOut(
            id=d.id,
            event_type=d.event_type,
            invoice_id=d.invoice_id,
            status=d.status,
            attempt_count=d.attempt_count,
            max_attempts=d.max_attempts,
            next_attempt_at=d.next_attempt_at,
            last_attempt_at=d.last_attempt_at,
            delivered_at=d.delivered_at,
            response_code=d.response_code,
            last_error=d.last_error,
            created_at=d.created_at,
        )
        for d in rows
    ]


__all__ = ["router"]
