"""Scope catalog for AI assistants over MCP.

The user picks a subset of these when creating an assistant in
``/settings/ai-assistants``. The catalog is family-coarse rather than
per-tool: a domain (``tasks``, ``time``, ...) splits into ``:read`` /
``:write`` and a small ``:danger`` bucket for ops that spend credits or
destroy data.

Enforcement: the MCP transport reads ``assistant.scope`` (a JSONB
list) from the SECURITY DEFINER ``authenticate_agent_token`` lookup
(migration 0059) and the gate filters tool calls against it. Legacy
bare ``agent_tokens`` rows (assistant_id IS NULL, minted before this
flow existed) keep their previous all-tools access — the scope filter
only kicks in when an assistant is bound. Future work: hook each
``@mcp.tool()`` to a scope key and reject calls outside ``scope``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Category = Literal["read", "write", "danger"]


@dataclass(frozen=True, slots=True)
class ScopeDef:
    key: str
    category: Category
    label: str
    description: str


SCOPE_CATALOG: tuple[ScopeDef, ...] = (
    # --- read ---
    ScopeDef("tasks:read", "read", "Read tasks", "List, search, and read tasks and their tags."),
    ScopeDef("time:read", "read", "Read time", "Read time entries, running timers and reports."),
    ScopeDef("tags:read", "read", "Read taxonomy", "Read tags, clients and projects."),
    ScopeDef("notes:read", "read", "Read notes", "Read notes and their attachments."),
    ScopeDef("memory:read", "read", "Read memory", "Search the memory store and read blobs."),
    ScopeDef("calendar:read", "read", "Read calendar", "Read calendars, events and holidays."),
    ScopeDef(
        "schedule:read", "read", "Read schedule", "Read the resource-aware schedule projection."
    ),
    ScopeDef("comments:read", "read", "Read comments", "Read task comments."),
    ScopeDef("dependencies:read", "read", "Read dependencies", "Read the task dependency graph."),
    ScopeDef("budgets:read", "read", "Read budgets", "Read budget envelopes and balances."),
    # --- write ---
    ScopeDef(
        "tasks:write", "write", "Write tasks", "Create, update and re-state tasks (no deletion)."
    ),
    ScopeDef("time:write", "write", "Write time", "Start, stop and edit time entries."),
    ScopeDef(
        "tags:write", "write", "Write taxonomy", "Create or update tags, clients and projects."
    ),
    ScopeDef("notes:write", "write", "Write notes", "Create and edit notes."),
    ScopeDef("memory:write", "write", "Write memory", "Append to the memory store."),
    ScopeDef("comments:write", "write", "Write comments", "Add comments to tasks."),
    ScopeDef("calendar:write", "write", "Write calendar", "Create or edit events and holidays."),
    ScopeDef(
        "dependencies:write", "write", "Write dependencies", "Create or remove task dependencies."
    ),
    # --- danger ---
    ScopeDef(
        "attachments:write",
        "danger",
        "Upload attachments",
        "Upload files to tasks or notes. The bytes leave the workspace boundary on read.",
    ),
    ScopeDef(
        "workflows:write",
        "danger",
        "Edit workflows",
        "Change workflow state machines used by every task — invariants matter here.",
    ),
    ScopeDef(
        "delete:taxonomy",
        "danger",
        "Delete clients/projects",
        "Hard-delete archived clients and projects (cascades attachments, notes, time entries).",
    ),
    ScopeDef(
        "dispatch:approve",
        "danger",
        "Approve LLM dispatch",
        "Approve a pending agent dispatch request — every approval can spend credits.",
    ),
    ScopeDef(
        "agent_runs:start",
        "danger",
        "Start LLM runs",
        "Trigger an agent run — costs credits against the workspace budget.",
    ),
    ScopeDef(
        "invoices:write",
        "danger",
        "Edit invoices",
        "Create or edit invoices (fiscal records). Read-only by default.",
    ),
    ScopeDef(
        "billing:read",
        "danger",
        "Read billing",
        "Read the billing ledger and credit balances. Surfaces sensitive financial data.",
    ),
    ScopeDef(
        "budgets:write",
        "danger",
        "Edit budgets",
        "Create or adjust budget envelopes. Loosening a budget is a finance decision.",
    ),
)


# Default scope set for a freshly-minted assistant. Picked to match the
# user's policy "tutto tranne danger": every read + every non-danger
# write enabled, every danger scope opt-in.
DEFAULT_SCOPES: tuple[str, ...] = tuple(s.key for s in SCOPE_CATALOG if s.category != "danger")


VALID_SCOPE_KEYS: frozenset[str] = frozenset(s.key for s in SCOPE_CATALOG)


__all__ = [
    "DEFAULT_SCOPES",
    "SCOPE_CATALOG",
    "VALID_SCOPE_KEYS",
    "Category",
    "ScopeDef",
]
