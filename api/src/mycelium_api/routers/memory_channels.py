"""Memory channels router: the controlled, seeded channel vocabulary
(docs/adr/0005, FR-8). Thin adapter over taxonomy (docs/adr/0001).

A memory channel is a ``memory_channel`` tag with a stable
``system_key``. Integrations (email ingest, Telegram) resolve their
target by that key, so the vocabulary must be deterministic, not
arbitrary user-named tags. Listing is open to any authenticated member
(the memory UI needs it to pick a channel); creating/renaming/
enabling/deleting is reserved to the PLATFORM ADMIN (global ``is_admin``
+ active ``X-Admin-Mode``), gated by ``tenant_admin_ctx`` -- the same
sudo rule as the global admin surface, just on an RLS-scoped tenant
session so the channel still lands in the caller's workspace.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from mycelium_api.deps import TenantCtx, tenant_admin_ctx, tenant_ctx
from mycelium_api.schemas import (
    MemoryChannelCreateIn,
    MemoryChannelOut,
    MemoryChannelPatchIn,
)
from mycelium_core.services import taxonomy
from mycelium_core.services.taxonomy import CANONICAL_MEMORY_CHANNELS, channel_description

router = APIRouter(prefix="/memory/channels", tags=["memory"])

_CANONICAL_KEYS = frozenset(k for k, _ in CANONICAL_MEMORY_CHANNELS)


def _out(tag: object) -> MemoryChannelOut:
    return MemoryChannelOut(
        id=tag.id,  # type: ignore[attr-defined]
        name=tag.name,  # type: ignore[attr-defined]
        system_key=tag.system_key,  # type: ignore[attr-defined]
        enabled=tag.status == "active",  # type: ignore[attr-defined]
        seeded=tag.system_key in _CANONICAL_KEYS,  # type: ignore[attr-defined]
        description=channel_description(tag.system_key),  # type: ignore[attr-defined]
        version=tag.version,  # type: ignore[attr-defined]
    )


@router.get("", response_model=list[MemoryChannelOut])
async def list_channels(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[MemoryChannelOut]:
    """List the tenant's configured channels (seeds the canonical four
    on first call). Any authenticated member may list -- the memory UI
    needs it to select a channel. RLS-scoped."""
    channels = await taxonomy.list_memory_channels(ctx.session, org_id=ctx.org_id)
    return [_out(t) for t in channels]


@router.post("", response_model=MemoryChannelOut)
async def create_channel(
    body: MemoryChannelCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_admin_ctx)],
) -> MemoryChannelOut:
    """Create a custom channel (platform-admin only)."""
    tag = await taxonomy.create_memory_channel(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        name=body.name,
        system_key=body.system_key,
    )
    return _out(tag)


@router.patch("/{channel_id}", response_model=MemoryChannelOut)
async def patch_channel(
    channel_id: uuid.UUID,
    body: MemoryChannelPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_admin_ctx)],
) -> MemoryChannelOut:
    """Rename and/or enable/disable a channel (platform-admin only). A
    seeded channel may be renamed and disabled but its key is immutable
    (channel.key_immutable)."""
    tag = await taxonomy.update_memory_channel(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        tag_id=channel_id,
        name=body.name,
        enabled=body.enabled,
        system_key=body.system_key,
    )
    return _out(tag)


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_admin_ctx)],
) -> None:
    """Delete a custom channel (platform-admin only). A seeded channel
    is not deletable -- disable it instead (channel.seeded_undeletable)."""
    await taxonomy.delete_memory_channel(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        tag_id=channel_id,
    )
