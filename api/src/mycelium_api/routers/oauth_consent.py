"""The person's half of the public-client OAuth flow (ADR-0069).

Two groups of routes, all HUMAN_ONLY, for the reason the device approval
routes are: answering a consent is minting a credential, and a credential
that could answer one could mint its way out of its own scope.

**Consent** (``GET /oauth/consent``, ``POST /oauth/consent/approve``,
``POST /oauth/consent/deny``). The SPA's consent page reads the request the
browser was sent there with and answers it. Approve and deny return the URL
the browser goes to next -- the client's redirect URI with a code, or with
``error=access_denied`` -- rather than redirecting themselves, because these
are JSON calls authenticated by the bearer the SPA holds, not form posts a
cookie would authenticate. That is also what makes them safe against
cross-site request forgery: a page on another origin can make the browser
send a form, but it cannot make it attach the SPA's bearer.

**Hand-registered clients** (``/oauth/clients``). For an MCP host that wants
a client id pasted into it instead of registering itself. Listed and removed
per workspace.

The client half (register, authorize, token) is in ``routers/oauth.py``: it
is unauthenticated, speaks OAuth's error format rather than ours, and is not
part of the SPA's schema.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from mycelium_api.deps import TenantCtx, current_user, tenant_ctx
from mycelium_api.schemas import (
    OAuthClientCreateIn,
    OAuthClientOut,
    OAuthConsentAnswerOut,
    OAuthConsentApproveIn,
    OAuthConsentDenyIn,
    OAuthConsentOut,
)
from mycelium_core.config import get_settings
from mycelium_core.db import admin_session
from mycelium_core.errors import NotFoundError, UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.oauth_client import OAuthClient
from mycelium_core.models.user import User
from mycelium_core.oauth.redirects import RedirectUriError, policy_from_settings, redirect_target
from mycelium_core.oauth.strategies import default_registry
from mycelium_core.services import oauth_clients as clients_svc
from mycelium_core.services import oauth_consent as consent_svc

router = APIRouter(prefix="/oauth", tags=["oauth"])


def _client_enabled(client_id: str) -> bool:
    """Whether the strategy that owns this client id is switched on NOW. A
    consent opened while it was on cannot be completed after it is off."""
    return default_registry().claimant(client_id, get_settings()) is not None


def _require_registered_clients() -> None:
    """Hand-registered clients belong to the registered-clients strategy;
    while it is switched off they cannot be listed, created or removed
    here either, as if the feature were absent."""
    if not get_settings().oauth_registered_clients_enabled:
        raise NotFoundError(MessageCode.OAUTH_CLIENT_NOT_FOUND)


@router.get("/consent", response_model=OAuthConsentOut)
async def consent_describe(
    request: Annotated[str, Query(min_length=16, max_length=128)],
    user: Annotated[User, Depends(current_user)],
) -> OAuthConsentOut:
    """What the person is being asked to approve. Authenticated, and not
    scoped to the caller's workspace: the request belongs to nobody until it
    is answered, and the client may be a dynamic registration with no
    workspace at all, or an https URL no table holds. Everything shown is the
    snapshot taken when the client was resolved at /authorize, so this does
    no resolution and no network I/O. Anything that is not a live request is
    a 404."""
    async with admin_session() as session:
        pending = await consent_svc.describe(
            session, request_id=request, client_enabled=_client_enabled
        )
        row = pending.request
        return OAuthConsentOut(
            client_id=row.client_id,
            client_name=row.client_name,
            registration=row.client_registration,
            redirect_uri=row.redirect_uri,
            redirect_target=redirect_target(row.redirect_uri),
            expires_at=row.expires_at,
            requested_scope=row.requested_scope,
            suggested_scope=pending.suggested_scope,
        )


@router.post("/consent/approve", response_model=OAuthConsentAnswerOut)
async def consent_approve(
    body: OAuthConsentApproveIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> OAuthConsentAnswerOut:
    """Answer yes. The workspace is the one the person is in (the request's
    tenant context), never one the body or the client names; the scope is
    checked against the same role rule as creating an assistant by hand.
    Mints the authorization code, not the credential: the client collects
    that at ``/token``."""
    label = " ".join(body.label.split())
    url = await consent_svc.approve(
        ctx.session,
        request_id=body.request,
        approver_id=ctx.user_id,
        org_id=ctx.org_id,
        label=label,
        scope=body.scope,
        client_enabled=_client_enabled,
    )
    return OAuthConsentAnswerOut(redirect_url=url)


@router.post("/consent/deny", response_model=OAuthConsentAnswerOut)
async def consent_deny(
    body: OAuthConsentDenyIn,
    user: Annotated[User, Depends(current_user)],
) -> OAuthConsentAnswerOut:
    """Answer no. The client is told, through its own redirect, rather than
    left waiting for one that never comes."""
    async with admin_session() as session:
        url = await consent_svc.deny(session, request_id=body.request, actor_id=user.id)
    return OAuthConsentAnswerOut(redirect_url=url)


def _client_out(row: OAuthClient) -> OAuthClientOut:
    return OAuthClientOut(
        client_id=row.client_id,
        client_name=row.client_name,
        redirect_uris=list(row.redirect_uris),
        created_at=row.created_at,
        created_by_id=row.created_by_id,
    )


@router.get("/clients", response_model=list[OAuthClientOut])
async def list_clients(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[OAuthClientOut]:
    _require_registered_clients()
    rows = await clients_svc.list_manual(ctx.session, org_id=ctx.org_id)
    return [_client_out(r) for r in rows]


@router.post("/clients", response_model=OAuthClientOut, status_code=status.HTTP_201_CREATED)
async def create_client(
    body: OAuthClientCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> OAuthClientOut:
    """Register a public client by hand. Same redirect-URI policy as Dynamic
    Client Registration: one policy, whoever writes the row."""
    _require_registered_clients()
    try:
        uris = policy_from_settings(get_settings()).validate_list(body.redirect_uris)
    except RedirectUriError as exc:
        raise UnprocessableError(MessageCode.OAUTH_REDIRECT_URI_INVALID, uri=exc.uri) from None
    name = clients_svc.clean_client_name(body.client_name, fallback=body.client_name.strip())
    row = await clients_svc.register_manual(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        client_name=name,
        redirect_uris=uris,
    )
    return _client_out(row)


@router.delete("/clients/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client(
    client_id: str,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> Response:
    """Remove a hand-registered client: its author or the workspace owner.
    Credentials earlier consents created are NOT revoked by this; they are
    assistants in their own right."""
    _require_registered_clients()
    await clients_svc.delete_manual(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, client_id=client_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
