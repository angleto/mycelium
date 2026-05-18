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
    MeOut,
    ResetPasswordIn,
    SignupIn,
    SignupOut,
    TokenOut,
    VerifyEmailIn,
)
from flow_core.db import admin_session
from flow_core.models.user import User
from flow_core.services.auth import (
    login,
    login_mfa,
    request_password_reset,
    resend_verification,
    reset_password,
    revoke_token,
    signup,
    verify_email,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=SignupOut)
async def signup_endpoint(body: SignupIn) -> SignupOut:
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
        email_verification_required=result.email_verification_required,
    )


@router.post("/login", response_model=TokenOut)
async def login_endpoint(body: LoginIn) -> TokenOut:
    async with admin_session() as session:
        token = await login(session, email=body.email, password=body.password)
    return TokenOut(token=token)


@router.post("/login-mfa", response_model=TokenOut)
async def login_mfa_endpoint(body: LoginMfaIn) -> TokenOut:
    """Combined password + TOTP/backup-code login. Use once /auth/login
    has answered 401 auth.mfa_required."""
    async with admin_session() as session:
        token = await login_mfa(
            session,
            email=body.email,
            password=body.password,
            totp_code=body.totp_code,
        )
    return TokenOut(token=token)


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
        token = await verify_email(session, raw_token=body.token)
    return TokenOut(token=token)


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
) -> Response:
    """Real server-side logout: revoke the current token's jti so it
    cannot be reused even though JWTs are stateless."""
    jti = claims.get("jti")
    sub = claims.get("sub")
    exp = claims.get("exp")
    if isinstance(jti, str) and isinstance(exp, int):
        subject = uuid.UUID(sub) if isinstance(sub, str) else None
        async with admin_session() as session:
            await revoke_token(
                session,
                jti=uuid.UUID(jti),
                expires_at=dt.datetime.fromtimestamp(exp, tz=dt.UTC),
                subject_id=subject,
                revoked_by=subject,
                reason="logout",
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
