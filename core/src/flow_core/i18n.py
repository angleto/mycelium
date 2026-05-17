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
    WORKFLOW_NOT_FOUND = "workflow.not_found"
    WORKFLOW_INVALID = "workflow.invalid"
    TRANSITION_NOT_ALLOWED = "workflow.transition_not_allowed"
    DEPENDENCY_CYCLE = "dependency.cycle"
    CALENDAR_NOT_FOUND = "calendar.not_found"
    EVENT_NOT_FOUND = "event.not_found"
    EVENT_OVERLAP = "event.overlap"
    TIMER_ALREADY_RUNNING = "time.timer_already_running"
    NO_RUNNING_TIMER = "time.no_running_timer"
    TIME_ENTRY_NOT_FOUND = "time_entry.not_found"
    TIME_ENTRY_INVALID = "time_entry.invalid"
    BUDGET_NOT_FOUND = "budget.not_found"
    EMAIL_ACCOUNT_NOT_FOUND = "email.account_not_found"
    EMAIL_ACCOUNT_DUPLICATE = "email.account_duplicate"
    EMAIL_MESSAGE_NOT_FOUND = "email.message_not_found"
    EMAIL_SYNC_FAILED = "email.sync_failed"
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
        MessageCode.WORKFLOW_NOT_FOUND: "Workflow not found",
        MessageCode.WORKFLOW_INVALID: ("Invalid workflow: exactly one initial state is required"),
        MessageCode.TRANSITION_NOT_ALLOWED: ("Transition not allowed by the workflow"),
        MessageCode.DEPENDENCY_CYCLE: ("This dependency would create a cycle"),
        MessageCode.CALENDAR_NOT_FOUND: "Working calendar not found",
        MessageCode.EVENT_NOT_FOUND: "Event not found",
        MessageCode.EVENT_OVERLAP: ("Overlapping appointment for a participant (no ubiquity)"),
        MessageCode.TIMER_ALREADY_RUNNING: ("A timer is already running for this user"),
        MessageCode.NO_RUNNING_TIMER: "No running timer for this user",
        MessageCode.TIME_ENTRY_NOT_FOUND: "Time entry not found",
        MessageCode.TIME_ENTRY_INVALID: ("Invalid time entry: provide a positive interval"),
        MessageCode.BUDGET_NOT_FOUND: "Budget not found",
        MessageCode.EMAIL_ACCOUNT_NOT_FOUND: "Email account not found",
        MessageCode.EMAIL_ACCOUNT_DUPLICATE: ("An email account with this address already exists"),
        MessageCode.EMAIL_MESSAGE_NOT_FOUND: "Email message not found",
        MessageCode.EMAIL_SYNC_FAILED: "Email sync failed: {detail}",
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
