"""Message catalog (i18n-ready).

Project rule (docs/adr/0017): no hardcoded user-facing strings. Domain
errors carry a stable machine ``MessageCode`` plus parameters; adapters
render the human text via this catalog for the requested locale. Only
``en`` exists now; adding locales later is purely additive.
"""

from __future__ import annotations

import enum
from typing import Any

DEFAULT_LOCALE = "en"


class MessageCode(enum.StrEnum):
    AUTH_INVALID_CREDENTIALS = "auth.invalid_credentials"
    AUTH_MISSING_BEARER = "auth.missing_bearer"
    AUTH_TOKEN_INVALID = "auth.token_invalid"  # noqa: S105 (message code, not a secret)
    AUTH_TOKEN_NO_SUB = "auth.token_no_sub"  # noqa: S105 (message code, not a secret)
    RBAC_NO_MEMBERSHIP = "rbac.no_membership"
    RBAC_ROLE_INSUFFICIENT = "rbac.role_insufficient"
    ORG_NOT_FOUND = "org.not_found"
    CONFLICT_STALE_VERSION = "concurrency.stale_version"
    TASK_NOT_FOUND = "task.not_found"
    TAG_NOT_FOUND = "tag.not_found"
    TAG_DUPLICATE = "tag.duplicate"
    TAG_AMBIGUOUS = "tag.ambiguous"
    TAG_KIND_MISMATCH = "tag.kind_mismatch"
    DOMAIN_ERROR = "domain.error"


# locale -> code -> template. Templates use str.format named params.
_CATALOG: dict[str, dict[MessageCode, str]] = {
    "en": {
        MessageCode.AUTH_INVALID_CREDENTIALS: "Invalid credentials",
        MessageCode.AUTH_MISSING_BEARER: "Missing Authorization: Bearer header",
        MessageCode.AUTH_TOKEN_INVALID: "Invalid or expired token",
        MessageCode.AUTH_TOKEN_NO_SUB: "Token without subject",
        MessageCode.RBAC_NO_MEMBERSHIP: "Not a member of this organization",
        MessageCode.RBAC_ROLE_INSUFFICIENT: (
            "Role {current} is insufficient, requires >= {minimum}"
        ),
        MessageCode.ORG_NOT_FOUND: "Organization not found",
        MessageCode.CONFLICT_STALE_VERSION: "Stale version write",
        MessageCode.TASK_NOT_FOUND: "Task not found",
        MessageCode.TAG_NOT_FOUND: "Tag not found",
        MessageCode.TAG_DUPLICATE: "A tag with this name already exists",
        MessageCode.TAG_AMBIGUOUS: "Ambiguous tag name: {name}",
        MessageCode.TAG_KIND_MISMATCH: "Tag is not of the expected kind",
        MessageCode.DOMAIN_ERROR: "Domain error",
    },
}


def render(code: MessageCode, locale: str = DEFAULT_LOCALE, /, **params: Any) -> str:
    table = _CATALOG.get(locale) or _CATALOG[DEFAULT_LOCALE]
    template = table.get(code) or _CATALOG[DEFAULT_LOCALE].get(code) or code.value
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template
