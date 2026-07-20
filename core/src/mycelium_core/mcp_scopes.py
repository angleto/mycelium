"""Scope catalog for AI assistants over MCP.

The user picks a subset of these when creating an assistant in
``/settings/ai-assistants``. The catalog is family-coarse rather than
per-tool: a domain (``tasks``, ``time``, ...) splits into ``:read`` /
``:write`` and a small ``:danger`` bucket for ops that spend credits or
destroy data.

Enforcement (task c19f2f63, enabler B): the HTTP transport reads
``assistant.scope`` (a JSONB list) from the SECURITY DEFINER
``authenticate_agent_token`` lookup (migration 0059) and publishes it into
``server._PRINCIPAL_SCOPE``. Every concrete tool maps to one scope key in
``mcp.tool_scopes.TOOL_SCOPES``; the gateway gate rejects an ``execute_tool``
call outside ``scope`` and hides out-of-scope tools from ``search_tools`` /
``describe_tools`` (``server._scope_permits``). FAIL-CLOSED: a tool with no
mapping is denied to a scoped assistant (a drift-guard test keeps the map in
lockstep with the registry). Legacy bare ``agent_tokens`` rows (assistant_id
IS NULL) and the stdio / human-bearer paths keep their previous all-tools
access — the filter only bites a bound assistant that carries a scope list.
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
    ScopeDef(
        "invoices:read",
        "read",
        "Read invoices",
        "List and read invoices (status, number, totals) and their FatturaPA XML.",
    ),
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
    # --- additions (task c19f2f63, enabler B): cover the MCP domains the coarse
    # v1 catalog missed (email, search, annotations, knowledge graph, workflows
    # read, agent-runs/dispatch/executors read+write) and split off the
    # destructive/credit-spending edges so a writer is not implicitly a destroyer
    # or a spender (least-privilege). Category drives the DEFAULT_SCOPES opt-out:
    # read/write are on by default, danger is opt-in. ---
    # read
    ScopeDef(
        "search:read",
        "read",
        "Search",
        "Run unified and semantic search across tasks, notes and memory, including "
        "graph-based context retrieval. Read-only.",
    ),
    ScopeDef(
        "email:read",
        "read",
        "Read email",
        "List and read email accounts, ingested messages, threads and drafted replies.",
    ),
    ScopeDef(
        "annotations:read",
        "read",
        "Read annotations",
        "Read inline comments and suggestions on notes and task descriptions.",
    ),
    ScopeDef(
        "kg:read",
        "read",
        "Read knowledge graph",
        "Query knowledge-graph entities and their relationship facts extracted from notes.",
    ),
    ScopeDef(
        "workflows:read",
        "read",
        "Read workflows",
        "View workflow definitions, their ordered states and allowed transitions.",
    ),
    ScopeDef(
        "agent_runs:read",
        "read",
        "Read agent runs",
        "View agent-run status, history, steps, credits spent and produced artifacts.",
    ),
    ScopeDef(
        "dispatch:read",
        "read",
        "Read dispatch queue",
        "View the agent-dispatch/approval queue, statuses and projected credit costs.",
    ),
    ScopeDef(
        "executors:read",
        "read",
        "Read executors",
        "List workspace executors (human members and LLM agents) and their capacity.",
    ),
    # write
    ScopeDef(
        "email:write",
        "write",
        "Manage email",
        "Register or configure email accounts, set default tags, sync inboxes, and "
        "queue or discard AI draft replies. Does not send mail.",
    ),
    ScopeDef(
        "annotations:write",
        "write",
        "Write annotations",
        "Add, edit, assign, resolve, reopen or withdraw inline comments and "
        "suggestions. Does not rewrite the underlying document body.",
    ),
    ScopeDef(
        "schedule:write",
        "write",
        "Write schedule",
        "Write scheduler pins/constraints on tasks and recompute the derived "
        "schedule. Does not spend credits.",
    ),
    ScopeDef(
        "notifications:write",
        "write",
        "Manage notifications",
        "Set per-channel notification preferences and enqueue due-date reminders. "
        "Internal only (see 'Send notifications externally').",
    ),
    ScopeDef(
        "agent_runs:write",
        "write",
        "Control agent runs",
        "Request cooperative cancellation of a running agent. Does not start runs "
        "or spend credits.",
    ),
    ScopeDef(
        "dispatch:write",
        "write",
        "Deny dispatch requests",
        "Deny pending agent-dispatch requests. Never starts a run or spends credits.",
    ),
    ScopeDef(
        "executors:write",
        "write",
        "Manage executors",
        "Create, update and delete executors (capacity, credit budgets, capability "
        "tags). Deletion can make LLM tasks unassignable.",
    ),
    # danger
    ScopeDef(
        "email:send",
        "danger",
        "Send email",
        "Send mail from a connected account (send, reply in-thread, approve-and-send "
        "drafts). Messages leave your boundary and cannot be recalled.",
    ),
    ScopeDef(
        "memory:delete",
        "danger",
        "Delete or erase memories",
        "Hard-delete a memory blob (cascades to tags/sources/vector) or run GDPR "
        "erasure by provenance. Split from memory:write so a writer cannot destroy.",
    ),
    ScopeDef(
        "memory:admin",
        "danger",
        "Manage memory channels",
        "Create, rename, enable/disable or delete the workspace's memory channels "
        "(the channel vocabulary that files and routes every memory).",
    ),
    ScopeDef(
        "ai:generate",
        "danger",
        "Spend LLM credits (synthesis)",
        "Run metered LLM synthesis/extraction (garden distillation, seasonal "
        "summaries, knowledge-graph extraction) that spends the credit budget. "
        "Distinct from agent_runs:start.",
    ),
    ScopeDef(
        "billing:write",
        "danger",
        "Write billing",
        "Grant or meter LLM credits (money movement against the org balance) and "
        "create or update model rate cards. Read-only by default.",
    ),
    ScopeDef(
        "notifications:send",
        "danger",
        "Send notifications externally",
        "Force-send pending notifications to external channels (email/push). An "
        "irreversible external side effect.",
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
