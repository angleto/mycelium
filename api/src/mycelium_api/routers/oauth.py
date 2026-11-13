"""OAuth 2.1 for MCP hosts: the HTTP adapter (ADR-0069).

Mycelium is both the authorization server and the protected resource (the
MCP transport at ``/mcp``), so everything a host needs to connect lives here
on the API host. This module only translates HTTP: what an OAuth client sent
goes to ``mycelium_core.oauth``, and what comes back is rendered as OAuth's
own ``{"error", "error_description"}`` body or a redirect. It does not know
which kinds of client exist.

The three layers it calls, each replaceable without the others:

* client RESOLUTION (``oauth.resolution`` and ``oauth.strategies``): an
  ordered registry of strategies -- registered public clients (dynamic or by
  hand), Client ID Metadata Documents, and AI-assistant UUID + secret -- each
  switchable in ``Settings``. The discovery document is generated from the
  ones switched on.
* AUTHORIZATION (``oauth.flow``): one path for every strategy. Consent
  clients go to the SPA's consent page (``routers/oauth_consent.py``);
  pre-authorized ones get a code at once.
* ISSUANCE (``oauth.issuance``): the installed ``CredentialIssuer`` builds
  the token response.

Endpoints:

* ``GET /.well-known/oauth-authorization-server`` (RFC 8414) and
  ``GET /.well-known/oauth-protected-resource[/<suffix>]`` (RFC 9728).
* ``POST /oauth/register`` (RFC 7591), served by whichever active strategy
  offers registration (``ClientRegistry.registration``); 404 while none does.
* ``GET /oauth/authorize`` and ``POST /oauth/token``.

Not provided, deliberately: refresh grants, token introspection and
revocation endpoints (revocation is per assistant, in the settings), client
secrets for public clients, and CORS (hosts register and exchange from a
server). The per-origin bounds on the open endpoints answer with the
server's domain error, as the device grant does.
"""

from __future__ import annotations

import base64
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from mycelium_api.deps import _resolve_issuer_client_ip
from mycelium_core import public_origin
from mycelium_core.config import get_settings
from mycelium_core.oauth import flow, issuance
from mycelium_core.oauth.origin import origin_bucket
from mycelium_core.oauth.resolution import RegistrationError
from mycelium_core.oauth.strategies import default_registry

logger = logging.getLogger("mycelium.oauth")

# Two routers: the well-known ones live at the host root (RFC 8414 / 9728 are
# explicit about the path); the rest lives at /api/oauth/* so nginx's /api/
# proxy block carries it.
well_known_router = APIRouter(tags=["oauth"], include_in_schema=False)
oauth_router = APIRouter(prefix="/oauth", tags=["oauth"], include_in_schema=False)

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _issuer(request: Request) -> str:
    """The externally visible base URL. One implementation, in core, shared
    with the MCP transport's 401 challenge so the two can never name
    different hosts (see ``mycelium_core.public_origin``)."""
    return public_origin.issuer(
        request.headers, scheme=request.url.scheme, netloc=request.url.netloc
    )


def _origin(request: Request) -> str | None:
    """The bucket the open endpoints count this caller against: the address
    the server resolved (never one the client declared), IPv6 per /64."""
    return origin_bucket(_resolve_issuer_client_ip(request))


def _oauth_error(*, status_code: int, error: str, description: str | None = None) -> JSONResponse:
    payload: dict[str, Any] = {"error": error}
    if description:
        payload["error_description"] = description
    logger.info("oauth error: status=%s error=%s description=%s", status_code, error, description)
    return JSONResponse(payload, status_code=status_code)


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


# ----- metadata ---------------------------------------------------------------


@well_known_router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(request: Request) -> JSONResponse:
    """RFC 8414, generated from the client strategies switched on: the
    registration endpoint only while dynamic registration is, the metadata-
    document flag only while that strategy is, the auth methods as the union
    of the active strategies'."""
    return JSONResponse(default_registry().metadata(_issuer(request), get_settings()))


@well_known_router.get("/.well-known/oauth-protected-resource")
@well_known_router.get("/.well-known/oauth-protected-resource/{suffix:path}")
async def protected_resource_metadata(request: Request, suffix: str = "") -> JSONResponse:
    """RFC 9728. The MCP Authorization spec constructs the well-known URL by
    inserting ``/.well-known/oauth-protected-resource`` between the host and
    the resource path: a resource at ``https://host/mcp`` exposes its metadata
    at ``https://host/.well-known/oauth-protected-resource/mcp``. Both the
    suffixed and the bare variant are served, the suffix flowing back into
    ``resource``."""
    iss = _issuer(request)
    suffix_clean = (suffix or "").strip("/")
    resource_url = f"{iss}/{suffix_clean}" if suffix_clean else public_origin.mcp_resource(iss)
    return JSONResponse(
        {
            "resource": resource_url,
            "authorization_servers": [iss],
            "bearer_methods_supported": ["header"],
            "resource_name": "Mycelium",
            "resource_documentation": iss,
        }
    )


# ----- registration -----------------------------------------------------------


@oauth_router.post("/register")
async def register_endpoint(request: Request) -> Response:
    """RFC 7591 Dynamic Client Registration, for public clients only.

    Unauthenticated by design: a host registering itself has nothing yet,
    and what it gets -- a ``client_id`` -- grants nothing until a logged-in
    person approves a request on the consent page. Bounded per network origin
    and globally (``oauth_clients.check_registration_rate``); unused rows are
    swept after a day."""
    settings = get_settings()
    # Whichever active strategy offers registration answers it; the endpoint
    # does not know which one that is.
    registrar = default_registry().registration(settings)
    if registrar is None:
        return _oauth_error(
            status_code=404,
            error="invalid_request",
            description="dynamic client registration is not enabled",
        )
    try:
        body: object = await request.json()
    except Exception:
        body = None
    outcome = await registrar.register_client(body=body, origin=_origin(request), settings=settings)
    if isinstance(outcome, RegistrationError):
        return _oauth_error(status_code=400, error=outcome.error, description=outcome.description)
    return JSONResponse(outcome, status_code=201, headers=_NO_STORE)


# ----- authorize + token --------------------------------------------------------


def _render(outcome: flow.Redirect | flow.OAuthFailure) -> Response:
    if isinstance(outcome, flow.Redirect):
        return RedirectResponse(outcome.location, status_code=302)
    return _oauth_error(
        status_code=outcome.status, error=outcome.error, description=outcome.description
    )


@oauth_router.get("/authorize")
async def authorize_endpoint(request: Request) -> Response:
    """Authorization code request. Consent clients are sent to the consent
    page with nothing minted; pre-authorized clients get a code at once
    (``oauth.flow``)."""
    outcome = await flow.authorize(
        request.query_params,
        issuer=_issuer(request),
        origin_ip=_origin(request),
        settings=get_settings(),
        registry=default_registry(),
    )
    return _render(outcome)


@oauth_router.post("/token")
async def token_endpoint(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Exchange a code for a token. The response is built by the installed
    credential issuer (``oauth.issuance``)."""
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
                status_code=400, error="invalid_request", description="form body required"
            )

    client_id = str(body.get("client_id", "")).strip()
    client_secret = str(body.get("client_secret", "")).strip()
    basic = _basic_auth_creds(authorization)
    if basic is not None:
        client_id = client_id or basic[0]
        client_secret = client_secret or basic[1]

    # The SHAPE of the request, never its secrets: enough to tell "client
    # unknown" from "code expired" from "verifier wrong" in the log.
    logger.info(
        "token request: ct=%s keys=%s grant=%s cid=%s basic=%s",
        content_type,
        sorted(body.keys()),
        body.get("grant_type"),
        client_id[:80],
        "yes" if basic else "no",
    )
    outcome = await flow.exchange(
        body,
        client_id=client_id,
        client_secret=client_secret,
        issuer=_issuer(request),
        settings=get_settings(),
        registry=default_registry(),
        credential_issuer=issuance.credential_issuer(),
    )
    if isinstance(outcome, flow.OAuthFailure):
        return _oauth_error(
            status_code=outcome.status, error=outcome.error, description=outcome.description
        )
    return JSONResponse(outcome.body(), headers=_NO_STORE)


__all__ = ["oauth_router", "well_known_router"]
