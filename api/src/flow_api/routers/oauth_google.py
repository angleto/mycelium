"""Google OAuth 2.0 router (epic #125 P1).

Two endpoints:

  - ``GET /oauth/google/start?scope=gmail|calendar|both`` (authenticated):
    returns the Google authorize URL plus the opaque signed state. The
    SPA navigates the browser there; the user agent then arrives at the
    public callback below.

  - ``GET /oauth/google/callback?code=...&state=...`` (public, no
    bearer): verifies the state's HMAC + freshness window, exchanges
    the authorization code for tokens at Google, stores the refresh
    token (encrypted via Fernet) on a Gmail ``EmailAccount`` (scope =
    gmail|both) and/or on a ``CalendarSubscription`` row (scope =
    calendar|both), then 302s the browser to
    ``frontend_base_url + /settings?google=connected``.

The HTTP boundary to Google is the ``GoogleApiClient`` Protocol; tests
inject a fake via ``set_google_api_client_override`` (the standard test
seam). The state is self-contained (signed JWT-style payload + HMAC, no
Redis): TTL ten minutes, single-use semantics are encouraged by the
short window and the nonce that bakes uniqueness into each token.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from starlette.responses import RedirectResponse

from flow_api.deps import TenantCtx, tenant_ctx
from flow_core.config import get_settings
from flow_core.crypto import encrypt_secret
from flow_core.db import tenant_session
from flow_core.errors import DomainError
from flow_core.google_api import (
    AUTHORIZE_URL,
    SCOPE_CALENDAR,
    SCOPE_EMAIL,
    SCOPE_GMAIL,
    SCOPE_OPENID,
    google_api_client,
)
from flow_core.google_oauth_state import (
    OAuthState,
    issue_state,
    verify_state,
)
from flow_core.i18n import MessageCode
from flow_core.models.calendar import WorkingCalendar
from flow_core.models.email import (
    EmailAccount,
    EmailAccountStatus,
    EmailProvider,
)
from flow_core.services import google_calendar as gcal_svc

router = APIRouter(prefix="/oauth/google", tags=["oauth"])

_SCOPES_BY_REQUEST: dict[str, tuple[str, ...]] = {
    "gmail": (SCOPE_OPENID, SCOPE_EMAIL, SCOPE_GMAIL),
    "calendar": (SCOPE_OPENID, SCOPE_EMAIL, SCOPE_CALENDAR),
    "both": (SCOPE_OPENID, SCOPE_EMAIL, SCOPE_GMAIL, SCOPE_CALENDAR),
}

OAuthScope = Literal["gmail", "calendar", "both"]


class OAuthStartOut(BaseModel):
    authorize_url: str
    state: str


@router.get("/start", response_model=OAuthStartOut)
async def start(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    scope: Annotated[OAuthScope, Query()] = "both",
) -> OAuthStartOut:
    s = get_settings()
    if not s.google_configured:
        raise DomainError(MessageCode.OAUTH_NOT_CONFIGURED)
    scopes = _SCOPES_BY_REQUEST.get(scope)
    if scopes is None:
        raise DomainError(MessageCode.OAUTH_SCOPE_INVALID)
    state = issue_state(user_id=ctx.user_id, org_id=ctx.org_id, scope=scope)
    params = {
        "response_type": "code",
        "client_id": s.google_client_id,
        "redirect_uri": s.google_redirect_uri,
        "scope": " ".join(scopes),
        # Force a refresh_token on every consent (Google omits it
        # otherwise on a returning user) so the stored envelope is
        # always usable.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return OAuthStartOut(
        authorize_url=f"{AUTHORIZE_URL}?{urlencode(params)}",
        state=state,
    )


async def _store_gmail_account(
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    email_address: str,
    refresh_token: str,
) -> None:
    """Idempotent: refresh the secret on an existing (org, email)
    EmailAccount, otherwise create one. Runs in a tenant session so RLS
    binds it to the right workspace; tagged by the caller."""
    async with tenant_session(str(org_id), str(user_id)) as s:
        existing = (
            await s.execute(select(EmailAccount).where(EmailAccount.email_address == email_address))
        ).scalar_one_or_none()
        if existing is not None:
            existing.secret_encrypted = encrypt_secret(refresh_token)
            existing.provider = EmailProvider.gmail
            existing.status = EmailAccountStatus.active
            existing.last_error = None
            existing.version = existing.version + 1
            await s.flush()
            return
        s.add(
            EmailAccount(
                org_id=org_id,
                provider=EmailProvider.gmail,
                email_address=email_address,
                secret_encrypted=encrypt_secret(refresh_token),
                status=EmailAccountStatus.active,
                # IMAP host / SMTP host: Gmail's standard endpoints. The
                # connector flips to XOAUTH2 by provider, so the host is
                # just for completeness.
                imap_host="imap.gmail.com",
                imap_port=993,
                smtp_host="smtp.gmail.com",
                smtp_port=587,
            )
        )
        await s.flush()


async def _store_calendar_subscription(
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    refresh_token: str,
) -> None:
    """Bind the consent to the workspace's default working calendar.
    Calendar pick / multi-calendar UX is P2; here we attach to the
    default so the consent is immediately useful (worker syncs it on
    the next tick)."""
    async with tenant_session(str(org_id), str(user_id)) as s:
        cal = (
            await s.execute(select(WorkingCalendar).where(WorkingCalendar.is_default.is_(True)))
        ).scalar_one_or_none()
        if cal is None:
            # No default calendar: pick any. A workspace always has at
            # least one (provisioning creates one).
            cal = (
                await s.execute(select(WorkingCalendar).order_by(WorkingCalendar.created_at))
            ).scalar_one_or_none()
            if cal is None:
                # Truly absent: skip silently rather than fail the
                # callback (a future P2 will let the user pick a target).
                return
        await gcal_svc.connect(
            s,
            org_id=org_id,
            actor_id=user_id,
            user_id=user_id,
            our_calendar_id=cal.id,
            refresh_token=refresh_token,
            google_calendar_id="primary",
        )


async def _email_from_id_token(id_token: str | None) -> str | None:
    """Extract the email claim from the Google id_token (it is a JWT;
    Google signs it, but we trust it here only because we just exchanged
    a freshly-minted code over TLS at Google's token endpoint). Returns
    None when the token is missing or malformed; the caller falls back."""
    if not id_token:
        return None
    try:
        # The id_token is a signed JWT. We do not validate the signature
        # here (the token was just returned to us over the TLS channel by
        # Google's token endpoint, which is the trust source); we only
        # decode the claims segment.
        import base64
        import json

        parts = id_token.split(".")
        if len(parts) < 2:
            return None
        body = parts[1]
        pad = "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body + pad).decode("utf-8"))
        email = claims.get("email")
        return str(email) if email else None
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


@router.get("/callback")
async def callback(
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
) -> RedirectResponse:
    s = get_settings()
    if not s.google_configured:
        raise DomainError(MessageCode.OAUTH_NOT_CONFIGURED)
    parsed: OAuthState = verify_state(state)
    api = google_api_client()
    try:
        tokens = await api.exchange_code(
            code=code,
            client_id=s.google_client_id,
            client_secret=s.google_client_secret,
            redirect_uri=s.google_redirect_uri,
        )
    except Exception as exc:
        raise DomainError(MessageCode.OAUTH_EXCHANGE_FAILED, detail=str(exc)) from exc
    if not tokens.refresh_token:
        # Google omits refresh_token on returning consent; we explicitly
        # set ``prompt=consent`` to force one. Surface the failure rather
        # than silently storing only the access_token (which expires).
        raise DomainError(
            MessageCode.OAUTH_EXCHANGE_FAILED,
            detail="missing refresh_token in token response",
        )
    refresh_token = tokens.refresh_token

    if parsed.scope in ("gmail", "both"):
        email_address = await _email_from_id_token(tokens.id_token)
        if email_address is None:
            # Fall back to the userinfo endpoint would add latency; this
            # is fine when the id_token is present (always the case for
            # ``openid email``). If it ever is absent we cannot construct
            # an EmailAccount, so surface a precise error.
            raise DomainError(
                MessageCode.OAUTH_EXCHANGE_FAILED,
                detail="missing email claim in id_token",
            )
        await _store_gmail_account(
            org_id=parsed.org_id,
            user_id=parsed.user_id,
            email_address=email_address,
            refresh_token=refresh_token,
        )

    if parsed.scope in ("calendar", "both"):
        await _store_calendar_subscription(
            org_id=parsed.org_id,
            user_id=parsed.user_id,
            refresh_token=refresh_token,
        )

    base = s.frontend_base_url.rstrip("/")
    return RedirectResponse(url=f"{base}/settings?google=connected", status_code=302)


__all__ = ["router"]
