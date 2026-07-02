"""Issuer-API-key management router (task 19b7e874, phase 4).

REST + GUI only (never MCP -- minting a credential configures a transport, the
same chicken-and-egg carve-out as agent tokens). A thin adapter over
``mycelium_core.services.issuer_api_keys``; mint / rotate / revoke are
owner-gated INSIDE the service. Nested under the issuer profile the key binds to.

- ``GET    /issuer-profiles/{id}/api-keys``            -> [IssuerApiKeyOut] (no secret)
- ``POST   /issuer-profiles/{id}/api-keys``            -> IssuerApiKeyCreateOut (raw once)
- ``POST   /issuer-profiles/{id}/api-keys/{kid}/rotate`` -> IssuerApiKeyCreateOut (new raw once)
- ``DELETE /issuer-profiles/{id}/api-keys/{kid}``      -> 204, idempotent
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_api.schemas import (
    IssuerApiKeyCreateIn,
    IssuerApiKeyCreateOut,
    IssuerApiKeyOut,
)
from mycelium_core.errors import NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.issuer_api_key import IssuerApiKey
from mycelium_core.services import issuer_api_keys as svc

router = APIRouter(tags=["issuer-api-keys"])


def _out(k: IssuerApiKey) -> IssuerApiKeyOut:
    now = datetime.datetime.now(tz=datetime.UTC)
    return IssuerApiKeyOut(
        id=k.id,
        issuer_profile_id=k.issuer_profile_id,
        name=k.name,
        prefix=f"{svc.RAW_PREFIX}{k.key_public_id}",
        permissions=list(k.permissions),
        created_at=k.created_at,
        expires_at=k.expires_at,
        last_used_at=k.last_used_at,
        previous_secret_last_used_at=k.previous_secret_last_used_at,
        rotated_at=k.rotated_at,
        revoked_at=k.revoked_at,
        days_to_expiry=(k.expires_at - now).days,
    )


def _create_out(k: IssuerApiKey, raw: str) -> IssuerApiKeyCreateOut:
    return IssuerApiKeyCreateOut(**_out(k).model_dump(), raw=raw)


async def _assert_key_in_issuer(
    ctx: TenantCtx, issuer_profile_id: uuid.UUID, key_id: uuid.UUID
) -> None:
    """The nested key_id must belong to the path's issuer profile (RLS already
    confines to the org). 404 -- never confirm a key under a different issuer."""
    found = (
        await ctx.session.execute(
            select(IssuerApiKey.id).where(
                IssuerApiKey.id == key_id,
                IssuerApiKey.issuer_profile_id == issuer_profile_id,
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotFoundError(MessageCode.ISSUER_API_KEY_NOT_FOUND)


@router.get("/issuer-profiles/{issuer_profile_id}/api-keys", response_model=list[IssuerApiKeyOut])
async def list_keys(
    issuer_profile_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[IssuerApiKeyOut]:
    """List the issuer's keys, newest first (active + revoked; the UI
    distinguishes via ``revoked_at``). Member-level read -- no secret is shown."""
    rows = await svc.list_keys(ctx.session, org_id=ctx.org_id, issuer_profile_id=issuer_profile_id)
    return [_out(k) for k in rows]


@router.post("/issuer-profiles/{issuer_profile_id}/api-keys", response_model=IssuerApiKeyCreateOut)
async def mint_key(
    issuer_profile_id: uuid.UUID,
    body: IssuerApiKeyCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> IssuerApiKeyCreateOut:
    """Mint a key (owner-gated in the service). ``raw`` is the only time the
    plaintext secret is sent -- copy it now."""
    res = await svc.mint(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        issuer_profile_id=issuer_profile_id,
        name=body.name,
        permissions=body.permissions,
        ttl_days=body.ttl_days,
    )
    return _create_out(res.key, res.raw)


@router.post(
    "/issuer-profiles/{issuer_profile_id}/api-keys/{key_id}/rotate",
    response_model=IssuerApiKeyCreateOut,
)
async def rotate_key(
    issuer_profile_id: uuid.UUID,
    key_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    grace_seconds: Annotated[int | None, Query(ge=0)] = None,
) -> IssuerApiKeyCreateOut:
    """Issue a new secret for the key (owner-gated). ``grace_seconds`` (default =
    the configured value, 0 = hard rotation) keeps the previous secret valid for a
    bounded window; a new ``raw`` is returned once."""
    await _assert_key_in_issuer(ctx, issuer_profile_id, key_id)
    res = await svc.rotate(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        key_id=key_id,
        grace_seconds=grace_seconds,
    )
    return _create_out(res.key, res.raw)


@router.delete(
    "/issuer-profiles/{issuer_profile_id}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_key(
    issuer_profile_id: uuid.UUID,
    key_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    """Revoke a key (owner-gated; kills both the current and any grace secret).
    Idempotent."""
    await _assert_key_in_issuer(ctx, issuer_profile_id, key_id)
    await svc.revoke(ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, key_id=key_id)


__all__ = ["router"]
