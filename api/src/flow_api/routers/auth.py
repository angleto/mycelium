"""Auth router: signup, login, email verification, password reset,
logout (server-side JWT revocation). Thin adapter over flow_core
(ADR-0024). Enumeration-safe endpoints return a fixed status
regardless of whether the address exists.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from flow_api.deps import current_claims, current_user
from flow_api.schemas import (
    EmailIn,
    LoginIn,
    LoginMfaIn,
    LogoutIn,
    MeOut,
    RefreshIn,
    ResetPasswordIn,
    SignupIn,
    SignupOut,
    TokenOut,
    VerifyEmailIn,
)
from flow_core.config import get_settings
from flow_core.db import admin_session
from flow_core.errors import ForbiddenError
from flow_core.i18n import MessageCode
from flow_core.models.user import User
from flow_core.services.auth import (
    login,
    login_mfa,
    refresh_session,
    request_password_reset,
    resend_verification,
    reset_password,
    revoke_refresh_family,
    revoke_token,
    signup,
    verify_email,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=SignupOut)
async def signup_endpoint(body: SignupIn) -> SignupOut:
    # Single-user prod can disable public self-service signup
    # (FLOW_ALLOW_SIGNUP=false). The gate is HTTP-only: the bootstrap
    # (`python -m flow_core.bootstrap_admin`) and tests call the
    # `signup` service directly and are intentionally unaffected.
    if not get_settings().allow_signup:
        raise ForbiddenError(MessageCode.AUTH_SIGNUP_DISABLED)
    # Personal-first: never force "create an organization". A personal
    # workspace is auto-provisioned; naming it is optional.
    workspace_name = body.workspace_name or body.display_name or "Personal"
    async with admin_session() as session:
        result = await signup(
            session,
            email=body.email,
            password=body.password,
            org_name=workspace_name,
        )
    return SignupOut(
        user_id=result.user_id,
        workspace_id=result.org_id,
        token=result.token,
        refresh_token=result.refresh_token,
        email_verification_required=result.email_verification_required,
    )


@router.post("/login", response_model=TokenOut)
async def login_endpoint(body: LoginIn) -> TokenOut:
    async with admin_session() as session:
        pair = await login(session, email=body.email, password=body.password)
    return TokenOut(token=pair.access_token, refresh_token=pair.refresh_token)


@router.post("/login-mfa", response_model=TokenOut)
async def login_mfa_endpoint(body: LoginMfaIn) -> TokenOut:
    """Combined password + TOTP/backup-code login. Use once /auth/login
    has answered 401 auth.mfa_required."""
    async with admin_session() as session:
        pair = await login_mfa(
            session,
            email=body.email,
            password=body.password,
            totp_code=body.totp_code,
        )
    return TokenOut(token=pair.access_token, refresh_token=pair.refresh_token)


@router.post("/refresh", response_model=TokenOut)
async def refresh_endpoint(body: RefreshIn) -> TokenOut:
    """Rotate the refresh token, mint a fresh access JWT. The presented
    refresh row is single-use: a replay revokes the whole family (theft
    signal). All failure modes collapse to 401 invalid-token."""
    async with admin_session() as session:
        pair = await refresh_session(session, raw_refresh=body.refresh_token)
    return TokenOut(token=pair.access_token, refresh_token=pair.refresh_token)


@router.get("/me", response_model=MeOut)
async def me_endpoint(
    user: Annotated[User, Depends(current_user)],
) -> MeOut:
    """Canonical identity for the SPA. Server-checks is_admin (the JWT
    claim is only a render hint and may lag a role change)."""
    return MeOut(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
    )


@router.post("/verify-email", response_model=TokenOut)
async def verify_email_endpoint(body: VerifyEmailIn) -> TokenOut:
    async with admin_session() as session:
        pair = await verify_email(session, raw_token=body.token)
    return TokenOut(token=pair.access_token, refresh_token=pair.refresh_token)


@router.post("/resend-verification", status_code=status.HTTP_202_ACCEPTED)
async def resend_verification_endpoint(body: EmailIn) -> Response:
    async with admin_session() as session:
        await resend_verification(session, email=body.email)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password_endpoint(body: EmailIn) -> Response:
    async with admin_session() as session:
        await request_password_reset(session, email=body.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password_endpoint(body: ResetPasswordIn) -> Response:
    async with admin_session() as session:
        await reset_password(session, raw_token=body.token, new_password=body.new_password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_endpoint(
    claims: Annotated[dict[str, Any], Depends(current_claims)],
    body: LogoutIn | None = None,
) -> Response:
    """Real server-side logout: revoke the current access token's jti
    AND (when the SPA sends it) the entire refresh family, so neither
    credential can be reused."""
    jti = claims.get("jti")
    sub = claims.get("sub")
    exp = claims.get("exp")
    subject = uuid.UUID(sub) if isinstance(sub, str) else None
    raw_refresh = body.refresh_token if body is not None else None
    async with admin_session() as session:
        if isinstance(jti, str) and isinstance(exp, int):
            await revoke_token(
                session,
                jti=uuid.UUID(jti),
                expires_at=dt.datetime.fromtimestamp(exp, tz=dt.UTC),
                subject_id=subject,
                revoked_by=subject,
                reason="logout",
            )
        if raw_refresh:
            await revoke_refresh_family(session, raw_refresh=raw_refresh)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
