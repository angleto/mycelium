"""Signup, login, org resolution, and auth hardening (W1b).

Pre-tenant (admin_session): ``users`` is not org-scoped and
org+membership creation goes through the SECURITY DEFINER function
``provision_organization`` (docs/adr/0015). Email verification,
password reset and JWT revocation are ported from bitvision_phoenix
(same stack) and adapted to Flow's DomainError + i18n pattern
(ADR-0024). One-shot tokens persist only a SHA-256 hash; the plaintext
leaves via the SystemMailer seam and is never stored.

Vocabulary: the core/domain term is ``org`` (RLS, ADR-0015,
unchanged). The user-facing rename to "workspace" lives in the
adapters.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.config import get_settings
from flow_core.errors import AuthError, DomainError, ForbiddenError
from flow_core.i18n import MessageCode
from flow_core.models.auth_tokens import (
    EmailVerificationToken,
    PasswordResetToken,
    RevokedToken,
)
from flow_core.models.user import User
from flow_core.security import create_access_token, hash_password, verify_password
from flow_core.services.mailer import OutboundEmail, get_mailer


@dataclass(frozen=True, slots=True)
class SignupResult:
    user_id: uuid.UUID
    org_id: uuid.UUID
    # None when email verification is required: the user has no usable
    # session until they verify (mirrors bitvision_phoenix).
    token: str | None
    email_verification_required: bool


@dataclass(frozen=True, slots=True)
class OrgMembership:
    id: uuid.UUID
    name: str
    role: str


def _hash_token(raw: str) -> str:
    """SHA-256 hex of the token; only this is persisted."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _provision_org(session: AsyncSession, *, name: str, user_id: uuid.UUID) -> uuid.UUID:
    result = await session.execute(
        text("SELECT provision_organization(:n, :u)"),
        {"n": name, "u": str(user_id)},
    )
    return uuid.UUID(str(result.scalar_one()))


async def _issue_verification(session: AsyncSession, *, user: User) -> None:
    s = get_settings()
    raw = secrets.token_urlsafe(32)
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=s.email_verification_ttl_seconds)
    session.add(
        EmailVerificationToken(
            user_id=user.id, token_hash=_hash_token(raw), expires_at=expires
        )
    )
    await session.flush()
    url = f"{s.frontend_base_url.rstrip('/')}/verify-email?token={raw}"
    await get_mailer().send(
        OutboundEmail(
            to=user.email,
            subject="Verify your email",
            body=f"Confirm your email: {url}",
        )
    )


async def signup(
    session: AsyncSession, *, email: str, password: str, org_name: str
) -> SignupResult:
    user = User(email=email.lower(), password_hash=hash_password(password))
    session.add(user)
    await session.flush()  # populate user.id
    org_id = await _provision_org(session, name=org_name, user_id=user.id)
    require_verify = get_settings().require_email_verification
    token: str | None = None
    if require_verify:
        await _issue_verification(session, user=user)
    else:
        token = create_access_token(user_id=str(user.id))
    return SignupResult(
        user_id=user.id,
        org_id=org_id,
        token=token,
        email_verification_required=require_verify,
    )


async def create_org_for_user(
    session: AsyncSession, *, user_id: uuid.UUID, name: str
) -> uuid.UUID:
    """Create an additional org for an existing authenticated user (they
    become its owner). Powers in-app workspace creation, no re-auth."""
    return await _provision_org(session, name=name, user_id=user_id)


async def list_user_orgs(
    session: AsyncSession, *, user_id: uuid.UUID
) -> list[OrgMembership]:
    """Orgs the user belongs to (pre-tenant; for the in-app switcher).
    Crosses the RLS boundary only via the SECURITY DEFINER
    ``list_user_organizations`` function (migration 0014)."""
    rows = await session.execute(
        text("SELECT org_id, name, role FROM list_user_organizations(:u) ORDER BY name"),
        {"u": str(user_id)},
    )
    return [
        OrgMembership(id=uuid.UUID(str(r.org_id)), name=str(r.name), role=str(r.role))
        for r in rows
    ]


async def get_user(session: AsyncSession, *, user_id: uuid.UUID) -> User:
    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise AuthError(MessageCode.AUTH_TOKEN_NO_SUB)
    return user


def _check_password(user: User | None, password: str) -> User:
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        raise AuthError(MessageCode.AUTH_INVALID_CREDENTIALS)
    return user


def _check_verified(user: User) -> None:
    if get_settings().require_email_verification and user.email_verified_at is None:
        # 403 (ForbiddenError), not 401: distinguish "verify your email"
        # from "wrong password" so the client can react correctly.
        raise ForbiddenError(MessageCode.AUTH_EMAIL_NOT_VERIFIED)


async def login(session: AsyncSession, *, email: str, password: str) -> str:
    result = await session.execute(select(User).where(User.email == email.lower()))
    user = _check_password(result.scalar_one_or_none(), password)
    _check_verified(user)
    if user.mfa_enabled_at is not None:
        # 401 mfa_required: the SPA pivots to /auth/login-mfa.
        raise AuthError(MessageCode.AUTH_MFA_REQUIRED)
    return create_access_token(user_id=str(user.id))


async def login_mfa(
    session: AsyncSession, *, email: str, password: str, totp_code: str
) -> str:
    """Combined password + TOTP/backup-code login (used once MFA is
    active). Consuming a backup code is persisted."""
    from flow_core.services.mfa import verify_mfa_code

    result = await session.execute(select(User).where(User.email == email.lower()))
    user = _check_password(result.scalar_one_or_none(), password)
    _check_verified(user)
    if user.mfa_enabled_at is None:
        raise DomainError(MessageCode.AUTH_MFA_NOT_ENABLED)
    if not verify_mfa_code(user, totp_code):
        raise AuthError(MessageCode.AUTH_INVALID_TOTP)
    await session.flush()  # persist backup-code consumption, if any
    return create_access_token(user_id=str(user.id))


# ---- email verification -------------------------------------------------


async def verify_email(session: AsyncSession, *, raw_token: str) -> str:
    """Consume a verification token, mark the email verified, return a
    fresh access token. Invalid/expired/used all collapse to one error
    (no oracle on token existence)."""
    now = dt.datetime.now(dt.UTC)
    row = (
        await session.execute(
            select(EmailVerificationToken).where(
                EmailVerificationToken.token_hash == _hash_token(raw_token)
            )
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None or row.expires_at < now:
        raise AuthError(MessageCode.AUTH_VERIFICATION_TOKEN_INVALID)
    user = (
        await session.execute(select(User).where(User.id == row.user_id))
    ).scalar_one_or_none()
    if user is None:
        raise AuthError(MessageCode.AUTH_VERIFICATION_TOKEN_INVALID)
    row.used_at = now
    if user.email_verified_at is None:
        user.email_verified_at = now
    await session.flush()
    return create_access_token(user_id=str(user.id))


async def resend_verification(session: AsyncSession, *, email: str) -> None:
    """Mint a fresh verification token. Always succeeds silently
    (account-enumeration avoidance); the outcome is visible only via the
    mailbox."""
    user = (
        await session.execute(select(User).where(User.email == email.lower()))
    ).scalar_one_or_none()
    if user is not None and user.email_verified_at is None:
        await _issue_verification(session, user=user)


# ---- password reset -----------------------------------------------------


async def request_password_reset(
    session: AsyncSession, *, email: str, ip: str | None = None
) -> None:
    """Issue a reset token and email it. Always silent regardless of
    whether the address exists (enumeration-safe)."""
    s = get_settings()
    user = (
        await session.execute(select(User).where(User.email == email.lower()))
    ).scalar_one_or_none()
    if user is None:
        return
    raw = secrets.token_urlsafe(32)
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=s.password_reset_ttl_minutes)
    session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(raw),
            expires_at=expires,
            requested_ip=ip,
        )
    )
    await session.flush()
    url = f"{s.frontend_base_url.rstrip('/')}/reset-password?token={raw}"
    await get_mailer().send(
        OutboundEmail(
            to=user.email,
            subject="Reset your password",
            body=f"Reset your password: {url} (valid {s.password_reset_ttl_minutes} min)",
        )
    )


async def reset_password(
    session: AsyncSession, *, raw_token: str, new_password: str
) -> None:
    """Consume a reset token, set the new password, and invalidate every
    other outstanding reset token for that user (defence in depth)."""
    now = dt.datetime.now(dt.UTC)
    row = (
        await session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == _hash_token(raw_token)
            )
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise AuthError(MessageCode.AUTH_RESET_TOKEN_INVALID)
    user = (
        await session.execute(select(User).where(User.id == row.user_id))
    ).scalar_one_or_none()
    if user is None:
        raise AuthError(MessageCode.AUTH_RESET_TOKEN_INVALID)
    user.password_hash = hash_password(new_password)
    row.used_at = now
    siblings = (
        (
            await session.execute(
                select(PasswordResetToken).where(
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.used_at.is_(None),
                    PasswordResetToken.id != row.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for sib in siblings:
        sib.used_at = now
    await session.flush()


# ---- JWT revocation -----------------------------------------------------


async def revoke_token(
    session: AsyncSession,
    *,
    jti: uuid.UUID,
    expires_at: dt.datetime,
    subject_id: uuid.UUID | None,
    revoked_by: uuid.UUID | None,
    reason: str | None = None,
) -> None:
    """Revoke a JWT by its ``jti``. Idempotent (ON CONFLICT DO
    NOTHING): re-revoking the same token is a no-op."""
    stmt = (
        pg_insert(RevokedToken)
        .values(
            jti=jti,
            revoked_at=dt.datetime.now(dt.UTC),
            expires_at=expires_at,
            revoked_by=revoked_by,
            subject_id=subject_id,
            reason=reason,
        )
        .on_conflict_do_nothing(index_elements=[RevokedToken.jti])
    )
    await session.execute(stmt)
    await session.flush()


async def assert_token_not_revoked(session: AsyncSession, *, jti: uuid.UUID) -> None:
    """Raise if the JWT's ``jti`` has been revoked."""
    found = (
        await session.execute(select(RevokedToken.jti).where(RevokedToken.jti == jti))
    ).scalar_one_or_none()
    if found is not None:
        raise AuthError(MessageCode.AUTH_TOKEN_REVOKED)
