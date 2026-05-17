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


class AuthError(DomainError):
    """Invalid credentials or missing/expired token (401)."""


class ForbiddenError(DomainError):
    """Insufficient role in the current org context (403)."""
