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
    AUTH_EMAIL_ALREADY_REGISTERED = "auth.email_already_registered"
    AUTH_EMAIL_NOT_VERIFIED = "auth.email_not_verified"
    AUTH_ACCOUNT_LOCKED = "auth.account_locked"
    AUTH_MFA_REQUIRED = "auth.mfa_required"
    AUTH_MFA_ENROLL_REQUIRED = "auth.mfa_enroll_required"
    AUTH_MFA_ALREADY_ENABLED = "auth.mfa_already_enabled"
    AUTH_MFA_NOT_ENABLED = "auth.mfa_not_enabled"
    AUTH_MFA_SETUP_REQUIRED = "auth.mfa_setup_required"
    AUTH_INVALID_TOTP = "auth.invalid_totp"
    AUTH_TOKEN_REVOKED = "auth.token_revoked"  # noqa: S105 (message code, not a secret)
    AUTH_RESET_TOKEN_INVALID = "auth.reset_token_invalid"  # noqa: S105 (code, not a secret)
    AUTH_VERIFICATION_TOKEN_INVALID = "auth.verification_token_invalid"  # noqa: S105 (code, not a secret)
    AUTH_SIGNUP_DISABLED = "auth.signup_disabled"
    RBAC_NO_MEMBERSHIP = "rbac.no_membership"
    RBAC_ROLE_INSUFFICIENT = "rbac.role_insufficient"
    MEMBER_NOT_FOUND = "member.not_found"
    MEMBER_LAST_OWNER = "member.last_owner"
    MEMBER_ROLE_INVALID = "member.role_invalid"
    ADMIN_REQUIRED = "admin.required"
    ADMIN_SELF_GUARD = "admin.self_guard"
    USER_NOT_FOUND = "user.not_found"
    ORG_NOT_FOUND = "org.not_found"
    CONFLICT_STALE_VERSION = "concurrency.stale_version"
    TASK_NOT_FOUND = "task.not_found"
    CHECKLIST_ITEM_NOT_FOUND = "task.checklist_item.not_found"
    CHECKLIST_ITEM_TEXT_EMPTY = "task.checklist_item.text_empty"
    CHECKLIST_REORDER_MISMATCH = "task.checklist.reorder_mismatch"
    TAG_NOT_FOUND = "tag.not_found"
    ADJUDICATION_NOT_FOUND = "adjudication.not_found"
    TAG_DUPLICATE = "tag.duplicate"
    TAG_AMBIGUOUS = "tag.ambiguous"
    TAG_KIND_MISMATCH = "tag.kind_mismatch"
    TAG_NOT_ARCHIVED = "tag.not_archived"
    TAG_DEFAULT_PROTECTED = "tag.default_protected"
    CLIENT_HAS_INVOICES = "tag.client_has_invoices"
    CHANNEL_NOT_TAG_CREATABLE = "channel.not_tag_creatable"
    CHANNEL_ADMIN_ONLY = "channel.admin_only"
    CHANNEL_KEY_IMMUTABLE = "channel.key_immutable"
    CHANNEL_SEEDED_UNDELETABLE = "channel.seeded_undeletable"
    CHANNEL_NOT_FOUND = "channel.not_found"
    WORKFLOW_NOT_FOUND = "workflow.not_found"
    WORKFLOW_INVALID = "workflow.invalid"
    WORKFLOW_IN_USE = "workflow.in_use"
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
    INSUFFICIENT_CREDITS = "billing.insufficient_credits"
    RATE_CARD_NOT_FOUND = "billing.rate_card_not_found"
    MEMORY_NOT_FOUND = "memory.not_found"
    MEMORY_DIM_MISMATCH = "memory.dim_mismatch"
    MEMORY_CROSS_SUBJECT = "memory.cross_subject"
    NOTE_NOT_FOUND = "note.not_found"
    NOTE_NOT_LINKED_TO_TASK = "note.not_linked_to_task"
    REVISION_NOT_FOUND = "revision.not_found"
    ATTACHMENT_NOT_FOUND = "attachment.not_found"
    ATTACHMENT_TOO_LARGE = "attachment.too_large"
    INTENT_UNRECOGNIZED = "intent.unrecognized"
    INVOICE_NOT_FOUND = "invoice.not_found"
    INVOICE_NOT_DRAFT = "invoice.not_draft"
    INVOICE_INVALID = "invoice.invalid"
    FISCAL_PROFILE_REQUIRED = "invoice.fiscal_profile_required"
    ISSUER_PROFILE_IN_USE = "invoice.issuer_profile_in_use"
    ISSUER_PROFILE_SOLE_DEFAULT = "invoice.issuer_profile_sole_default"
    MANDATE_REQUIRED = "invoice.mandate_required"
    MANDATE_NOT_FOUND = "invoice.mandate_not_found"
    NOTIFICATION_NOT_FOUND = "notification.not_found"
    EXECUTOR_NOT_FOUND = "executor.not_found"
    EXECUTOR_INVALID = "executor.invalid"
    AGENT_RUN_NOT_FOUND = "agent_run.not_found"
    AGENT_RUN_NOT_DISPATCHABLE = "agent_run.not_dispatchable"
    AGENT_RUN_ALREADY_ACTIVE = "agent_run.already_active"
    AGENT_RUN_TERMINAL = "agent_run.terminal"
    HANDOFF_NOT_FOUND = "handoff.not_found"
    DISPATCH_NOT_FOUND = "dispatch.not_found"
    DISPATCH_NOT_PENDING = "dispatch.not_pending"
    DISPATCH_ALREADY_DECIDED = "dispatch.already_decided"
    AUTONOMOUS_DISABLED = "dispatch.autonomous_disabled"
    AUTONOMOUS_POLICY_INVALID = "dispatch.autonomous_policy_invalid"
    TASK_NOT_OFFERED = "task.not_offered"
    TASK_ALREADY_CLAIMED = "task.already_claimed"
    RECURRENCE_WITH_DEPS = "recurrence.with_dependencies"
    WORKSPACE_NOT_OWNER = "workspace.not_owner"
    WORKSPACE_SOLE = "workspace.sole"
    OAUTH_NOT_CONFIGURED = "oauth.not_configured"
    OAUTH_STATE_INVALID = "oauth.state_invalid"
    OAUTH_EXCHANGE_FAILED = "oauth.exchange_failed"
    OAUTH_SCOPE_INVALID = "oauth.scope_invalid"
    OAUTH_REFRESH_FAILED = "oauth.refresh_failed"
    GOOGLE_CALENDAR_SUBSCRIPTION_NOT_FOUND = "google_calendar.subscription_not_found"
    GOOGLE_CALENDAR_API_ERROR = "google_calendar.api_error"
    TELEGRAM_NOT_CONFIGURED = "telegram.not_configured"
    TELEGRAM_WEBHOOK_FORBIDDEN = "telegram.webhook_forbidden"
    # Bot webhook replies (ADR-0026 P4 / ADR-0017): Flow-generated bot
    # feedback goes through the catalog. The assistant's own generated
    # answer is passthrough (not catalogable).
    TELEGRAM_HELP = "telegram.help"
    TELEGRAM_FREETEXT_HINT = "telegram.freetext_hint"
    TELEGRAM_START_WELCOME = "telegram.start_welcome"
    TELEGRAM_CODE_INVALID = "telegram.code_invalid"
    TELEGRAM_LINKED = "telegram.linked"
    TELEGRAM_NOT_LINKED = "telegram.not_linked"
    TELEGRAM_NO_WORKSPACE = "telegram.no_workspace"
    TELEGRAM_VOICE_FAILED = "telegram.voice_failed"
    TELEGRAM_VOICE_SAVED = "telegram.voice_saved"
    TELEGRAM_EMPTY_IGNORED = "telegram.empty_ignored"
    TELEGRAM_TASK_CREATED = "telegram.task_created"
    TELEGRAM_NOTE_SAVED = "telegram.note_saved"
    TELEGRAM_NOTE_USAGE = "telegram.note_usage"
    TELEGRAM_UNKNOWN_COMMAND = "telegram.unknown_command"
    TELEGRAM_ASSISTANT_UNAVAILABLE = "telegram.assistant_unavailable"
    TELEGRAM_ASSISTANT_TIMEOUT = "telegram.assistant_timeout"
    TELEGRAM_ASSISTANT_ERROR = "telegram.assistant_error"
    TELEGRAM_ASSISTANT_BUDGET = "telegram.assistant_budget"
    AGENT_TOKEN_NOT_FOUND = "agent_token.not_found"  # noqa: S105 (message code, not a secret)
    AGENT_TOKEN_INVALID = "agent_token.invalid"  # noqa: S105 (message code, not a secret)
    AI_ASSISTANT_NOT_FOUND = "ai_assistant.not_found"
    AI_ASSISTANT_INVALID_SCOPE = "ai_assistant.invalid_scope"
    DOMAIN_ERROR = "domain.error"


# locale -> code -> template. Templates use str.format named params.
_CATALOG: dict[str, dict[MessageCode, str]] = {
    "en": {
        MessageCode.AUTH_INVALID_CREDENTIALS: "Invalid credentials",
        MessageCode.AUTH_MISSING_BEARER: "Missing Authorization: Bearer header",
        MessageCode.AUTH_TOKEN_INVALID: "Invalid or expired token",
        MessageCode.AUTH_TOKEN_NO_SUB: "Token without subject",
        MessageCode.AUTH_EMAIL_ALREADY_REGISTERED: "Email already registered",
        MessageCode.AUTH_EMAIL_NOT_VERIFIED: "Email not verified",
        MessageCode.AUTH_ACCOUNT_LOCKED: (
            "Account temporarily locked due to repeated failed logins"
        ),
        MessageCode.AUTH_MFA_REQUIRED: "Multi-factor authentication required",
        MessageCode.AUTH_MFA_ENROLL_REQUIRED: "Multi-factor enrolment required",
        MessageCode.AUTH_MFA_ALREADY_ENABLED: "MFA is already enabled",
        MessageCode.AUTH_MFA_NOT_ENABLED: "MFA is not enabled",
        MessageCode.AUTH_MFA_SETUP_REQUIRED: "MFA setup is required first",
        MessageCode.AUTH_INVALID_TOTP: "Invalid authentication code",
        MessageCode.AUTH_TOKEN_REVOKED: "Token has been revoked",
        MessageCode.AUTH_RESET_TOKEN_INVALID: "Invalid or expired reset token",
        MessageCode.AUTH_VERIFICATION_TOKEN_INVALID: "Invalid or expired token",
        MessageCode.AUTH_SIGNUP_DISABLED: "Public sign-up is disabled on this instance",
        MessageCode.RBAC_NO_MEMBERSHIP: "Not a member of this workspace",
        MessageCode.RBAC_ROLE_INSUFFICIENT: (
            "Role {current} is insufficient, requires >= {minimum}"
        ),
        MessageCode.MEMBER_NOT_FOUND: "No user with that email",
        MessageCode.MEMBER_LAST_OWNER: ("Cannot remove or demote the only owner"),
        MessageCode.MEMBER_ROLE_INVALID: "Invalid role",
        MessageCode.ADMIN_REQUIRED: "Administrator privileges required",
        MessageCode.ADMIN_SELF_GUARD: (
            "You cannot remove your own admin role or deactivate "
            "yourself; ask another administrator"
        ),
        MessageCode.USER_NOT_FOUND: "User not found",
        MessageCode.ORG_NOT_FOUND: "Workspace not found",
        MessageCode.CONFLICT_STALE_VERSION: "Stale version write",
        MessageCode.TASK_NOT_FOUND: "Task not found",
        MessageCode.CHECKLIST_ITEM_NOT_FOUND: "Checklist item not found",
        MessageCode.CHECKLIST_ITEM_TEXT_EMPTY: "Checklist item text must not be empty",
        MessageCode.CHECKLIST_REORDER_MISMATCH: (
            "Reorder payload does not match the task's checklist items"
        ),
        MessageCode.TAG_NOT_FOUND: "Tag not found",
        MessageCode.TAG_DUPLICATE: "A tag with this name already exists",
        MessageCode.TAG_AMBIGUOUS: "Ambiguous tag name: {name}",
        MessageCode.TAG_KIND_MISMATCH: "Tag is not of the expected kind",
        MessageCode.TAG_NOT_ARCHIVED: ("Archive the {kind} before deleting it permanently"),
        MessageCode.TAG_DEFAULT_PROTECTED: ("The default {kind} cannot be deleted"),
        MessageCode.CLIENT_HAS_INVOICES: (
            "Client has {count} invoice(s); void or delete them first"
        ),
        MessageCode.CHANNEL_NOT_TAG_CREATABLE: (
            "Memory channels are managed in settings, not created as tags"
        ),
        MessageCode.CHANNEL_ADMIN_ONLY: (
            "Only a platform administrator can manage memory channels"
        ),
        MessageCode.CHANNEL_KEY_IMMUTABLE: (
            "The system key of a seeded memory channel cannot be changed"
        ),
        MessageCode.CHANNEL_SEEDED_UNDELETABLE: (
            "A seeded memory channel cannot be deleted; disable it instead"
        ),
        MessageCode.CHANNEL_NOT_FOUND: "Memory channel not found",
        MessageCode.WORKFLOW_NOT_FOUND: "Workflow not found",
        MessageCode.WORKFLOW_INVALID: ("Invalid workflow: exactly one initial state is required"),
        MessageCode.WORKFLOW_IN_USE: (
            "Workflow in use or default: reassign its tasks / pick another default first"
        ),
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
        MessageCode.INSUFFICIENT_CREDITS: ("Insufficient credits: need {needed}, have {balance}"),
        MessageCode.RATE_CARD_NOT_FOUND: ("No active rate card for model {model_id}"),
        MessageCode.MEMORY_NOT_FOUND: "Memory blob not found",
        MessageCode.MEMORY_DIM_MISMATCH: ("Embedding dimension mismatch: expected {expected}"),
        MessageCode.MEMORY_CROSS_SUBJECT: (
            "Consolidation cannot cross org/project (hard isolation)"
        ),
        MessageCode.NOTE_NOT_FOUND: "Note not found",
        MessageCode.NOTE_NOT_LINKED_TO_TASK: ("Link a task to the note before billing time"),
        MessageCode.REVISION_NOT_FOUND: "Revision not found",
        MessageCode.ATTACHMENT_NOT_FOUND: "Attachment not found",
        MessageCode.ATTACHMENT_TOO_LARGE: "Attachment exceeds the maximum size",
        MessageCode.INTENT_UNRECOGNIZED: ("Command not recognized: {raw}"),
        MessageCode.INVOICE_NOT_FOUND: "Invoice not found",
        MessageCode.INVOICE_NOT_DRAFT: (
            "Invoice is emitted and immutable (only draft is editable)"
        ),
        MessageCode.INVOICE_INVALID: ("Invalid invoice: {detail}"),
        MessageCode.FISCAL_PROFILE_REQUIRED: (
            "The issuer profile is missing or incomplete: {detail}"
        ),
        MessageCode.ISSUER_PROFILE_IN_USE: (
            "This issuer profile is used by one or more invoices and cannot be deleted"
        ),
        MessageCode.ISSUER_PROFILE_SOLE_DEFAULT: (
            "Set another profile as default before deleting this one"
        ),
        MessageCode.MANDATE_REQUIRED: (
            "No active SdI transmission mandate for this issuer profile; grant one "
            "before transmitting through the accredited channel"
        ),
        MessageCode.MANDATE_NOT_FOUND: "No active SdI transmission mandate to revoke",
        MessageCode.NOTIFICATION_NOT_FOUND: "Notification not found",
        MessageCode.EXECUTOR_NOT_FOUND: "Executor not found",
        MessageCode.EXECUTOR_INVALID: "Invalid executor: {detail}",
        MessageCode.AGENT_RUN_NOT_FOUND: "Agent run not found",
        MessageCode.AGENT_RUN_NOT_DISPATCHABLE: (
            "Task is not dispatchable to an agent: it must be an llm_agent "
            "task with an assigned, dispatchable executor"
        ),
        MessageCode.AGENT_RUN_ALREADY_ACTIVE: ("An agent run for this task is already active"),
        MessageCode.AGENT_RUN_TERMINAL: ("Agent run has already finished and cannot be cancelled"),
        MessageCode.HANDOFF_NOT_FOUND: "Handoff not found",
        MessageCode.DISPATCH_NOT_FOUND: "Dispatch request not found",
        MessageCode.DISPATCH_NOT_PENDING: ("Dispatch request is not pending (cannot approve)"),
        MessageCode.DISPATCH_ALREADY_DECIDED: ("Dispatch request has already been decided"),
        MessageCode.AUTONOMOUS_DISABLED: ("Autonomous dispatch is disabled for this workspace"),
        MessageCode.AUTONOMOUS_POLICY_INVALID: ("Invalid autonomous dispatch policy: {detail}"),
        MessageCode.TASK_NOT_OFFERED: ("Task is not offered (nothing to claim or decline)"),
        MessageCode.TASK_ALREADY_CLAIMED: ("Task has already been claimed by a member"),
        MessageCode.RECURRENCE_WITH_DEPS: (
            "A recurring task cannot have dependencies (mutually exclusive in v1)"
        ),
        MessageCode.WORKSPACE_NOT_OWNER: "Only the workspace owner can do this",
        MessageCode.WORKSPACE_SOLE: (
            "Cannot delete your only workspace: create or join another first"
        ),
        MessageCode.OAUTH_NOT_CONFIGURED: ("Google OAuth is not configured on this server"),
        MessageCode.OAUTH_STATE_INVALID: "Invalid or expired OAuth state",
        MessageCode.OAUTH_EXCHANGE_FAILED: (
            "Failed to exchange the authorization code for tokens: {detail}"
        ),
        MessageCode.OAUTH_SCOPE_INVALID: (
            "Invalid OAuth scope: must be one of gmail, calendar, both"
        ),
        MessageCode.OAUTH_REFRESH_FAILED: ("Failed to refresh the Google access token: {detail}"),
        MessageCode.GOOGLE_CALENDAR_SUBSCRIPTION_NOT_FOUND: (
            "Google Calendar subscription not found"
        ),
        MessageCode.GOOGLE_CALENDAR_API_ERROR: ("Google Calendar API error: {detail}"),
        MessageCode.TELEGRAM_NOT_CONFIGURED: ("Telegram bot is not configured on this instance"),
        MessageCode.TELEGRAM_WEBHOOK_FORBIDDEN: "Forbidden webhook caller",
        MessageCode.TELEGRAM_HELP: (
            "Flow bot:\n"
            "• /note <text> → save a note\n"
            "• /task <title> → create a task\n"
            "• voice message → save a voice note\n"
            "• /help → this message\n"
            "Send free text to chat with the Flow assistant."
        ),
        MessageCode.TELEGRAM_FREETEXT_HINT: (
            "Send /note <text> to save a note, /task <title> for a task, or a voice"
            " message for a voice note. /help for the list."
        ),
        MessageCode.TELEGRAM_START_WELCOME: (
            "Welcome to Flow. To link your account, open the deep-link from the Flow"
            " web app's Telegram settings."
        ),
        MessageCode.TELEGRAM_CODE_INVALID: (
            "That link code is invalid or has expired. Generate a new one from the Flow web app."
        ),
        MessageCode.TELEGRAM_LINKED: (
            "Your Telegram account is now linked to Flow. Send /help to see what I"
            " can do, or just tell me what you need."
        ),
        MessageCode.TELEGRAM_NOT_LINKED: (
            "Your Telegram chat is not linked to Flow. Open the Flow web app and use"
            " the Telegram settings to link."
        ),
        MessageCode.TELEGRAM_NO_WORKSPACE: (
            "Your Flow account has no workspace yet. Open the web app first."
        ),
        MessageCode.TELEGRAM_VOICE_FAILED: "Could not download voice message. Try again.",
        MessageCode.TELEGRAM_VOICE_SAVED: "Voice note saved.",
        MessageCode.TELEGRAM_EMPTY_IGNORED: (
            "Empty message ignored. Send /note <text> to save a note."
        ),
        MessageCode.TELEGRAM_TASK_CREATED: "Task created: {title}",
        MessageCode.TELEGRAM_NOTE_SAVED: "Note saved.",
        MessageCode.TELEGRAM_NOTE_USAGE: "Usage: /note <text>",
        MessageCode.TELEGRAM_UNKNOWN_COMMAND: "Unknown command {command}. {help}",
        MessageCode.TELEGRAM_ASSISTANT_UNAVAILABLE: (
            "The assistant is unavailable right now. Try again, or use /note and /task."
        ),
        MessageCode.TELEGRAM_ASSISTANT_TIMEOUT: (
            "I couldn't finish that in time. Try rephrasing, or use /note and /task."
        ),
        MessageCode.TELEGRAM_ASSISTANT_ERROR: (
            "Something went wrong handling that. Try again, or use /note and /task."
        ),
        MessageCode.TELEGRAM_ASSISTANT_BUDGET: (
            "Budget for this turn is exhausted. Try a simpler request."
        ),
        MessageCode.AGENT_TOKEN_NOT_FOUND: "Agent token not found",
        MessageCode.AI_ASSISTANT_NOT_FOUND: "AI assistant not found",
        MessageCode.AI_ASSISTANT_INVALID_SCOPE: ("Unknown scope key: {key}"),
        MessageCode.AGENT_TOKEN_INVALID: "Invalid or revoked agent token",
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
