"""Capability-token mint for text blocks.

The attachment mints live on the attachments router
(``POST /attachments/capability`` read, ``/attachments/capability/write``).
This router mints the three TEXT block grants (note part body, task
description, comment body) through one generic endpoint, so the SPA / CLI
have an HTTP path symmetric to the MCP ``*_text_block_capability`` tools
(which mint the same grants directly through the service).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_api.schemas import (
    CapabilityRevokeOut,
    TextBlockCapabilityIn,
    TextBlockCapabilityOut,
)
from mycelium_core.services import capability_tokens

router = APIRouter(prefix="/capability", tags=["capabilities"])


@router.post("/text-block", status_code=status.HTTP_201_CREATED)
async def mint_text_block_capability(
    payload: TextBlockCapabilityIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TextBlockCapabilityOut:
    """Mint a mycelium_cap_ token for a text block. ``(kind, verb)`` maps to the
    capability action via :func:`capability_tokens.text_block_action`; the
    resource_kind follows ``kind`` (note_part -> note_part id;
    task_description -> task id; annotation -> annotation id). read is
    multi-use within the TTL; write / patch are single-use (consumed on the
    first successful write). Member-gated, so the token grants nothing the
    caller did not already hold. The raw token is returned exactly once; the
    caller hits the matching raw / stream / patch route with
    ``Authorization: Bearer <token>`` (no PAT, no X-Workspace-Id)."""
    action = capability_tokens.text_block_action(payload.kind, payload.verb)
    resource_kind = capability_tokens.text_block_resource_kind(payload.kind)
    grant = await capability_tokens.mint(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        action=action,
        resource_kind=resource_kind,
        resource_id=payload.resource_id,
        ttl_seconds=payload.ttl_seconds,
    )
    return TextBlockCapabilityOut(
        token=grant.raw,
        expires_at=grant.expires_at,
        kind=payload.kind,
        resource_id=payload.resource_id,
        verb=payload.verb,
    )


@router.post("/{token_id}/revoke", status_code=status.HTTP_200_OK)
async def revoke_capability(
    token_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> CapabilityRevokeOut:
    """Withdraw a capability before it expires (task 1428a184).

    Until this existed a minted grant could only be waited out, which is
    tolerable at the five-minute default and no answer at all for one
    that leaked: the raw value is handed back exactly once, so whoever
    minted it could not take it away again.

    ``revoked`` is "did THIS call do it": false means the grant was
    already revoked, already consumed, expired, or belongs to another
    workspace. Absent rather than forbidden on that last one -- a caller
    holding an id it should not know learns nothing about whether the id
    exists. Member-gated, the same floor as the mint: the right to take a
    grant away is the right to make one."""
    done = await capability_tokens.revoke(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, token_id=token_id
    )
    return CapabilityRevokeOut(revoked=done)
