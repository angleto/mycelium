"""HTTP transport for the MCP server.

Serves the dynamic-toolset *gateway* (``mycelium_mcp.gateway.gateway``,
three meta-tools) rather than the full ~140-tool registry, so the
``tools/list`` payload an OAuth client carries drops from ~21k to ~1k
tokens. The concrete tools stay on ``mycelium_mcp.server.mcp`` and are
dispatched by ``execute_tool``; the stdio entrypoint still serves them
directly. See ``gateway.py`` for the rationale.

It wraps the gateway in a streamable-http Starlette app + a Bearer
middleware that authenticates each request via the SECURITY DEFINER
``authenticate_agent_token`` function (migration 0059) and publishes
the resolved principal into ``_PRINCIPAL`` so the tools'
``_tenant`` context picks it up without re-decoding.

Mounted under ``/mcp`` inside the existing mycelium_api FastAPI app
(``api/src/mycelium_api/app.py``): same process, same DB pool, same
ingress. There is no separate K8s Service or pod — the MCP and REST
surfaces are co-equal adapters over ``mycelium_core`` (docs/adr/0001).

Auth flow per request:
1. Read ``Authorization: Bearer <mycelium_at_…>``; reject 401 if absent.
2. ``agent_tokens.authenticate(raw)`` → ``AuthenticatedAgent`` or None.
   The SECURITY DEFINER function already enforces token expiry,
   revocation, and the bound assistant's ``is_active`` flag.
3. Bind ``(user_id, org_id)`` into ``_PRINCIPAL`` for the duration of
   the request. ``_tenant`` inside every ``@mcp.tool()`` short-circuits
   on the ctxvar — bearer args on the tool become tolerated empties.

The per-assistant scope list (``AuthenticatedAgent.assistant_scope``) is
published into ``_PRINCIPAL_SCOPE`` and enforced per tool by the gateway gate
(``server._scope_permits`` / ``tool_scopes``; task c19f2f63, enabler B). A bare
agent token carries no scope list and keeps full-MCP access, as before.

The transport runs STATELESS (``stateless_http=True`` in ``make_mcp_app``): the
scope is re-read from the bearer on every request rather than frozen into a
long-lived session task at ``initialize``. See ``make_mcp_app`` for why.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mycelium_core.services import agent_tokens as agent_tokens_svc
from mycelium_mcp.gateway import gateway
from mycelium_mcp.server import _PRINCIPAL, _PRINCIPAL_SCOPE


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Resolve ``Authorization: Bearer mycelium_at_…`` to an
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
                {"error": "Bearer is not a Mycelium agent token"},
                status_code=401,
            )
        principal = await agent_tokens_svc.authenticate(raw)
        if principal is None:
            return JSONResponse(
                {"error": "Unknown / revoked / expired token"},
                status_code=401,
            )
        token = _PRINCIPAL.set((principal.user_id, principal.org_id, principal.token_id))
        # Publish the bound assistant's scope so the gateway gate can enforce it
        # per tool (task c19f2f63, enabler B). None for a bare token = full
        # access; a scope list restricts a bound assistant to those keys.
        scope_token = _PRINCIPAL_SCOPE.set(principal.assistant_scope)
        try:
            response: Response = await call_next(request)
            return response
        finally:
            _PRINCIPAL_SCOPE.reset(scope_token)
            _PRINCIPAL.reset(token)


def make_mcp_app() -> Starlette:
    """Build the streamable-http Starlette app + bearer auth middleware.
    Idempotent: re-invoking returns a fresh app sharing the same FastMCP
    instance + tool registry (the in-process session manager is created
    lazily on first ``streamable_http_app()`` call)."""
    from mcp.server.transport_security import TransportSecuritySettings

    # Inner FastMCP route at '/' so when this app is mounted at '/mcp'
    # in the FastAPI parent, the public URL is just '/mcp' (otherwise
    # the default '/mcp' route would compose to '/mcp/mcp').
    gateway.settings.streamable_http_path = "/"
    # Disable the SDK's DNS-rebinding Host/Origin guard. FastMCP
    # auto-enables it (with allowed_hosts = localhost-only) because the
    # instance's default host is 127.0.0.1; behind nginx the public
    # Host is mycelium.xeno.garden and the Origin is the MCP client
    # (claude.ai, Cursor, ...), so the localhost allowlist returns
    # 421 Misdirected Request on every call. DNS rebinding is a
    # localhost-server threat (a malicious page reaching a loopback
    # MCP); for a public HTTPS server the real gate is the
    # ``mycelium_at_`` bearer + the nginx Host routing, both already in
    # place. Setting an explicit (disabled) policy overrides the
    # auto-enabled localhost one.
    gateway.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    # Stateless transport (task c19f2f63, enabler B). In stateful mode the SDK
    # starts ONE long-lived per-session task at ``initialize`` and anyio copies
    # the caller's context into it, so ``_PRINCIPAL`` / ``_PRINCIPAL_SCOPE`` are
    # frozen for the session's whole life (sessions never expire) and the
    # Mcp-Session-Id becomes unbound ambient authority (a leaked id replays the
    # frozen principal). Stateless starts a FRESH per-REQUEST task instead, so
    # each request runs with the scope the bearer middleware just published for
    # THAT token -- the scope can never outlive a revocation, and there is no
    # session id to replay. The gateway is pure request/response (no progress /
    # sampling / elicitation / server-initiated notifications), so statelessness
    # costs nothing; the streamable-http session was only ever a scope-freeze
    # and a replay surface here. The REST surface already authenticates per
    # request; this brings MCP in line.
    gateway.settings.stateless_http = True
    app = gateway.streamable_http_app()
    app.add_middleware(_BearerAuthMiddleware)
    return app


__all__ = ["make_mcp_app"]
