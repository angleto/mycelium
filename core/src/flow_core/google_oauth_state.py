"""Signed OAuth state token (epic #125 P1).

The state shuttled through Google's OAuth flow must bind the
authenticated user/org back to the callback (which arrives without our
auth context) and resist CSRF. The token is self-contained: a base64url
JSON payload + HMAC-SHA256 over it, keyed by ``FLOW_SECRET_KEY``. No DB
or Redis hit, but tamper-evident: the callback verifies the signature
and the freshness window before doing anything.

Format (compact, URL-safe):
    <b64url(payload_json)>.<b64url(hmac)>

The payload carries: user_id, org_id, scope, nonce, exp (unix seconds).
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import Any

from flow_core.config import get_settings
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode

# State is single-use-ish (the user just clicks once); 10 minutes is the
# canonical window across OAuth providers. Short on purpose: a stale
# state means the user got distracted; they restart the flow.
DEFAULT_TTL_SECONDS = 600


@dataclasses.dataclass(frozen=True)
class OAuthState:
    user_id: uuid.UUID
    org_id: uuid.UUID
    scope: str  # gmail | calendar | both
    nonce: str
    exp: int  # unix seconds (UTC)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload: bytes, key: bytes) -> bytes:
    return hmac.new(key, payload, hashlib.sha256).digest()


def issue_state(
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    scope: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Mint a signed state token for the OAuth start request."""
    payload: dict[str, Any] = {
        "u": str(user_id),
        "o": str(org_id),
        "s": scope,
        "n": secrets.token_urlsafe(12),
        "e": int(time.time()) + int(ttl_seconds),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = _sign(raw, get_settings().secret_key.encode("utf-8"))
    return f"{_b64url_encode(raw)}.{_b64url_encode(sig)}"


def verify_state(token: str) -> OAuthState:
    """Verify the signature, the freshness window, and unpack the
    payload. Raises ``DomainError(OAUTH_STATE_INVALID)`` on any failure
    (single error code: do not leak whether the cause was a bad
    signature, a malformed token, or an expired window)."""
    try:
        body_b64, sig_b64 = token.split(".", 1)
        raw = _b64url_decode(body_b64)
        sig = _b64url_decode(sig_b64)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DomainError(MessageCode.OAUTH_STATE_INVALID) from exc
    expected = _sign(raw, get_settings().secret_key.encode("utf-8"))
    if not hmac.compare_digest(sig, expected):
        raise DomainError(MessageCode.OAUTH_STATE_INVALID)
    try:
        payload = json.loads(raw.decode("utf-8"))
        user_id = uuid.UUID(payload["u"])
        org_id = uuid.UUID(payload["o"])
        scope = str(payload["s"])
        nonce = str(payload["n"])
        exp = int(payload["e"])
    except (KeyError, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise DomainError(MessageCode.OAUTH_STATE_INVALID) from exc
    if exp <= int(time.time()):
        raise DomainError(MessageCode.OAUTH_STATE_INVALID)
    return OAuthState(user_id=user_id, org_id=org_id, scope=scope, nonce=nonce, exp=exp)
