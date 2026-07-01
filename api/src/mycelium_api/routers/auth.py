"""Auth router: signup, login, email verification, password reset,
logout (server-side JWT revocation). Thin adapter over mycelium_core
(ADR-0024). Enumeration-safe endpoints return a fixed status
regardless of whether the address exists.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status

from mycelium_api.deps import current_claims, current_user
from mycelium_api.schemas import (
    EmailIn,
    LoginIn,
    LoginMfaIn,
    LogoutIn,
    MeOut,
    MePatchIn,
    RefreshIn,
    ResetPasswordIn,
    SignupIn,
    SignupOut,
    TokenOut,
    VerifyEmailIn,
)
from mycelium_core.config import get_settings
from mycelium_core.db import admin_session
from mycelium_core.errors import ForbiddenError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.user import User
from mycelium_core.services.auth import (
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
from mycelium_core.services.image_validation import IMAGE_MAX_BYTES
from mycelium_core.services.users import get_user_avatar, set_user_avatar, update_profile

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=SignupOut)
async def signup_endpoint(body: SignupIn) -> SignupOut:
    # Single-user prod can disable public self-service signup
    # (MYCELIUM_ALLOW_SIGNUP=false). The gate is HTTP-only: the bootstrap
    # (`python -m mycelium_core.bootstrap_admin`) and tests call the
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
        timezone=user.timezone,
        day_start_minute=user.day_start_minute,
        language=user.language,
        is_admin=user.is_admin,
        has_avatar=user.avatar_mime is not None,
        avatar_seed=user.avatar_seed,
        avatar_bg=user.avatar_bg,
        avatar_net=user.avatar_net,
    )


@router.patch("/me", response_model=MeOut)
async def patch_me_endpoint(
    body: MePatchIn,
    user: Annotated[User, Depends(current_user)],
) -> MeOut:
    """Update the caller's reminder profile (timezone and/or day-start);
    only the fields actually sent are applied. Validated server-side. The
    users table is global (no tenant RLS), so the write goes through the
    no-tenant admin session like the other auth flows."""
    sent = body.model_fields_set
    patch: dict[str, Any] = {}
    if "timezone" in sent:
        patch["timezone"] = body.timezone
    if "day_start_minute" in sent:
        patch["day_start_minute"] = body.day_start_minute
    if "language" in sent:
        patch["language"] = body.language
    async with admin_session() as session:
        updated = await update_profile(session, user_id=user.id, **patch)
        return MeOut(
            user_id=updated.id,
            email=updated.email,
            display_name=updated.display_name,
            timezone=updated.timezone,
            day_start_minute=updated.day_start_minute,
            language=updated.language,
            is_admin=updated.is_admin,
            has_avatar=updated.avatar_mime is not None,
            avatar_seed=updated.avatar_seed,
            avatar_bg=updated.avatar_bg,
            avatar_net=updated.avatar_net,
        )


@router.post("/me/avatar", response_model=MeOut)
async def upload_avatar_endpoint(
    user: Annotated[User, Depends(current_user)],
    file: Annotated[UploadFile, File()],
    seed: Annotated[str | None, Form()] = None,
    bg: Annotated[str | None, Form()] = None,
    net: Annotated[str | None, Form()] = None,
) -> MeOut:
    """Store/replace the caller's generated avatar (PNG/JPEG) + its styling
    identity (regeneration seed + colors). Self-service: the caller writes
    their own (global) row via the no-tenant admin session. Bytes are read
    bounded to the size cap; the service decode-validates before persist."""
    data = await file.read(IMAGE_MAX_BYTES + 1)
    async with admin_session() as session:
        updated = await set_user_avatar(
            session,
            user_id=user.id,
            data=data,
            mime=file.content_type or "",
            seed=seed,
            bg=bg,
            net=net,
        )
        return MeOut(
            user_id=updated.id,
            email=updated.email,
            display_name=updated.display_name,
            timezone=updated.timezone,
            day_start_minute=updated.day_start_minute,
            language=updated.language,
            is_admin=updated.is_admin,
            has_avatar=updated.avatar_mime is not None,
            avatar_seed=updated.avatar_seed,
            avatar_bg=updated.avatar_bg,
            avatar_net=updated.avatar_net,
        )


@router.get("/me/avatar")
async def get_avatar_endpoint(
    user: Annotated[User, Depends(current_user)],
) -> Response:
    """Stream the caller's avatar bytes (or 404 when unset). ``no-store`` so a
    regenerated avatar never serves stale; the SPA also cache-busts on
    ``avatar_updated_at``."""
    async with admin_session() as session:
        avatar = await get_user_avatar(session, user_id=user.id)
    if avatar is None:
        return Response(status_code=404)
    data, mime = avatar
    return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store"})


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
