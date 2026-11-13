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
   Every 401 carries ``WWW-Authenticate: Bearer resource_metadata=…`` so an
   OAuth-capable client can discover how to obtain a token.
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

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mycelium_core import public_origin
from mycelium_core.services import agent_tokens as agent_tokens_svc
from mycelium_mcp.gateway import gateway
from mycelium_mcp.server import _PRINCIPAL, _PRINCIPAL_SCOPE


def _unauthorized(request: Request, message: str) -> JSONResponse:
    """A 401 that tells the client where to start OAuth.

    RFC 9728 §5.1 and the MCP authorization specification: the challenge
    names the protected-resource metadata URL, which names the
    authorization server, which names the registration, authorization and
    token endpoints. A client that gets a bare 401 has to guess the first
    URL, and several do not guess -- they stop. Built from the same issuer
    the metadata itself is built from (``public_origin``), so the URL the
    header names is the one that answers."""
    iss = public_origin.issuer(
        request.headers, scheme=request.url.scheme, netloc=request.url.netloc
    )
    challenge = public_origin.bearer_challenge(iss)
    return JSONResponse(
        {"error": message}, status_code=401, headers={"WWW-Authenticate": challenge}
    )


class _BearerAuthMiddleware:
    """Resolve ``Authorization: Bearer mycelium_at_…`` to an
    ``AuthenticatedAgent`` and publish the principal into ``_PRINCIPAL``
    for the duration of the request. The session manager and the tool
    dispatch start their tasks from this one, so the ContextVar is visible
    all the way to ``_tenant`` inside the @mcp.tool body.

    Raw ASGI, not ``BaseHTTPMiddleware``. The latter runs the app in a
    separate task and reads its response back through a stream; when the
    app ends a streamed exchange without that stream yielding a response
    start, it raises "No response returned." and the client gets a 500.
    Driving the official MCP SDK client against a real server hit exactly
    that on ``POST /mcp`` in 2 runs out of 6 (2026-11-13). A raw middleware
    passes ``send`` through untouched, so there is no second stream to lose
    the response in, and streamed responses are not buffered either."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        header = request.headers.get("authorization", "")
        refusal: str | None = None
        principal = None
        if not header or not header.lower().startswith("bearer "):
            refusal = "Missing or malformed Authorization header"
        else:
            raw = header.split(" ", 1)[1].strip()
            if not agent_tokens_svc.is_agent_token(raw):
                refusal = "Bearer is not a Mycelium agent token"
            else:
                principal = await agent_tokens_svc.authenticate(raw)
                if principal is None:
                    refusal = "Unknown / revoked / expired token"
        if refusal is not None or principal is None:
            await _unauthorized(request, refusal or "Unauthorized")(scope, receive, send)
            return
        token = _PRINCIPAL.set((principal.user_id, principal.org_id, principal.token_id))
        # Publish the bound assistant's scope so the gateway gate can enforce it
        # per tool (task c19f2f63, enabler B). None for a bare token = full
        # access; a scope list restricts a bound assistant to those keys.
        scope_token = _PRINCIPAL_SCOPE.set(principal.assistant_scope)
        try:
            await self.app(scope, receive, send)
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
