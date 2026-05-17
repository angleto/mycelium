"""Password hashing (argon2) and JWT tokens.

No insecure fallback: the JWT secret is mandatory (config.py).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from flow_core.config import get_settings
from flow_core.errors import AuthError
from flow_core.i18n import MessageCode

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return str(_ph.hash(password))


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _ph.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def create_access_token(*, user_id: str, extra: dict[str, Any] | None = None) -> str:
    s = get_settings()
    now = dt.datetime.now(tz=dt.UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=s.jwt_ttl_seconds)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_alg)


def decode_token(token: str) -> dict[str, Any]:
    s = get_settings()
    try:
        decoded: dict[str, Any] = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_alg])
    except jwt.PyJWTError as exc:
        raise AuthError(MessageCode.AUTH_TOKEN_INVALID) from exc
    return decoded
