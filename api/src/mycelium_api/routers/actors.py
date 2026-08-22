"""Actors router: read-only ``@handle`` directory the SPA's assignee
picker (and future MCP tools) walks to resolve human-readable IDs to
concrete principals (users + AI assistants).

Stage A of #21 (kill Executor model) — see migration 0060 and
``core/src/mycelium_core/services/actors.py`` for the resolver. Future
stages add mutation endpoints (``PATCH /me/handle`` etc.) and
``llm_embedded`` actors.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_core.services import actors as svc

router = APIRouter(prefix="/actors", tags=["actors"])


class ActorOut(BaseModel):
    """One row of the directory. The kind discriminator lets the
    picker render a kind-aware glyph (user / assistant) and the SPA
    decide what to do on click."""

    handle: str
    kind: str
    display_name: str
    ref_id: str


@router.get("", response_model=list[ActorOut])
async def list_actors(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    q: str | None = None,
    limit: int = 50,
    include_inactive: bool = False,
) -> list[ActorOut]:
    """The assignable directory. Deactivated users and assistants are
    excluded by default: they cannot log in / cannot authenticate, so a
    picker must not offer them. ``include_inactive=true`` is for the
    callers that resolve an EXISTING assignment or ownership back to a
    name (the owner chip has no other source for it) rather than offer
    a choice."""
    rows = await svc.list_actors(
        ctx.session,
        org_id=ctx.org_id,
        q=q,
        limit=max(1, min(limit, 200)),
        include_inactive=include_inactive,
    )
    return [
        ActorOut(
            handle=a.handle,
            kind=a.kind,
            display_name=a.display_name,
            ref_id=str(a.ref_id),
        )
        for a in rows
    ]
