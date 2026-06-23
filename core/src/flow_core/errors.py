"""Domain exceptions.

Raised by the service layer and mapped by adapters (api/mcp) to error
codes. Each error carries a stable ``MessageCode`` plus parameters; no
hardcoded user-facing text here (docs/adr/0017). ``ConflictError`` maps
to HTTP 409 (optimistic concurrency, docs/adr/0002).
"""

from __future__ import annotations

from typing import Any

from flow_core.i18n import DEFAULT_LOCALE, MessageCode, render


class DomainError(Exception):
    """Base of all domain errors. Holds a machine code + params."""

    def __init__(self, code: MessageCode, /, **params: Any) -> None:
        self.code = code
        self.params = params
        super().__init__(render(code, DEFAULT_LOCALE, **params))


class NotFoundError(DomainError):
    """Entity missing or not visible in the current tenant context."""


class ConflictError(DomainError):
    """Write on a stale version: optimistic concurrency (409)."""


class UnprocessableError(DomainError):
    """Well-formed request the server cannot act on (HTTP 422). The base
    gate passed but the unified diff is malformed or does not apply to the
    live body; re-downloading the body would not help (unlike a stale
    ConflictError). See services/text_patch.py."""


class AuthError(DomainError):
    """Invalid credentials or missing/expired token (401)."""


class ForbiddenError(DomainError):
    """Insufficient role in the current org context (403)."""


class LockedError(DomainError):
    """Account temporarily locked (repeated failed logins): HTTP 423.
    Distinct from AuthError so clients can stop retrying (ADR-0024)."""


class QuotaExceededError(DomainError):
    """Rate/volume quota exhausted: HTTP 429. The event-bus anti-runaway
    cap on an actor's emissions (ADR-0036 / c19b5489)."""


def jsonable_params(params: dict[str, Any]) -> dict[str, Any]:
    """Coerce ``DomainError.params`` to JSON-safe values so an adapter
    can embed them in a structured error envelope without the serializer
    choking on a UUID/Decimal/datetime. Lists/tuples and dicts are
    coerced element-wise; anything else falls back to ``str()``."""

    def _coerce(v: Any) -> Any:
        if v is None or isinstance(v, (str, int, float, bool)):
            return v
        if isinstance(v, (list, tuple)):
            return [_coerce(x) for x in v]
        if isinstance(v, dict):
            return {str(k): _coerce(x) for k, x in v.items()}
        return str(v)

    return {k: _coerce(v) for k, v in params.items()}
