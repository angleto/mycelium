"""Capability-token mint for text blocks.

The attachment mints live on the attachments router
(``POST /attachments/capability`` read, ``/attachments/capability/write``).
This router mints the three TEXT block grants (note part body, task
description, comment body) through one generic endpoint, so the SPA / CLI
have an HTTP path symmetric to the MCP ``*_text_block_capability`` tools
(which mint the same grants directly through the service).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_api.schemas import TextBlockCapabilityIn, TextBlockCapabilityOut
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
