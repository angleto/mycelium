"""HTTP transport for the MCP server.

Wraps the existing stdio-mode FastMCP instance (``flow_mcp.server.mcp``)
with a streamable-http Starlette app + a Bearer middleware that
authenticates each request via the SECURITY DEFINER
``authenticate_agent_token`` function (migration 0059) and publishes
the resolved principal into ``_PRINCIPAL`` so the tools'
``_tenant`` context picks it up without re-decoding.

Mounted under ``/mcp`` inside the existing flow_api FastAPI app
(``api/src/flow_api/app.py``): same process, same DB pool, same
ingress. There is no separate K8s Service or pod — the MCP and REST
surfaces are co-equal adapters over ``flow_core`` (docs/adr/0001).

Auth flow per request:
1. Read ``Authorization: Bearer <flow_at_…>``; reject 401 if absent.
2. ``agent_tokens.authenticate(raw)`` → ``AuthenticatedAgent`` or None.
   The SECURITY DEFINER function already enforces token expiry,
   revocation, and the bound assistant's ``is_active`` flag.
3. Bind ``(user_id, org_id)`` into ``_PRINCIPAL`` for the duration of
   the request. ``_tenant`` inside every ``@mcp.tool()`` short-circuits
   on the ctxvar — bearer args on the tool become tolerated empties.

The per-assistant scope list (``AuthenticatedAgent.assistant_scope``)
is captured here but **not yet enforced**: the gate that filters each
@mcp.tool by scope is the "hook each tool to a scope key" follow-up
flagged in ``core/src/flow_core/mcp_scopes.py``. Until then assistant
tokens behave as full-MCP just like legacy bare agent tokens, which
matches the v1.2.16 backend contract.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from flow_core.services import agent_tokens as agent_tokens_svc
from flow_mcp.server import _PRINCIPAL, mcp


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Resolve ``Authorization: Bearer flow_at_…`` to an
    ``AuthenticatedAgent`` and publish the principal into ``_PRINCIPAL``
    for the duration of the request. The session manager + tool
    dispatch run on the same task, so the ContextVar is visible all
    the way to ``_tenant`` inside the @mcp.tool body."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        header = request.headers.get("authorization") or request.headers.get("Authorization", "")
        if not header or not header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "Missing or malformed Authorization header"},
                status_code=401,
            )
        raw = header.split(" ", 1)[1].strip()
        if not agent_tokens_svc.is_agent_token(raw):
            return JSONResponse(
                {"error": "Bearer is not a Flow agent token"},
                status_code=401,
            )
        principal = await agent_tokens_svc.authenticate(raw)
        if principal is None:
            return JSONResponse(
                {"error": "Unknown / revoked / expired token"},
                status_code=401,
            )
        token = _PRINCIPAL.set((principal.user_id, principal.org_id))
        try:
            response: Response = await call_next(request)
            return response
        finally:
            _PRINCIPAL.reset(token)


def make_mcp_app() -> Starlette:
    """Build the streamable-http Starlette app + bearer auth middleware.
    Idempotent: re-invoking returns a fresh app sharing the same FastMCP
    instance + tool registry (the in-process session manager is created
    lazily on first ``streamable_http_app()`` call)."""
    # Inner FastMCP route at '/' so when this app is mounted at '/mcp'
    # in the FastAPI parent, the public URL is just '/mcp' (otherwise
    # the default '/mcp' route would compose to '/mcp/mcp').
    mcp.settings.streamable_http_path = "/"
    app = mcp.streamable_http_app()
    app.add_middleware(_BearerAuthMiddleware)
    return app


__all__ = ["make_mcp_app"]
