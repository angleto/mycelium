"""Per-tool scope map for MCP scope enforcement (task c19f2f63, enabler B).

Every concrete MCP tool (the ~250 dispatched behind the gateway's
``execute_tool``) maps to exactly ONE required scope key from
``core.mcp_scopes.SCOPE_CATALOG``, or to ``None`` for a META tool
(self-identity / liveness / discovery) that must always be callable so an
agent can bootstrap and find out what it may do.

The exception is a handful of tools whose required key depends on a call
ARGUMENT (a ``kind`` discriminator): those live in ``DYNAMIC_TOOL_SCOPES``
below with a per-call resolver instead of a single static key. A tool is in
exactly one of the two maps.

FAIL-CLOSED: a tool absent from this map is DENIED to a *scoped* assistant
(bare / stdio / human tokens keep full access -- see ``server._scope_permits``).
The ``test_mcp_scope_enforcement`` drift guard asserts this map stays in
lockstep with the live registry, so a newly added @mcp.tool() that lacks an
entry here fails CI rather than silently defaulting open or shut.

Derived by the classification workflow wf_58460418 (14 per-domain agents +
synthesis) and hand-audited; the scope TAXONOMY (the new keys in
``mcp_scopes.SCOPE_CATALOG``) is the user-facing contract in
/settings/ai-assistants.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Sentinel distinct from ``None``: ``None`` = META (allowed), a missing key
# = UNMAPPED (denied, fail-closed).
UNMAPPED: object = object()

# tool name -> required scope key, or None for META (always allowed).
TOOL_SCOPES: dict[str, str | None] = {
    "help": None,
    "ping": None,
    "whoami": None,
    # --- scoped ---
    "agent_run_get": "agent_runs:read",
    "agent_runs_list": "agent_runs:read",
    "agent_run_start": "agent_runs:start",
    "agent_run_cancel": "agent_runs:write",
    "append_message": "ai:generate",
    "distill_note": "ai:generate",
    "extract_cluster_pattern": "ai:generate",
    "kg_extract": "ai:generate",
    "synthesize_season": "ai:generate",
    "synthesize_speech": "ai:generate",
    "transcribe_note": "ai:generate",
    "count_annotations": "annotations:read",
    "list_annotations": "annotations:read",
    "list_assigned_annotations": "annotations:read",
    "add_annotation": "annotations:write",
    "assign_annotation": "annotations:write",
    "delete_annotation": "annotations:write",
    "edit_annotation": "annotations:write",
    "edit_annotation_body_instructions": "annotations:write",
    "reopen_annotation": "annotations:write",
    "delete_attachment": "attachments:write",
    "download_attachment_capability": "attachments:write",
    "upload_attachment": "attachments:write",
    "upload_attachment_capability": "attachments:write",
    "upload_attachment_instructions": "attachments:write",
    "billing_balance": "billing:read",
    "list_rate_cards": "billing:read",
    "list_usage": "billing:read",
    "grant_credits": "billing:write",
    "meter_usage": "billing:write",
    "upsert_rate_card": "billing:write",
    "budget_consumption": "budgets:read",
    "list_budgets": "budgets:read",
    "prioritize_within_budget": "budgets:read",
    "create_budget": "budgets:write",
    "delete_budget": "budgets:write",
    "update_budget": "budgets:write",
    "list_calendars": "calendar:read",
    "list_holidays": "calendar:read",
    "add_holiday": "calendar:write",
    "create_calendar": "calendar:write",
    "remove_holiday": "calendar:write",
    "set_user_calendar": "calendar:write",
    "list_comments": "comments:read",
    "accept_suggestion": "comments:write",
    "add_comment": "comments:write",
    "add_comment_instructions": "comments:write",
    "delete_comment": "comments:write",
    "propose_suggestion": "comments:write",
    "propose_suggestion_instructions": "comments:write",
    "reject_suggestion": "comments:write",
    "resolve_annotation": "comments:write",
    "graph": "dependencies:read",
    "list_dependencies": "dependencies:read",
    "add_dependency": "dependencies:write",
    "remove_dependency": "dependencies:write",
    "dispatch_approve": "dispatch:approve",
    "dispatch_tick": "dispatch:approve",
    "dispatch_requests_list": "dispatch:read",
    "dispatch_deny": "dispatch:write",
    "email_thread": "email:read",
    "list_email_accounts": "email:read",
    "list_email_drafts": "email:read",
    "list_email_messages": "email:read",
    "approve_email_draft": "email:send",
    "reply_email": "email:send",
    "send_email": "email:send",
    "create_email_account": "email:write",
    "draft_email_reply": "email:write",
    "reject_email_draft": "email:write",
    "set_email_auto_draft_replies": "email:write",
    "set_email_default_tags": "email:write",
    "set_email_ingest_to_memory": "email:write",
    "sync_email_account": "email:write",
    "list_events": "events:read",
    "executors_list": "executors:read",
    "executor_create": "executors:write",
    "executor_delete": "executors:write",
    "executor_update": "executors:write",
    "get_invoice": "invoices:read",
    "get_invoice_xml": "invoices:read",
    "list_invoices": "invoices:read",
    "list_issuer_profiles": "invoices:read",
    "add_invoice_line": "invoices:write",
    "create_invoice": "invoices:write",
    "ingest_sdi_receipt": "invoices:write",
    "invoice_credit_note": "invoices:write",
    "set_issuer_profile": "invoices:write",
    "transmit_invoice": "invoices:write",
    "kg_entities": "kg:read",
    "kg_neighbors": "kg:read",
    "memory_channel_create": "memory:admin",
    "memory_channel_delete": "memory:admin",
    "memory_channel_update": "memory:admin",
    "gdpr_erase_note": "memory:delete",
    "memory_delete_blob": "memory:delete",
    "memory_erase": "memory:delete",
    "memory_channels_list": "memory:read",
    "memory_get_blob": "memory:read",
    "memory_migration_status": "memory:read",
    "memory_status": "memory:read",
    "memory_attach_tag": "memory:write",
    "memory_consolidate": "memory:write",
    "memory_detach_tag": "memory:write",
    "memory_migrate": "memory:write",
    "memory_recompute_tiers": "memory:write",
    "memory_write": "memory:write",
    "garden_classify": "notes:read",
    "garden_review_pending": "notes:read",
    "get_note": "notes:read",
    "get_note_part": "notes:read",
    "get_note_part_body_capability": "notes:read",
    "get_note_revision": "notes:read",
    "graph_walk": "notes:read",
    "list_distillation_candidates": "notes:read",
    "list_note_links": "notes:read",
    "list_note_parts": "notes:read",
    "list_note_revisions": "notes:read",
    "list_note_task_links": "notes:read",
    "list_notes": "notes:read",
    "list_turns": "notes:read",
    "suggest_note_links": "notes:read",
    "add_note_part": "notes:write",
    "add_note_part_instructions": "notes:write",
    "add_note_tag": "notes:write",
    "append_note_part": "notes:write",
    "append_to_note": "notes:write",
    "archive_note": "notes:write",
    "create_note": "notes:write",
    "create_task_note": "notes:write",
    "delete_note": "notes:write",
    "delete_note_part": "notes:write",
    "email_to_note": "notes:write",
    "garden_apply": "notes:write",
    "garden_review_approve": "notes:write",
    "garden_review_reject": "notes:write",
    "get_or_create_task_note": "notes:write",
    "link_notes": "notes:write",
    "merge_notes": "notes:write",
    "note_restore_source": "notes:write",
    "patch_note_part_body_capability": "notes:write",
    "prepend_note_part": "notes:write",
    "protect_note": "notes:write",
    "remove_note_tag": "notes:write",
    "reorder_note_parts": "notes:write",
    "replace_in_note_part": "notes:write",
    "restore_note": "notes:write",
    "restore_note_revision": "notes:write",
    "run_command": "notes:write",
    "set_note_maturity": "notes:write",
    "set_note_part_body_capability": "notes:write",
    "set_note_part_body_instructions": "notes:write",
    "start_conversation_session": "notes:write",
    "start_task_on_note": "notes:write",
    "unlink_note_task": "notes:write",
    "unlink_notes": "notes:write",
    "update_note": "notes:write",
    "update_note_part": "notes:write",
    "dispatch_notifications": "notifications:send",
    "scan_reminders": "notifications:write",
    "set_notification_pref": "notifications:write",
    "get_schedule": "schedule:read",
    "list_schedule": "schedule:read",
    "recompute_schedule": "schedule:write",
    "set_task_schedule": "schedule:write",
    "graph_focus_context": "search:read",
    "memory_search": "search:read",
    "search": "search:read",
    "get_tag": "tags:read",
    "list_clients": "tags:read",
    "list_projects": "tags:read",
    "list_tags": "tags:read",
    "add_task_tag": "tags:write",
    "create_client": "tags:write",
    "create_project": "tags:write",
    "create_tag": "tags:write",
    "move_task_to_project": "tags:write",
    "remove_task_tag": "tags:write",
    "set_tag_scope": "tags:write",
    "update_client": "tags:write",
    "update_project": "tags:write",
    "update_tag": "tags:write",
    "count_tasks": "tasks:read",
    "errands": "tasks:read",
    "get_task": "tasks:read",
    "get_task_revision": "tasks:read",
    "list_checklist": "tasks:read",
    "list_task_participants": "tasks:read",
    "list_task_relations": "tasks:read",
    "list_task_revisions": "tasks:read",
    "list_tasks": "tasks:read",
    "resolve_prefix": "tasks:read",
    "task_handoffs_list": "tasks:read",
    "what_can_i_do_now": "tasks:read",
    "add_checklist_item": "tasks:write",
    "add_task_participant": "tasks:write",
    "add_task_relation": "tasks:write",
    "append_to_task_description": "tasks:write",
    "archive_task": "tasks:write",
    "assign_task": "tasks:write",
    "check_item": "tasks:write",
    "clear_done": "tasks:write",
    "create_recurrence": "tasks:write",
    "create_task": "tasks:write",
    "delete_task": "tasks:write",
    "derive_task_from_note": "tasks:write",
    "email_to_task": "tasks:write",
    "prepend_to_task_description": "tasks:write",
    "promote_note_to_task": "tasks:write",
    "record_task_artifact": "tasks:write",
    "remove_item": "tasks:write",
    "remove_task_participant": "tasks:write",
    "remove_task_relation": "tasks:write",
    "restore_task": "tasks:write",
    "restore_task_revision": "tasks:write",
    "set_task_assignee": "tasks:write",
    "set_task_owner": "tasks:write",
    "spawn_due_recurrences": "tasks:write",
    "task_claim": "tasks:write",
    "task_decline": "tasks:write",
    "task_offer": "tasks:write",
    "unassign_task": "tasks:write",
    "uncheck_item": "tasks:write",
    "update_task": "tasks:write",
    "get_time_entry": "time:read",
    "list_running_timers": "time:read",
    "list_time_entries": "time:read",
    "time_report": "time:read",
    "time_report_by_task": "time:read",
    "add_time_entry": "time:write",
    "delete_time_entry": "time:write",
    "pause_timer": "time:write",
    "resume_timer": "time:write",
    "start_timer": "time:write",
    "stop_timer": "time:write",
    "update_time_entry": "time:write",
    "list_workflows": "workflows:read",
    "workflow_states": "workflows:read",
    "workflow_transitions": "workflows:read",
    "create_workflow": "workflows:write",
    "delete_workflow": "workflows:write",
    "set_default_workflow": "workflows:write",
    "set_project_workflow": "workflows:write",
    "set_task_state": "workflows:write",
    "update_workflow": "workflows:write",
}


# --------------------------------------------------------------------------
# Argument-dependent tools
# --------------------------------------------------------------------------
# A handful of tools multiplex several operations, each needing a DIFFERENT
# scope, behind one signature: the required key depends on a call argument
# (a ``kind`` / target discriminator), which the flat ``TOOL_SCOPES`` map
# above cannot express. Mapping such a tool to a single key is a real bug --
# it either over-grants (a tasks-only assistant reaches an annotation's body)
# or under-grants (an annotations-only assistant is denied its own comment
# body). Enabler B's REST classification surfaced exactly this divergence:
# ``/annotations/{id}/body/*`` is annotations:* on REST while the MCP twin
# was tasks:* for every kind.
#
# Each entry is ``(resolver, possible)``:
#   - ``resolver(arguments)`` -> the ONE scope THIS call needs, or ``None``
#     when the arguments do not determine a valid kind (the tool itself then
#     rejects them; the gate fails closed rather than inventing a scope).
#   - ``possible`` -> every scope the tool could require across its kinds.
#     Used when there are no concrete arguments yet (``search_tools`` /
#     ``describe_tools`` visibility): the tool is listed iff the assistant
#     holds AT LEAST ONE of them.
#
# A tool is in EITHER ``TOOL_SCOPES`` or ``DYNAMIC_TOOL_SCOPES``, never both
# (a drift-guard test asserts the two are disjoint and jointly total).


def _text_block_scope(verb: str) -> Callable[[dict[str, Any]], str | None]:
    """``get/set/patch_text_block_capability`` mint a body capability for a
    task description (``kind='task_description'`` -> tasks:*) or a comment
    body (``kind='annotation'`` -> annotations:*). ``verb`` is read / write
    (patch is a write; the catalog has no ``:patch`` half)."""

    def resolve(arguments: dict[str, Any]) -> str | None:
        kind = arguments.get("kind")
        if kind == "annotation":
            return f"annotations:{verb}"
        if kind == "task_description":
            return f"tasks:{verb}"
        return None

    return resolve


def _list_attachments_scope(arguments: dict[str, Any]) -> str | None:
    """``list_attachments`` reads a note's OR a task's attachment metadata
    (exactly one id). A notes-only assistant must reach its own note's
    attachments, so the family follows the parent, not a fixed tasks:read."""
    if arguments.get("note_id"):
        return "notes:read"
    if arguments.get("task_id"):
        return "tasks:read"
    return None


DYNAMIC_TOOL_SCOPES: dict[str, tuple[Callable[[dict[str, Any]], str | None], frozenset[str]]] = {
    "get_text_block_capability": (
        _text_block_scope("read"),
        frozenset({"annotations:read", "tasks:read"}),
    ),
    "set_text_block_capability": (
        _text_block_scope("write"),
        frozenset({"annotations:write", "tasks:write"}),
    ),
    "patch_text_block_capability": (
        _text_block_scope("write"),
        frozenset({"annotations:write", "tasks:write"}),
    ),
    "list_attachments": (_list_attachments_scope, frozenset({"notes:read", "tasks:read"})),
}


def required_scope(tool_name: str) -> str | None | object:
    """The scope key ``tool_name`` needs, ``None`` if it is META (always
    allowed), or the ``UNMAPPED`` sentinel if the tool is not in the map
    (fail-closed: a scoped assistant is denied). For an argument-dependent
    tool this returns ``UNMAPPED`` -- use :func:`required_scope_for_call`
    with the concrete arguments instead."""
    return TOOL_SCOPES.get(tool_name, UNMAPPED)


def required_scope_for_call(
    tool_name: str, arguments: dict[str, Any] | None
) -> str | None | object:
    """The scope a CONCRETE call needs. Resolves an argument-dependent tool
    from its arguments (``None`` if the arguments do not pin a kind), else the
    static map (``UNMAPPED`` if the tool is unknown). Used to report the
    required scope in the gate's denial envelope."""
    dyn = DYNAMIC_TOOL_SCOPES.get(tool_name)
    if dyn is not None:
        return dyn[0](arguments or {})
    return TOOL_SCOPES.get(tool_name, UNMAPPED)
