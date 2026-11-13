"""How an MCP tool bound to a registry operation opens its context
(ADR-0068): the tenant session of the call, and the agent token behind it
so an operation can name the assistant that carried out an act."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mycelium_core.operations import OpCtx


@asynccontextmanager
async def op_ctx(token: str, org_id: str) -> AsyncIterator[OpCtx]:
    # Imported here, not at the top: ``server`` imports the tool modules
    # that import this one, so a module-level import is a cycle whenever a
    # caller imports a tool module before the server.
    from mycelium_mcp.server import _PRINCIPAL, _tenant

    async with _tenant(token, org_id) as (session, org, user):
        principal = _PRINCIPAL.get()
        yield OpCtx(
            session=session,
            org_id=org,
            actor_id=user,
            agent_token_id=principal[2] if principal is not None else None,
        )
