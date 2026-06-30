"""Agent tokens router (v1.1).

Long-lived bearer credentials for MCP / external automation. Thin
adapter over ``mycelium_core.services.agent_tokens`` (docs/adr/0001). All
endpoints are RLS-scoped to the caller's workspace; mint and revoke
are owner-gated *inside* the service (the RBAC choke point + effective-
role sudo), mirroring the executor / billing-grant precedent for
sensitive workspace config.

Response contract -- stable for the SPA / external clients:

- ``GET /agent-tokens`` -> ``[AgentTokenOut]`` (raw absent always)
- ``POST /agent-tokens`` -> ``AgentTokenCreateOut`` with ``raw``
  (the ONLY surface where the plaintext token ever appears)
- ``DELETE /agent-tokens/{id}`` -> 204, idempotent
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_api.schemas import (
    AgentTokenCreateIn,
    AgentTokenCreateOut,
    AgentTokenOut,
)
from mycelium_core.models.agent_token import AgentToken
from mycelium_core.services import agent_tokens as svc

router = APIRouter(prefix="/agent-tokens", tags=["agent-tokens"])


def _out(t: AgentToken) -> AgentTokenOut:
    return AgentTokenOut(
        id=t.id,
        name=t.name,
        scope=t.scope,
        prefix=t.prefix,
        expires_at=t.expires_at,
        last_used_at=t.last_used_at,
        revoked_at=t.revoked_at,
        created_at=t.created_at,
    )


@router.get("", response_model=list[AgentTokenOut])
async def list_tokens(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[AgentTokenOut]:
    """List the workspace's agent tokens, newest first. Includes
    revoked rows (the UI distinguishes via ``revoked_at``) so the audit
    trail stays visible."""
    rows = await svc.list_tokens(ctx.session, org_id=ctx.org_id)
    return [_out(t) for t in rows]


@router.post("", response_model=AgentTokenCreateOut)
async def create_token(
    body: AgentTokenCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AgentTokenCreateOut:
    """Mint a fresh long-lived bearer token (owner-gated in the
    service). The ``raw`` value in the response is the only time the
    plaintext credential is sent; persist it on the client side now."""
    result = await svc.mint(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        name=body.name,
        scope=body.scope,
        # 0 in the API contract = "never expires"; the service maps
        # both 0 and ``None`` to a non-expiring row.
        ttl_days=body.ttl_days if (body.ttl_days is None or body.ttl_days > 0) else None,
    )
    return AgentTokenCreateOut(
        id=result.token.id,
        name=result.token.name,
        scope=result.token.scope,
        prefix=result.token.prefix,
        expires_at=result.token.expires_at,
        created_at=result.token.created_at,
        raw=result.raw,
    )


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    """Revoke a token (owner-gated in the service). Idempotent: a
    second revoke on a previously revoked token is a 204 no-op that
    preserves the original ``revoked_at`` timestamp."""
    await svc.revoke(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        token_id=token_id,
    )


__all__ = ["router"]
