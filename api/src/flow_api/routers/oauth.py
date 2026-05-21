"""MCP OAuth 2.1 + PKCE shim, ported from bitvision_phoenix
(``bvmcp.oauth_shim``).

Flow's MCP transport at ``/mcp`` already accepts a static
``Authorization: Bearer flow_at_…`` (the agent_token system). Claude
Desktop's "Add custom connector" flow doesn't speak that: when the
operator pastes the server URL + client_id + client_secret, Claude
performs a standard OAuth 2.1 Authorization Code + PKCE handshake.
Without the metadata + authorize + token endpoints the dance dies at
``/.well-known/oauth-authorization-server`` (which today returns the
SPA HTML, since nginx falls back to the SPA for unknown paths).

This module is the thin wrapper that makes Claude.ai / Claude
Desktop and every spec-compliant MCP host happy. Three endpoints +
one metadata pair:

* ``GET /.well-known/oauth-authorization-server`` (RFC 8414) and
  ``GET /.well-known/oauth-protected-resource[/<suffix>]`` (RFC 9728)
  advertise the auth + resource metadata. Both live on the API host
  because we are auth server *and* resource server.
* ``GET /authorize`` — accepts the standard authorization-code
  request, mints a code via ``services.oauth_codes.mint`` (Postgres-
  backed so any replica can redeem it), 302s back to
  ``redirect_uri?code=…&state=…``. No consent page: the human
  approval already happened in ``/settings → AI assistants`` when
  the operator minted the credential.
* ``POST /token`` — calls ``services.oauth_codes.consume`` to pop
  the bound challenge, runs PKCE verification locally, validates the
  submitted ``client_secret`` against the existing agent_token
  system, returns ``access_token = <client_secret>`` so subsequent
  MCP calls reuse the same bearer flow the ``/mcp`` middleware
  already understands.

The server is intentionally not a full OAuth provider: no refresh
tokens (the static client_secret IS the long-lived credential), no
dynamic client registration (clients are pre-issued as AI assistants
in Flow), no introspection. Anything beyond the Claude.ai flow falls
through to a 4xx with an OAuth-shaped error body.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import uuid
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select

from flow_core.db import admin_session
from flow_core.models.ai_assistant import AiAssistant
from flow_core.services import agent_tokens as agent_tokens_svc
from flow_core.services import oauth_codes as codes_svc

logger = logging.getLogger("flow.oauth_shim")

# Two routers: the well-known ones live at the host root (RFC 8414 /
# 9728 are explicit about the path); the authorize/token pair lives
# at /api/oauth/* so nginx's existing /api/ proxy block carries them.
# The well-known router carries no prefix and is mounted with the
# include_router(..., prefix="") form in app.py.
well_known_router = APIRouter(tags=["oauth"], include_in_schema=False)
oauth_router = APIRouter(prefix="/oauth", tags=["oauth"], include_in_schema=False)


def _issuer(request: Request) -> str:
    """Return the externally-visible base URL. Honors X-Forwarded-*
    when the deployment publishes them; falls back to the Host header.
    An override via ``FLOW_PUBLIC_BASE_URL`` env wins for setups where
    the host header is unreliable."""
    import os

    override = os.getenv("FLOW_PUBLIC_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get(
        "host", request.url.netloc
    )
    return f"{proto}://{host}"


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    if method.upper() != "S256":
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, code_challenge)


def _oauth_error(
    *,
    status_code: int,
    error: str,
    description: str | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"error": error}
    if description:
        payload["error_description"] = description
    logger.info(
        "oauth_shim error: status=%s error=%s description=%s",
        status_code,
        error,
        description,
    )
    return JSONResponse(payload, status_code=status_code)


def _is_acceptable_redirect(uri: str) -> bool:
    """Accept https:// callbacks and localhost loopbacks. ``http://``
    on a non-loopback host is rejected — the secret travels through
    the redirect-URI host's TLS terminator in the code response, so a
    cleartext callback would leak it. Claude Desktop registers
    https://claude.ai/api/.../callback so the strict policy works."""
    if uri.startswith("https://"):
        return True
    return uri.startswith("http://localhost") or uri.startswith("http://127.0.0.1")


def _basic_auth_creds(authorization: str | None) -> tuple[str, str] | None:
    if not authorization or not authorization.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(authorization[6:].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in raw:
        return None
    cid, _, sec = raw.partition(":")
    return cid, sec


# ----- metadata endpoints -------------------------------------------------


@well_known_router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(request: Request) -> JSONResponse:
    """RFC 8414. The authorize + token endpoints sit under /api/oauth
    because nginx already proxies /api/* to the backend; the issuer
    is the host root so MCP clients can compose the URLs from it."""
    iss = _issuer(request)
    return JSONResponse(
        {
            "issuer": iss,
            "authorization_endpoint": f"{iss}/api/oauth/authorize",
            "token_endpoint": f"{iss}/api/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
            ],
            "scopes_supported": [],
            "service_documentation": iss,
            "client_uri": iss,
        }
    )


@well_known_router.get("/.well-known/oauth-protected-resource")
@well_known_router.get("/.well-known/oauth-protected-resource/{suffix:path}")
async def protected_resource_metadata(request: Request, suffix: str = "") -> JSONResponse:
    """RFC 9728. The MCP Authorization spec constructs the well-known
    URL by inserting ``/.well-known/oauth-protected-resource`` between
    the host and the resource path: a resource at ``https://host/mcp``
    exposes its metadata at
    ``https://host/.well-known/oauth-protected-resource/mcp``. We
    serve both the path-suffixed variant and the bare one and let the
    captured suffix flow back into the ``resource`` field."""
    iss = _issuer(request)
    suffix_clean = (suffix or "").strip("/")
    resource_url = f"{iss}/{suffix_clean}" if suffix_clean else f"{iss}/mcp"
    return JSONResponse(
        {
            "resource": resource_url,
            "authorization_servers": [iss],
            "bearer_methods_supported": ["header"],
            "resource_name": "Flow",
            "resource_documentation": iss,
        }
    )


# ----- authorize + token --------------------------------------------------


@oauth_router.get("/authorize")
async def authorize_endpoint(request: Request) -> Response:
    """Authorization code request. We don't render a consent page —
    the operator already chose this assistant's scope in
    ``/settings → AI assistants``. Bind the PKCE challenge to a fresh
    code and 302 back to the redirect_uri.

    ``client_id`` is the AI assistant's UUID; the value the SPA shows
    in the Credentials reveal card next to the secret."""
    params = request.query_params
    response_type = params.get("response_type", "")
    client_id = params.get("client_id", "").strip()
    redirect_uri = params.get("redirect_uri", "").strip()
    code_challenge = params.get("code_challenge", "").strip()
    code_challenge_method = params.get("code_challenge_method", "S256").strip()
    state = params.get("state", "")

    if response_type != "code":
        return _oauth_error(
            status_code=400,
            error="unsupported_response_type",
            description="only response_type=code is supported",
        )
    try:
        client_uuid = uuid.UUID(client_id)
    except (ValueError, AttributeError):
        return _oauth_error(
            status_code=400,
            error="invalid_request",
            description="client_id must be the AI assistant UUID",
        )
    if not redirect_uri or not _is_acceptable_redirect(redirect_uri):
        return _oauth_error(
            status_code=400,
            error="invalid_request",
            description="redirect_uri missing or not https / loopback",
        )
    if not code_challenge:
        return _oauth_error(
            status_code=400,
            error="invalid_request",
            description="code_challenge required (PKCE)",
        )
    if code_challenge_method.upper() != "S256":
        return _oauth_error(
            status_code=400,
            error="invalid_request",
            description="code_challenge_method must be S256",
        )

    # Validate the assistant exists and is active. We do not gate on
    # client_secret at /authorize -- that comes at /token. The check
    # here is "is this a real client we should issue a code for".
    async with admin_session() as session:
        row = (
            await session.execute(
                select(AiAssistant.id, AiAssistant.is_active).where(AiAssistant.id == client_uuid)
            )
        ).first()
    if row is None or not row[1]:
        return _oauth_error(
            status_code=400,
            error="invalid_client",
            description="unknown or revoked assistant",
        )

    async with admin_session() as session:
        code = await codes_svc.mint(
            session,
            client_id=str(client_uuid),
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )

    target_qs = {"code": code}
    if state:
        target_qs["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    target = f"{redirect_uri}{sep}{urlencode(target_qs)}"
    return RedirectResponse(target, status_code=302)


@oauth_router.post("/token")
async def token_endpoint(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Exchange a code for an access token.

    The access token we return is literally the assistant's
    ``client_secret`` (the raw ``flow_at_…``) — the MCP HTTP
    middleware resolves it via ``agent_tokens.authenticate`` on every
    MCP call. Reusing the static credential as the access_token
    keeps the bearer life-cycle (rotate, revoke, scope changes) on
    the existing AI assistant row; we don't mint a second short-lived
    token whose revocation we'd have to track separately."""
    content_type = request.headers.get("content-type", "")
    body: dict[str, Any] = {}
    if "application/json" in content_type:
        try:
            raw_body = await request.json()
            if isinstance(raw_body, dict):
                body = raw_body
        except Exception:
            body = {}
    else:
        try:
            form = await request.form()
            body = {k: str(v) for k, v in form.items()}
        except Exception:
            return _oauth_error(
                status_code=400,
                error="invalid_request",
                description="form body required",
            )

    grant_type = str(body.get("grant_type", ""))
    code = str(body.get("code", "")).strip()
    redirect_uri = str(body.get("redirect_uri", "")).strip()
    code_verifier = str(body.get("code_verifier", "")).strip()
    form_client_id = str(body.get("client_id", "")).strip()
    form_client_secret = str(body.get("client_secret", "")).strip()

    basic = _basic_auth_creds(authorization)
    if basic is not None:
        if not form_client_id:
            form_client_id = basic[0]
        if not form_client_secret:
            form_client_secret = basic[1]

    if grant_type != "authorization_code":
        return _oauth_error(
            status_code=400,
            error="unsupported_grant_type",
            description="only authorization_code is supported",
        )
    if not code or not code_verifier or not form_client_id or not form_client_secret:
        return _oauth_error(
            status_code=400,
            error="invalid_request",
            description="code, code_verifier, client_id and client_secret are required",
        )

    async with admin_session() as session:
        entry = await codes_svc.consume(session, code=code)
    if entry is None:
        return _oauth_error(
            status_code=400, error="invalid_grant", description="code unknown or expired"
        )
    if not secrets.compare_digest(entry.client_id, form_client_id):
        return _oauth_error(
            status_code=400, error="invalid_grant", description="client_id mismatch"
        )
    if redirect_uri and not secrets.compare_digest(entry.redirect_uri, redirect_uri):
        return _oauth_error(
            status_code=400, error="invalid_grant", description="redirect_uri mismatch"
        )
    if not _verify_pkce(code_verifier, entry.code_challenge, entry.code_challenge_method):
        return _oauth_error(
            status_code=400, error="invalid_grant", description="PKCE verification failed"
        )

    # Validate client_secret against the agent_token system. The
    # secret must (a) be a valid agent token, AND (b) be bound to the
    # AI assistant whose UUID equals client_id. The second check
    # prevents using one assistant's secret with a different
    # assistant's client_id to bypass the (future) per-assistant
    # scope enforcement.
    if not agent_tokens_svc.is_agent_token(form_client_secret):
        return _oauth_error(
            status_code=401,
            error="invalid_client",
            description="client_secret is not a Flow agent token",
        )
    principal = await agent_tokens_svc.authenticate(form_client_secret)
    if principal is None:
        return _oauth_error(
            status_code=401, error="invalid_client", description="client_secret rejected"
        )
    if principal.assistant_id is None or str(principal.assistant_id) != form_client_id:
        return _oauth_error(
            status_code=401,
            error="invalid_client",
            description="client_secret is not bound to that client_id",
        )

    return JSONResponse(
        {
            "access_token": form_client_secret,
            "token_type": "Bearer",
            # The bearer is a long-lived static secret. We surface a
            # nominal expiry so well-behaved clients refresh through
            # the connector if we ever switch to short-lived JWTs,
            # but in practice the secret is valid until the operator
            # rotates or revokes it from /settings.
            "expires_in": 31536000,
            "scope": " ".join(sorted(principal.assistant_scope or [])),
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


__all__ = ["oauth_router", "well_known_router"]
