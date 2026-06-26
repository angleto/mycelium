"""Signup, login, org resolution, and auth hardening (W1b).

Pre-tenant (admin_session): ``users`` is not org-scoped and
org+membership creation goes through the SECURITY DEFINER function
``provision_organization`` (docs/adr/0015). Email verification,
password reset and JWT revocation are ported from bitvision_phoenix
(same stack) and adapted to Mycelium's DomainError + i18n pattern
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

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session
from mycelium_core.errors import (
    AuthError,
    DomainError,
    ForbiddenError,
    LockedError,
    NotFoundError,
)
from mycelium_core.i18n import MessageCode
from mycelium_core.models.auth_tokens import (
    EmailVerificationToken,
    PasswordResetToken,
    RefreshToken,
    RevokedToken,
)
from mycelium_core.models.user import User
from mycelium_core.security import create_access_token, hash_password, verify_password
from mycelium_core.services import actors as actors_svc
from mycelium_core.services.mailer import OutboundEmail, get_mailer

# Cosmetic discriminator on the raw refresh token. Lookup is by SHA-256
# hash, so refresh tokens minted under the old "flow_rt_" brand keep
# validating after the rename; only newly minted tokens carry this prefix.
_REFRESH_PREFIX = "mycelium_rt_"


@dataclass(frozen=True, slots=True)
class TokenPair:
    """Access JWT + raw refresh token returned by login / refresh."""

    access_token: str
    refresh_token: str


@dataclass(frozen=True, slots=True)
class SignupResult:
    user_id: uuid.UUID
    org_id: uuid.UUID
    # None when email verification is required: the user has no usable
    # session until they verify (mirrors bitvision_phoenix).
    token: str | None
    refresh_token: str | None
    email_verification_required: bool


@dataclass(frozen=True, slots=True)
class OrgMembership:
    id: uuid.UUID
    name: str
    role: str
    status: str


def _hash_token(raw: str) -> str:
    """SHA-256 hex of the token; only this is persisted."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _token_for(user: User) -> str:
    """Access token carrying identity claims (email, is_admin) so the
    SPA can render the admin affordances without an extra round-trip;
    /auth/me remains the canonical, server-checked source."""
    return create_access_token(
        user_id=str(user.id),
        extra={"email": user.email, "is_admin": user.is_admin},
    )


async def _mint_refresh_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
) -> str:
    """Persist a fresh refresh row in ``family_id`` and return the
    plaintext (only the SHA-256 hash hits the DB)."""
    raw = _REFRESH_PREFIX + secrets.token_urlsafe(32)
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(
        seconds=get_settings().refresh_token_ttl_seconds
    )
    session.add(
        RefreshToken(
            family_id=family_id,
            user_id=user_id,
            token_hash=_hash_token(raw),
            expires_at=expires,
        )
    )
    await session.flush()
    return raw


async def _issue_login_pair(session: AsyncSession, *, user: User) -> TokenPair:
    """Mint a fresh access JWT plus a brand-new refresh family. Used
    by login / login_mfa / verify_email — anywhere a brand-new session
    begins (NOT by /auth/refresh, which rotates inside an existing
    family)."""
    access = _token_for(user)
    refresh = await _mint_refresh_token(session, user_id=user.id, family_id=uuid.uuid4())
    return TokenPair(access_token=access, refresh_token=refresh)


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
        EmailVerificationToken(user_id=user.id, token_hash=_hash_token(raw), expires_at=expires)
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
    # Mint the actor handle BEFORE provisioning the org. provision_organization
    # inserts the membership, which fires trg_sync_identity_on_membership_insert
    # (migration 0085) — that trigger only creates the identity row when
    # users.handle is non-empty. Without this call the user would be visible
    # in the assignee picker (it sources users.handle) but unassignable
    # (lookup_by_handle queries the identities table) → DomainError on
    # self-assign.
    await actors_svc.mint_user_handle(session, user_id=user.id, seed=email)
    org_id = await _provision_org(session, name=org_name, user_id=user.id)
    require_verify = get_settings().require_email_verification
    token: str | None = None
    refresh_token: str | None = None
    if require_verify:
        await _issue_verification(session, user=user)
    else:
        pair = await _issue_login_pair(session, user=user)
        token = pair.access_token
        refresh_token = pair.refresh_token
    return SignupResult(
        user_id=user.id,
        org_id=org_id,
        token=token,
        refresh_token=refresh_token,
        email_verification_required=require_verify,
    )


async def create_org_for_user(session: AsyncSession, *, user_id: uuid.UUID, name: str) -> uuid.UUID:
    """Create an additional org for an existing authenticated user (they
    become its owner). Powers in-app workspace creation, no re-auth."""
    # Defensive: pre-Stage-A users may still carry the empty-string
    # handle sentinel. Ensure it before the membership insert so the
    # identity-sync trigger has a real handle to copy.
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
    if not user.handle:
        await actors_svc.mint_user_handle(session, user_id=user_id, seed=user.email)
    return await _provision_org(session, name=name, user_id=user_id)


async def list_user_orgs(session: AsyncSession, *, user_id: uuid.UUID) -> list[OrgMembership]:
    """Orgs the user belongs to (pre-tenant; for the in-app switcher).
    Crosses the RLS boundary only via the SECURITY DEFINER
    ``list_user_organizations`` function (migration 0014)."""
    rows = await session.execute(
        text("SELECT org_id, name, role, status FROM list_user_organizations(:u) ORDER BY name"),
        {"u": str(user_id)},
    )
    return [
        OrgMembership(
            id=uuid.UUID(str(r.org_id)),
            name=str(r.name),
            role=str(r.role),
            status=str(r.status),
        )
        for r in rows
    ]


async def delete_org_for_user(
    session: AsyncSession, *, user_id: uuid.UUID, org_id: uuid.UUID
) -> None:
    """Hard-delete a workspace (owner only; refuses the user's sole
    workspace). The org-scoped data goes with it via ON DELETE CASCADE.
    Crosses the RLS boundary only through the SECURITY DEFINER
    ``delete_organization`` function (migration 0019), which re-checks
    both preconditions atomically (defense in depth)."""
    orgs = await list_user_orgs(session, user_id=user_id)
    target = next((o for o in orgs if o.id == org_id), None)
    if target is None:
        raise NotFoundError(MessageCode.ORG_NOT_FOUND)
    if target.role != "owner":
        raise ForbiddenError(MessageCode.WORKSPACE_NOT_OWNER)
    if len(orgs) <= 1:
        raise DomainError(MessageCode.WORKSPACE_SOLE)
    await session.execute(
        text("SELECT delete_organization(:o, :u)"),
        {"o": str(org_id), "u": str(user_id)},
    )


async def set_workspace_status(
    session: AsyncSession, *, user_id: uuid.UUID, org_id: uuid.UUID, status: str
) -> None:
    """Archive / unarchive a workspace (owner or admin). Archived
    workspaces are hidden from the switcher by default but stay usable.
    Routed through the SECURITY DEFINER ``set_organization_status``
    function (migration 0019)."""
    if status not in ("active", "archived"):
        raise DomainError(MessageCode.DOMAIN_ERROR)
    orgs = await list_user_orgs(session, user_id=user_id)
    target = next((o for o in orgs if o.id == org_id), None)
    if target is None:
        raise NotFoundError(MessageCode.ORG_NOT_FOUND)
    if target.role not in ("owner", "admin"):
        raise ForbiddenError(MessageCode.RBAC_ROLE_INSUFFICIENT)
    await session.execute(
        text("SELECT set_organization_status(:o, :u, :s)"),
        {"o": str(org_id), "u": str(user_id), "s": status},
    )


async def get_user(session: AsyncSession, *, user_id: uuid.UUID) -> User:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise AuthError(MessageCode.AUTH_TOKEN_NO_SUB)
    return user


def _check_verified(user: User) -> None:
    if get_settings().require_email_verification and user.email_verified_at is None:
        # 403 (ForbiddenError), not 401: distinguish "verify your email"
        # from "wrong password" so the client can react correctly.
        raise ForbiddenError(MessageCode.AUTH_EMAIL_NOT_VERIFIED)


def _is_locked(user: User) -> bool:
    return user.locked_until is not None and user.locked_until > dt.datetime.now(dt.UTC)


async def _record_login_failure(user_id: uuid.UUID) -> None:
    """Persist a failed attempt in its OWN transaction so it survives
    the rejected login's rollback (admin_session rolls back on the
    raised AuthError). Locks the account past the threshold."""
    s = get_settings()
    async with admin_session() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None:
            return
        user.failed_login_count += 1
        if user.failed_login_count >= s.login_max_failures:
            user.locked_until = dt.datetime.now(dt.UTC) + dt.timedelta(
                seconds=s.login_lockout_seconds
            )
            user.failed_login_count = 0


async def _authenticate(session: AsyncSession, *, email: str, password: str) -> User:
    """Password auth with DB-backed lockout. On success the failure
    counters are reset (committed with the caller's session)."""
    user = (
        await session.execute(select(User).where(User.email == email.lower()))
    ).scalar_one_or_none()
    if user is None:
        raise AuthError(MessageCode.AUTH_INVALID_CREDENTIALS)
    if _is_locked(user):
        raise LockedError(MessageCode.AUTH_ACCOUNT_LOCKED)
    if not user.is_active or not verify_password(password, user.password_hash):
        await _record_login_failure(user.id)
        raise AuthError(MessageCode.AUTH_INVALID_CREDENTIALS)
    if user.failed_login_count or user.locked_until is not None:
        user.failed_login_count = 0
        user.locked_until = None
    _check_verified(user)
    return user


async def login(session: AsyncSession, *, email: str, password: str) -> TokenPair:
    user = await _authenticate(session, email=email, password=password)
    if user.mfa_enabled_at is not None:
        # 401 mfa_required: the SPA pivots to /auth/login-mfa.
        raise AuthError(MessageCode.AUTH_MFA_REQUIRED)
    return await _issue_login_pair(session, user=user)


async def login_mfa(
    session: AsyncSession, *, email: str, password: str, totp_code: str
) -> TokenPair:
    """Combined password + TOTP/backup-code login (used once MFA is
    active). Consuming a backup code is persisted."""
    from mycelium_core.services.mfa import verify_mfa_code

    user = await _authenticate(session, email=email, password=password)
    if user.mfa_enabled_at is None:
        raise DomainError(MessageCode.AUTH_MFA_NOT_ENABLED)
    if not verify_mfa_code(user, totp_code):
        raise AuthError(MessageCode.AUTH_INVALID_TOTP)
    await session.flush()  # persist backup-code consumption, if any
    return await _issue_login_pair(session, user=user)


# ---- email verification -------------------------------------------------


async def verify_email(session: AsyncSession, *, raw_token: str) -> TokenPair:
    """Consume a verification token, mark the email verified, return a
    fresh token pair. Invalid/expired/used all collapse to one error
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
    user = (await session.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None:
        raise AuthError(MessageCode.AUTH_VERIFICATION_TOKEN_INVALID)
    row.used_at = now
    if user.email_verified_at is None:
        user.email_verified_at = now
    await session.flush()
    return await _issue_login_pair(session, user=user)


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


async def reset_password(session: AsyncSession, *, raw_token: str, new_password: str) -> None:
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
    user = (await session.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
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


# ---- Refresh-token rotation ---------------------------------------------


async def _revoke_family(session: AsyncSession, *, family_id: uuid.UUID) -> None:
    """Mark every still-active row in ``family_id`` revoked. Used on
    logout AND on reuse detection (token-theft signal)."""
    now = dt.datetime.now(dt.UTC)
    rows = (
        (
            await session.execute(
                select(RefreshToken).where(
                    RefreshToken.family_id == family_id,
                    RefreshToken.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        r.revoked_at = now
    await session.flush()


async def refresh_session(session: AsyncSession, *, raw_refresh: str) -> TokenPair:
    """Rotate a refresh token: validate, mark the presented row used,
    mint a successor inside the same family, return the new pair.

    Failure modes (all collapse to ``AUTH_TOKEN_INVALID`` so the SPA
    pivots to /login uniformly):

    - unknown / malformed token
    - expired
    - revoked (family was revoked by logout or by an earlier reuse)
    - **reuse**: the row already has ``used_at`` set. This is the
      theft signal: revoke every active sibling in the family so the
      attacker's stolen branch loses access alongside the legitimate
      one.
    """
    now = dt.datetime.now(dt.UTC)
    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == _hash_token(raw_refresh))
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        raise AuthError(MessageCode.AUTH_TOKEN_INVALID)
    if row.used_at is not None:
        # Theft signal: the rotation already happened, yet this row
        # is being presented again. Persist the family revocation in
        # its OWN transaction so the side-effect survives the
        # AuthError below (the caller's transaction rolls back on
        # raise, which would otherwise lose the security-critical
        # revoke).
        family_id = row.family_id
        async with admin_session() as oob:
            await _revoke_family(oob, family_id=family_id)
        raise AuthError(MessageCode.AUTH_TOKEN_INVALID)
    user = (await session.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None:
        raise AuthError(MessageCode.AUTH_TOKEN_INVALID)
    # Refuse to keep a session alive for a locked / unverified account
    # (mirrors the login path's preconditions).
    _check_verified(user)
    if _is_locked(user):
        raise LockedError(MessageCode.AUTH_ACCOUNT_LOCKED)

    row.used_at = now
    new_raw = await _mint_refresh_token(session, user_id=user.id, family_id=row.family_id)
    successor = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == _hash_token(new_raw))
        )
    ).scalar_one()
    row.replaced_by_id = successor.id
    await session.flush()
    return TokenPair(access_token=_token_for(user), refresh_token=new_raw)


async def revoke_refresh_family(session: AsyncSession, *, raw_refresh: str) -> None:
    """Revoke the whole family that owns ``raw_refresh`` (logout).
    Silent on unknown tokens — a logout request should never leak
    whether a credential was valid."""
    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == _hash_token(raw_refresh))
        )
    ).scalar_one_or_none()
    if row is None:
        return
    await _revoke_family(session, family_id=row.family_id)
