"""TOTP MFA + backup codes (W1b; ported from bitvision_phoenix,
adapted to Flow's DomainError + i18n; ADR-0024).

Enrolment: ``setup`` mints a pending secret; ``activate`` verifies a
code, stamps ``mfa_enabled_at`` and returns 10 one-shot backup codes
(stored as argon2 hashes). ``/auth/login`` returns 401
``auth.mfa_required`` when MFA is active; the SPA pivots to
``/auth/login-mfa``. QR rendering is an adapter concern (api), not
here.
"""

from __future__ import annotations

import datetime as dt
import secrets
import string
from dataclasses import dataclass

import pyotp

from flow_core.config import get_settings
from flow_core.errors import AuthError, ConflictError, DomainError
from flow_core.i18n import MessageCode
from flow_core.models.user import User
from flow_core.security import hash_password, verify_password

_ALPHABET = string.ascii_uppercase + string.digits
_BACKUP_LEN = 8
_BACKUP_COUNT = 10
# TOTP verification window in 30s steps; 1 absorbs typical clock skew.
_TOTP_WINDOW = 1


def _gen_backup_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_BACKUP_LEN))


def verify_totp(secret: str | None, code: str) -> bool:
    if not secret:
        return False
    code = (code or "").strip()
    if not code:
        return False
    return bool(pyotp.TOTP(secret).verify(code, valid_window=_TOTP_WINDOW))


def _consume_backup_code(user: User, code: str) -> bool:
    hashes = user.backup_codes_hash or []
    if not hashes:
        return False
    normalized = code.strip().upper()
    for i, h in enumerate(hashes):
        if verify_password(normalized, h):
            # Reassign a new list: SQLAlchemy ARRAY mutation tracking
            # does not see in-place edits.
            user.backup_codes_hash = hashes[:i] + hashes[i + 1 :]
            return True
    return False


def verify_mfa_code(user: User, code: str) -> bool:
    """A valid TOTP, or a backup code (consumed on match). The caller
    must flush/commit so a consumed backup code is persisted."""
    if verify_totp(user.mfa_secret, code):
        return True
    return _consume_backup_code(user, code)


@dataclass(frozen=True, slots=True)
class MfaStatus:
    enabled: bool
    pending: bool
    enabled_at: dt.datetime | None
    backup_codes_remaining: int


@dataclass(frozen=True, slots=True)
class MfaSetup:
    provisioning_uri: str
    secret: str


@dataclass(frozen=True, slots=True)
class MfaActivated:
    backup_codes: list[str]
    enabled_at: dt.datetime


def status(user: User) -> MfaStatus:
    return MfaStatus(
        enabled=user.mfa_enabled_at is not None,
        pending=user.mfa_secret is not None and user.mfa_enabled_at is None,
        enabled_at=user.mfa_enabled_at,
        backup_codes_remaining=len(user.backup_codes_hash or []),
    )


def setup(*, user: User) -> MfaSetup:
    """Start (or restart) enrolment. Refuses if MFA is already active
    (disable first). Mutates the ORM-attached ``user``; the caller's
    session context flushes/commits."""
    if user.mfa_enabled_at is not None:
        raise ConflictError(MessageCode.AUTH_MFA_ALREADY_ENABLED)
    secret = pyotp.random_base32()
    user.mfa_secret = secret
    user.backup_codes_hash = None
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=user.email, issuer_name=get_settings().mfa_issuer
    )
    return MfaSetup(provisioning_uri=uri, secret=secret)


def activate(*, user: User, totp_code: str) -> MfaActivated:
    if user.mfa_enabled_at is not None:
        raise ConflictError(MessageCode.AUTH_MFA_ALREADY_ENABLED)
    if user.mfa_secret is None:
        raise DomainError(MessageCode.AUTH_MFA_SETUP_REQUIRED)
    if not verify_totp(user.mfa_secret, totp_code):
        raise AuthError(MessageCode.AUTH_INVALID_TOTP)
    plain = [_gen_backup_code() for _ in range(_BACKUP_COUNT)]
    user.backup_codes_hash = [hash_password(c) for c in plain]
    enabled_at = dt.datetime.now(dt.UTC)
    user.mfa_enabled_at = enabled_at
    return MfaActivated(backup_codes=plain, enabled_at=enabled_at)


def disable(*, user: User, code: str) -> None:
    """Disable MFA. Requires a currently-valid TOTP or backup code so a
    hijacked session cannot silently lower the account's security."""
    if user.mfa_enabled_at is None:
        raise DomainError(MessageCode.AUTH_MFA_NOT_ENABLED)
    if not verify_mfa_code(user, code):
        raise AuthError(MessageCode.AUTH_INVALID_TOTP)
    user.mfa_secret = None
    user.mfa_enabled_at = None
    user.backup_codes_hash = None
