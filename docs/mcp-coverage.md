# MCP coverage

Inventory of every `@mcp.tool()` exposed by `mcp/src/flow_mcp/server.py`,
grouped by domain, with the corresponding service-layer entry point and
the scope key (`core/src/flow_core/mcp_scopes.py`) that will gate each
tool when the per-assistant scope filter is wired
(`SCOPE_CATALOG.<key>`, currently advisory: enforcement is the deferred
"hook each `@mcp.tool()` to a scope key" item flagged in
`mcp_scopes.py`).

Snapshot: 140 tools across 18 domains; the scope catalog defines 23
scopes (10 read, 8 write, 5 danger). The MCP surface is the only
client-equal channel beside REST per ADR-0001 (`mcp/` is a thin adapter
over `core/`, co-equal to `api/`), so any GUI-only capability is a
genuine gap, not an asymmetry by design.

## Convention

- `path:line` columns point at the symbol declaration. The MCP path is
  always `mcp/src/flow_mcp/server.py`; the service path is always
  `core/src/flow_core/services/<module>.py`.
- "Scope key" is the entry from `SCOPE_CATALOG` in `mcp_scopes.py`.
  When a tool acts on data of more than one family (e.g. tools that
  attach a tag to a task), I report the **most specific** key (for
  `add_task_tag` that is `tasks:write`, not `tags:write`, because the
  mutation lives on the task row).
- "GAP" means: the service-layer function exists in `flow_core` (and
  may be reachable from REST or SPA) but is **not exposed as an MCP
  tool**. Items marked "GAP (no service)" need both a service entry
  and a tool.

## Domain summary

| Domain | Tools | Read | Write/Mutating | Owner-gated | Scope buckets |
|---|---|---|---|---|---|
| Tasks | 16 | 4 | 12 | a few via RBAC sudo | `tasks:read`, `tasks:write` |
| Task participants (appointment-tasks) | 3 | 1 | 2 | none | `tasks:read`, `tasks:write` |
| Tags / Projects / Clients | 11 | 4 | 7 | tag scope is admin | `tags:read`, `tags:write`, `delete:taxonomy` |
| Notes | 16 | 4 | 12 | none | `notes:read`, `notes:write` |
| Memory | 9 | 3 | 6 | channel admin gated | `memory:read`, `memory:write` |
| Time tracking | 9 | 4 | 5 | none | `time:read`, `time:write` |
| Calendar (working calendars + holidays) | 6 | 2 | 4 | service-level | `calendar:read`, `calendar:write` |
| Events (appointments) | — | — | — | unified into Tasks (migration 0094 / 0097) | use `tasks:*` |
| Dependencies | 4 | 2 | 2 | none | `dependencies:read`, `dependencies:write` |
| Workflows | 7 | 3 | 4 | service-level | (none yet) → `workflows:write` |
| Comments | 2 | 1 | 1 | none | `comments:read`, `comments:write` |
| Attachments | 2 | 1 | 1 | none | (read in `notes:read`/`tasks:read`); `attachments:write` is danger |
| Budgets | 5 | 2 | 3 | none | `budgets:read`, `budgets:write` |
| Billing / metering | 6 | 3 | 3 | admin gated | `billing:read`, (no `billing:write` key yet) |
| Dispatch (LLM admission gate) | 4 | 1 | 3 | yes (sudo) | `dispatch:approve` |
| Agent runs | 4 | 2 | 2 | yes (sudo) | `agent_runs:start` |
| Executors | 4 | 1 | 3 | yes (sudo) | (none yet) |
| Focus / advisory | 3 | 3 | 0 | none | `tasks:read` (no dedicated key) |
| Scheduler | 4 | 2 | 2 | service-level | `schedule:read` |
| Invoices | 5 | 0 | 5 | admin gated | `invoices:write` |
| Email | 7 | 2 | 5 | none | (no key yet) |
| Notifications / recurrences | 4 | 0 | 4 | none | (no key yet) |
| Auxiliary (`ping`) | 1 | 1 | 0 | none | (unscoped) |

Totals: 140 `@mcp.tool()` symbols. The table groups Calendar and
Events separately for ergonomics; both map to the same `calendar:*`
scope buckets.

---

## Tasks

The mutation surface is full: create, read, update, soft-delete /
restore, archive / unarchive, set state, parent / subtask reparenting
(via `update_task`), and the assignee / tag join is symmetric.

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `create_task` | server.py:391 | `tasks.create_task` (tasks.py:86) | `tasks:write` |
| `list_tasks` | server.py:437 | `tasks.list_tasks` (tasks.py:189) | `tasks:read` |
| `get_task` | server.py:546 | `tasks.get_task` (tasks.py:73) | `tasks:read` |
| `update_task` | server.py:554 | `tasks.update_task` (tasks.py:239) | `tasks:write` |
| `archive_task` | server.py:623 | `tasks.archive_task` (tasks.py:417) | `tasks:write` |
| `delete_task` | server.py:644 | `tasks.soft_delete_task` (tasks.py:437) | `tasks:write` |
| `restore_task` | server.py:660 | `tasks.restore_task` (tasks.py:456) | `tasks:write` |
| `set_task_state` | server.py:497 | `tasks.set_state` (tasks.py:326) | `tasks:write` |
| `add_task_tag` | server.py:676 | `tasks.attach_tag` (tasks.py:475) | `tasks:write` |
| `remove_task_tag` | server.py:691 | `tasks.detach_tag` (tasks.py:516) | `tasks:write` |
| `move_task_to_project` | server.py:705 | composed: `tasks.tags_by_task` + `detach_tag` + `attach_tag` | `tasks:write` |
| `assign_task` | server.py:734 | `tasks.assign` (tasks.py:538) | `tasks:write` |
| `unassign_task` | server.py:748 | `tasks.unassign` (tasks.py:564) | `tasks:write` |
| `set_task_schedule` | server.py:1018 | `tasks.set_schedule_fields` (tasks.py:289) | `tasks:write` |
| `task_offer` | server.py:1360 | `coordination_svc.offer_task` (coordination.py:479) | `tasks:write` |
| `task_claim` | server.py:1373 | `coordination_svc.claim_task` (coordination.py:525) | `tasks:write` |
| `task_decline` | server.py:1385 | `coordination_svc.decline_task` (coordination.py:580) | `tasks:write` |

GAPs (tasks domain, no MCP tool):

- **Search / filter beyond `state_id`**: `list_tasks` accepts only an
  optional `state_id`. The SPA `/board` filters by tag / project /
  assignee / due-date window / archived / billable / `offered` /
  text. The service layer already has the building blocks
  (`tasks.list_tasks` at tasks.py:189 plus `tasks.tags_by_task` at
  tasks.py:221), but the tool surface forces the agent to over-fetch
  and filter client-side. Expose a `list_tasks` overload (or a
  `search_tasks`) that accepts the same shape as
  `api/src/flow_api/routers/tasks.py` query params. Scope:
  `tasks:read`.
- **Bulk state change**. REST has a batch endpoint
  (`POST /tasks/bulk-state`); the service path uses
  `tasks.set_state` per row. No MCP equivalent. Scope: `tasks:write`.
- **Activity log / audit read**: `services/audit.py` only writes
  (audit.py:17). There is no read tool over `audit_logs`, so the
  agent can't introspect "what changed and when". Either expose a
  read tool here or accept it as outside MCP scope.

## Tags / Projects / Clients

Full read parity with REST. The MCP write surface covers create / patch
on every kind plus tag scoping; **hard delete of a client or a project
(taxonomy.purge_project / purge_client) is intentionally not exposed**
yet (that is the `delete:taxonomy` danger bucket).

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `create_tag` | server.py:133 | `taxonomy.create_tag` (taxonomy.py:83) | `tags:write` |
| `create_client` | server.py:150 | `taxonomy.create_client` (taxonomy.py:113) | `tags:write` |
| `create_project` | server.py:175 | `taxonomy.create_project` (taxonomy.py:296) | `tags:write` |
| `list_tags` | server.py:195 | `taxonomy.list_tags` (taxonomy.py:344) | `tags:read` |
| `list_clients` | server.py:241 | `taxonomy.list_clients` (taxonomy.py:514) | `tags:read` |
| `list_projects` | server.py:249 | `taxonomy.list_projects` (taxonomy.py:526) | `tags:read` |
| `get_tag` | server.py:257 | `taxonomy.get_tag` (taxonomy.py:507) | `tags:read` |
| `update_tag` | server.py:265 | `taxonomy.update_tag` (taxonomy.py:640) | `tags:write` |
| `update_client` | server.py:290 | `taxonomy.update_client` (taxonomy.py:538) | `tags:write` |
| `update_project` | server.py:340 | `taxonomy.update_project` (taxonomy.py:573) | `tags:write` |
| `set_tag_scope` | server.py:374 | `taxonomy.set_tag_scope` (taxonomy.py:466) | `tags:write` |

GAPs (taxonomy):

- **Hard delete client / project**: `taxonomy.purge_client`
  (taxonomy.py:1070) and `taxonomy.purge_project` (taxonomy.py:1027)
  exist and are reachable via REST `DELETE /clients/{id}` and
  `DELETE /projects/{id}`, but no MCP tool. This is the
  `delete:taxonomy` danger scope, opt-in by design. Add
  `delete_client` / `delete_project` tools and gate on
  `delete:taxonomy`.
- **`scopes_by_tag` read**: `taxonomy.scopes_by_tag`
  (taxonomy.py:451) is not callable from MCP; an agent that wants to
  understand which client / project a generic tag is scoped to needs
  to read `task.tag_ids` indirectly. Add a thin `get_tag_scope` tool;
  scope `tags:read`.
- **`find_tag_by_name`**: only used internally; not a real GAP, but
  worth exposing as `find_tag` for agent quoting workflows. Scope:
  `tags:read`.

## Notes

Full surface: every CRUD knob, conversation turn append, command
parsing, TTS, archive / restore. The Proposal A note↔task link is
addressable from both sides (`get_or_create_task_note`,
`create_task_note`, `update_note(task_id=…)`).

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `create_note` | server.py:2585 | `notes_svc.create_note` (notes.py:384) | `notes:write` |
| `list_notes` | server.py:2608 | `notes_svc.list_notes` (notes.py:143) | `notes:read` |
| `get_note` | server.py:2631 | `notes_svc.get_note` (notes.py:171) | `notes:read` |
| `get_or_create_task_note` | server.py:2639 | `notes_svc.get_or_create_work_note` (notes.py:449) | `notes:write` |
| `create_task_note` | server.py:2654 | `notes_svc.create_note_for_task` (notes.py:497) | `notes:write` |
| `update_note` | server.py:2678 | `notes_svc.update_note` (notes.py:282) | `notes:write` |
| `archive_note` | server.py:2713 | `notes_svc.archive_note` (notes.py:326) | `notes:write` |
| `delete_note` | server.py:2734 | `notes_svc.soft_delete_note` (notes.py:346) | `notes:write` |
| `restore_note` | server.py:2750 | `notes_svc.restore_note` (notes.py:365) | `notes:write` |
| `add_note_tag` | server.py:2766 | `notes_svc.attach_tag` (notes.py:201) | `notes:write` |
| `remove_note_tag` | server.py:2781 | `notes_svc.detach_tag` (notes.py:230) | `notes:write` |
| `list_turns` | server.py:2795 | `notes_svc.list_turns` (notes.py:695) | `notes:read` |
| `start_conversation_session` | server.py:2803 | `notes_svc.create_note(kind=conversation)` | `notes:write` |
| `append_message` | server.py:2823 | `notes_svc.append_message` (notes.py:624) | `notes:write` |
| `transcribe_note` | server.py:2840 | `notes_svc.transcribe` (notes.py:563) | `notes:write` |
| `run_command` | server.py:2856 | `notes_svc.run_command` (notes.py:542) | `notes:write` |
| `synthesize_speech` | server.py:2864 | `notes_svc.synthesize` (notes.py:709) | `notes:write` |

GAPs (notes):

- **`gdpr_erase_note`**: `notes_svc.gdpr_erase_note`
  (notes.py:735) is not exposed; only the memory-side
  `memory_erase` is. For full GDPR parity an agent should be able to
  invoke note-side erasure. Scope: `notes:write` (danger-adjacent;
  consider promoting to a `notes:delete` danger scope).
- No bulk operations: each note rename / re-tag is one round-trip.
  Not a service-layer gap, but a tool ergonomic gap.

## Memory

Hybrid retrieval + write + erasure + consolidation + channel admin.
This is one of the better-covered domains because it is the primary
agent surface (retrieval-as-tool, ADR-0016).

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `memory_write` | server.py:2313 | `memory_svc.write_blob` (memory.py:182) | `memory:write` |
| `memory_search` | server.py:2358 | `memory_svc.retrieve` (memory.py:278) | `memory:read` |
| `memory_erase` | server.py:2395 | `memory_svc.gdpr_erase` (memory.py:409) | `memory:write` (danger-adjacent) |
| `memory_consolidate` | server.py:2409 | `memory_svc.consolidate` (memory.py:495) | `memory:write` |
| `memory_delete_blob` | server.py:2432 | `memory_svc.delete_blob` (memory.py:589) | `memory:write` |
| `memory_status` | server.py:2448 | (no service; just `embedder_available()`) | `memory:read` |
| `memory_channels_list` | server.py:2493 | `taxonomy.list_memory_channels` (taxonomy.py:793) | `memory:read` |
| `memory_channel_create` | server.py:2503 | `taxonomy.create_memory_channel` (taxonomy.py:815); platform-admin | `memory:write` |
| `memory_channel_update` | server.py:2521 | `taxonomy.update_memory_channel` (taxonomy.py:852); platform-admin | `memory:write` |
| `memory_channel_delete` | server.py:2547 | `taxonomy.delete_memory_channel` (taxonomy.py:894); platform-admin | `memory:write` |

GAPs (memory):

- **`recompute_tier`**: `memory_svc.recompute_tier`
  (memory.py:462) recomputes the hot/warm/cold tiering for one blob
  and is wired into the re-embedding worker, but not exposed as a
  tool. An agent can't ask "promote this entry". Scope:
  `memory:write`.
- **`get_blob`**: `memory_svc.get_blob` (memory.py:580) is
  reachable only via `memory_search`; if the agent has a blob id from
  an earlier turn it must search again. Add `memory_get_blob`. Scope:
  `memory:read`.
- **`attach_blob_tag` / `detach_blob_tag`** -
  `memory_svc.attach_blob_tag` (memory.py:624) and
  `detach_blob_tag` (memory.py:654) have no tool counterpart.
  Tagging is currently done at write time. Scope: `memory:write`.

## Time tracking

Full timer life cycle plus reports. The Proposal A note↔task linkage
goes through `start_timer(note_id=…)` and `update_time_entry`.

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `start_timer` | server.py:1557 | `time_svc.start_timer` (time_tracking.py:257) | `time:write` |
| `stop_timer` | server.py:1588 | `time_svc.stop_timer` (time_tracking.py:322) | `time:write` |
| `add_time_entry` | server.py:1609 | `time_svc.add_manual_entry` (time_tracking.py:342) | `time:write` |
| `list_time_entries` | server.py:1641 | `time_svc.list_entries` (time_tracking.py:398) | `time:read` |
| `get_time_entry` | server.py:1659 | `time_svc.get_entry` (time_tracking.py:165) | `time:read` |
| `list_running_timers` | server.py:1667 | `time_svc.running_entries` (time_tracking.py:174) | `time:read` |
| `update_time_entry` | server.py:1675 | `time_svc.update_entry` (time_tracking.py:429) | `time:write` |
| `delete_time_entry` | server.py:1725 | `time_svc.delete_entry` (time_tracking.py:534) | `time:write` |
| `time_report` | server.py:1738 | `time_svc.report` (time_tracking.py:582) | `time:read` |
| `time_report_by_task` | server.py:1767 | `time_svc.task_report` (time_tracking.py:874) | `time:read` |

GAPs (time):

- **CSV / spreadsheet export**. REST exposes
  `/time/entries/export.csv` (the entry-level CSV from commit
  51f259c). MCP does not. Add a `time_export_csv` tool that returns
  text. Scope: `time:read`.
- **`running_serial` / `running_for_task`**: internal helpers
  (time_tracking.py:191, 206); a `get_running_serial_timer` tool
  would let an agent answer "what am I tracking right now" without
  scoping by user. Scope: `time:read`.
- Date-window filter on `list_time_entries`: the tool ignores
  `start_from` / `start_to`; the service entry doesn't accept them
  either, so this is a joint gap. The `task_report` already has it,
  but `list_entries` doesn't.

## Calendar (working calendars + holidays)

Full create / list / patch on working calendars; user binding
(`set_user_calendar`).

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `create_calendar` | server.py:853 | `calendars.create_calendar` (calendar.py:230) | `calendar:write` |
| `list_calendars` | server.py:874 | `calendars.list_calendars` (calendar.py:264) | `calendar:read` |
| `add_holiday` | server.py:890 | `calendars.add_holiday` (calendar.py:315) | `calendar:write` |
| `list_holidays` | server.py:904 | `calendars.list_holidays` (calendar.py:340) | `calendar:read` |
| `remove_holiday` | server.py:912 | `calendars.remove_holiday` (calendar.py:356) | `calendar:write` |
| `set_user_calendar` | server.py:926 | `calendars.set_user_calendar` (calendar.py:272) | `calendar:write` |

GAPs (calendar):

- **No "update calendar" tool**: once a calendar is created, weekly
  hours / timezone cannot be patched from MCP (the service does not
  expose an `update_calendar` either; this is a joint GAP, not just
  an MCP omission). Service work: add `calendars.update_calendar` and
  the tool. Scope: `calendar:write`.
- **No "delete calendar"**: same shape, joint GAP. Service-side it
  needs to refuse if `is_default` or any `user_calendar` row binds
  it. Scope: `calendar:write`.
- **Google Calendar subscriptions**: `services/google_calendar.py`
  has the full pipeline (`connect`, `disconnect`, `list_subscriptions`,
  `sync_subscription`, `push_event`) and the REST surface uses it.
  Nothing of this is on MCP. Scope: `calendar:write` for connect /
  disconnect; `calendar:read` for list / sync (with the caveat that
  sync also writes events).

## Events (appointments) — unified into Tasks

Migration 0094 (ADR-0008 addendum) collapsed appointments onto
`tasks` via `start_at` + `duration_minutes`; migration 0097 dropped
the standalone `events` / `event_participants` tables. The four
former tools (`create_event` / `list_events` / `reschedule_event` /
`delete_event`) are gone. AI agents now use:

| Old MCP tool | Replacement |
|---|---|
| `create_event(title, start_at, end_at, participant_ids, ...)` | `create_task(title, start_at, duration_minutes, assignee_id, ...)` + `add_task_participant` per extra invitee |
| `list_events` | `list_tasks` filtered client-side by `duration_minutes != null` |
| `reschedule_event(start_at, end_at)` | `update_task(start_at, duration_minutes)` (the 0095 trigger keeps participants' windows in sync) |
| `delete_event` | `delete_task` |

No-ubiquity is enforced by the GiST EXCLUDE on `task_participants`
(consolidated in 0096): every identity in the appointment — the
assignee mirror + every explicit participant — gets the slot in its
calendar, and an overlapping appointment for any of them is
rejected with `event.overlap`.

### Participants on appointment-tasks

| Tool | Service entry | Scope key |
|---|---|---|
| `list_task_participants` | `part_svc.list_participants` (participants.py) | `tasks:read` |
| `add_task_participant` | `part_svc.add_participant` (participants.py) | `tasks:write` |
| `remove_task_participant` | `part_svc.remove_participant` (participants.py) | `tasks:write` |

`add_task_participant` accepts either `identity_id` (uuid) or
`handle` (resolved through the workspace's identities). Idempotent
on `(task_id, identity_id)`; rejects with `event.overlap` (409)
when the identity already holds an overlapping appointment. The
list always includes the assignee mirror row inserted by the 0096
trigger.

## Dependencies

Full graph + add / remove / list.

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `add_dependency` | server.py:463 | `dependencies.add_dependency` (dependencies.py:63) | `dependencies:write` |
| `remove_dependency` | server.py:807 | `dependencies.remove_dependency` (dependencies.py:105) | `dependencies:write` |
| `list_dependencies` | server.py:793 | `dependencies.list_dependencies` (dependencies.py:130) | `dependencies:read` |
| `graph` | server.py:486 | `dependencies.graph` (dependencies.py:177) | `dependencies:read` |

GAPs (dependencies):

- **Blocked-task projection**: `dependencies.blocked_task_ids`
  (dependencies.py:169) is used by the scheduler but not exposed.
  Useful for "what is currently waiting on something else". Scope:
  `dependencies:read`.

## Workflows

Full CRUD on the workflow / state / transition graph, plus default
promotion and per-project override. Note: in the MCP layer the
`update_workflow` tool does not take `expected_version`, while
`set_project_workflow` does (the service entry enforces optimistic
concurrency on the project profile, not on the workflow definition).

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `list_workflows` | server.py:3201 | `workflow_svc.list_workflows` (workflow.py:73) | (read; no key; see GAP) |
| `workflow_states` | server.py:3209 | `workflow_svc.get_states` (workflow.py:81) | (read; no key) |
| `workflow_transitions` | server.py:3217 | `workflow_svc.list_transitions` (workflow.py:95) | (read; no key) |
| `create_workflow` | server.py:3225 | `workflow_svc.create_workflow` (workflow.py:153) | `workflows:write` (danger) |
| `update_workflow` | server.py:3256 | `workflow_svc.update_workflow` (workflow.py:304) | `workflows:write` |
| `delete_workflow` | server.py:3289 | `workflow_svc.delete_workflow` (workflow.py:253) | `workflows:write` |
| `set_default_workflow` | server.py:3303 | `workflow_svc.set_default_workflow` (workflow.py:219) | `workflows:write` |
| `set_project_workflow` | server.py:3316 | `workflow_svc.set_project_workflow` (workflow.py:398) | `workflows:write` |

GAPs (workflows):

- **Scope catalog has no `workflows:read`**: the catalog only
  defines `workflows:write` (a danger key). The three workflow reads
  currently don't have a clean fit. Either add a `workflows:read`
  key (preferred: three reads is enough surface to warrant it) or
  alias them under `tasks:read` (defensible: a task's state is a
  task fact). Recommend a new key.
- The MCP `update_workflow` skips `expected_version` while the
  service entry accepts none either, so this is consistent across
  layers, but worth noting that "loose" mutation on workflow defs is
  a real concurrency hole (two concurrent updates can lose a state
  reconcile). Tracked separately; not a coverage GAP.

## Comments

Minimal: add + list. No edit / delete.

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `add_comment` | server.py:449 | `tasks.add_comment` (tasks.py:589) | `comments:write` |
| `list_comments` | server.py:762 | `tasks.list_comments` (tasks.py:613) | `comments:read` |

GAPs (comments):

- **No edit / delete**: the service layer also has no
  `update_comment` / `delete_comment` (comments are append-only by
  design, per the activity-log carve-out in FR-1). Not a tool GAP.
- **No threading**: out of scope for v1.

## Attachments

Read + delete only. **Binary upload is intentionally REST-only**, see
the comment block at server.py:2878. Tools exchange JSON and base64
round-trips don't fit the protocol or the multipart size guard.

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `list_attachments` | server.py:2888 | `attachments_svc.list_attachments` (attachments.py:307) | `notes:read` / `tasks:read` (depending on parent) |
| `delete_attachment` | server.py:2918 | `attachments_svc.delete_attachment` (attachments.py:379) | `attachments:write` (danger) |

GAPs (attachments):

- **Upload over MCP**: by-design, but worth flagging: there is no
  mechanism for an agent to push bytes. If we ever lift it, the
  scope is already `attachments:write` (danger because bytes leave
  the workspace boundary at read time). Service entry exists:
  `attachments_svc.add_attachment` (attachments.py:236).
- **Download**: `attachments_svc.read_attachment_bytes`
  (attachments.py:366) exists; same protocol concern. If we add a
  base64 read tool, gate it strictly: `attachments:read` (new key
  needed, it isn't in the catalog yet).

## Budgets

Full CRUD + consumption read.

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `create_budget` | server.py:1820 | `budgets_svc.create_budget` (budgets.py:59) | `budgets:write` (danger) |
| `list_budgets` | server.py:1849 | `budgets_svc.list_budgets` (budgets.py:98) | `budgets:read` |
| `budget_consumption` | server.py:1856 | `budgets_svc.consumption` (budgets.py:159) | `budgets:read` |
| `update_budget` | server.py:1871 | `budgets_svc.update_budget` (budgets.py:106) | `budgets:write` |
| `delete_budget` | server.py:1913 | `budgets_svc.delete_budget` (budgets.py:138) | `budgets:write` |

GAPs (budgets): none on the budget envelope itself. The `budget_id` on
task creation is exposed; the budget→task selection is covered under
"Focus / advisory" below.

## Billing / metering

Full surface for credit balance, manual top-up, idempotent metering,
rate cards and usage ledger reads.

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `billing_balance` | server.py:2204 | `billing_svc.balance` (billing.py:89) | `billing:read` (danger) |
| `grant_credits` | server.py:2211 | `billing_svc.grant_credits` (billing.py:93) | (no key; needs new `billing:write`) |
| `meter_usage` | server.py:2227 | `billing_svc.meter` (billing.py:215) | (no key) |
| `upsert_rate_card` | server.py:2255 | `billing_svc.upsert_rate_card` (billing.py:315) | (no key; ratecards are admin-tier) |
| `list_rate_cards` | server.py:2280 | `billing_svc.list_rate_cards` (billing.py:360) | `billing:read` |
| `list_usage` | server.py:2287 | `billing_svc.list_usage` (billing.py:447) | `billing:read` |

GAPs (billing):

- **No `billing:write` scope**: `mcp_scopes.py` defines only
  `billing:read` (danger). `grant_credits`, `meter_usage`,
  `upsert_rate_card` have no clean scope key. Add a `billing:write`
  danger scope; gate the three mutating tools on it.
- **No `set_storage_rate` / `set_byok_factor` / `list_ledger`
  tools**. `billing_svc.set_storage_rate` (billing.py:366),
  `set_byok_factor` (billing.py:399), `list_ledger` (billing.py:426)
  are reachable via REST admin endpoints but not from MCP. Add a
  read-only `billing_ledger` tool (scope: `billing:read`) at minimum;
  storage / BYOK setters are admin-only and arguably out of MCP scope.

## Dispatch (LLM admission gate)

Full P5 surface: list the queue, approve / deny one request, force a
tick now. All mutating tools are owner-gated inside the service
(`effective_request_role` + `app.current_role` GUC).

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `dispatch_requests_list` | server.py:1420 | `dispatch_loop_svc.list_requests` (dispatch_loop.py:585) | (read; no key; see GAP) |
| `dispatch_approve` | server.py:1431 | `dispatch_loop_svc.approve_request` (dispatch_loop.py:611) | `dispatch:approve` (danger) |
| `dispatch_deny` | server.py:1451 | `dispatch_loop_svc.deny_request` (dispatch_loop.py:658) | `dispatch:approve` (or split `dispatch:deny`?) |
| `dispatch_tick` | server.py:1474 | `dispatch_loop_svc.tick` (dispatch_loop.py:383) | `dispatch:approve` (a tick can spend credits) |

GAPs (dispatch):

- **No `dispatch:read` scope**: the catalog has only
  `dispatch:approve` (danger). Listing the queue is member-level read
  in the service and should not require the danger approval key. Add
  `dispatch:read`.
- **`get_request`**: `dispatch_loop_svc.get_request`
  (dispatch_loop.py:600) is not exposed; agents need to filter the
  list. Add `dispatch_request_get`. Scope: `dispatch:read`.
- The `dispatch_deny` operation is currently lumped under
  `dispatch:approve` (defensible: both are the danger decision
  surface) but worth a comment in the catalog. Consider keeping them
  on the same key for v1.

## Agent runs

The P3 runtime: start, get, list, cancel. Start and cancel are
owner-gated (credit-spending operations).

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `agent_run_start` | server.py:1274 | `agent_runtime_svc.start_run` (agent_runtime.py:342) | `agent_runs:start` (danger) |
| `agent_run_get` | server.py:1293 | `agent_runtime_svc.get_run` (agent_runtime.py:585) | (no key) |
| `agent_runs_list` | server.py:1301 | `agent_runtime_svc.list_runs` (agent_runtime.py:596) | (no key) |
| `agent_run_cancel` | server.py:1316 | `agent_runtime_svc.cancel_run` (agent_runtime.py:549) | `agent_runs:start` (cancel is a control-plane op) |

GAPs (agent runs):

- **No `agent_runs:read` scope**: the catalog only has
  `agent_runs:start`. Reading the run log is a low-risk read. Add
  `agent_runs:read`.
- **`cancel_run` is gated on the same `start` key**: defensible (it
  is the kill switch for an already-paid run) but consider a
  dedicated `agent_runs:cancel` if we ever expose the runtime to
  non-owner roles.

## Executors

Full CRUD via MCP. Mutating ops are owner-gated.

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `executors_list` | server.py:1135 | `executors_svc.list_executors` (executors.py:145) | (no key; see GAP) |
| `executor_create` | server.py:1143 | `executors_svc.create_executor` (executors.py:231) | (no key) |
| `executor_update` | server.py:1183 | `executors_svc.update_executor` (executors.py:301) | (no key) |
| `executor_delete` | server.py:1232 | `executors_svc.delete_executor` (executors.py:338) | (no key) |

GAPs (executors):

- **Executors scope key absent**: `mcp_scopes.py` has nothing for
  executors. Add `executors:read` (low-risk; the schedule plan must
  show its assignments to be useful) and `executors:write` (danger;
  changing `credit_budget` / `credit_rate_per_hour` is a finance
  decision indistinguishable from `budgets:write`). For v1 it is
  defensible to alias executor mutations under `budgets:write`
  (both are credit-policy levers).
- **`ensure_default_agent` / `ensure_workspace_executors`**: these
  are bootstrap services (executors.py:43, executors.py:130), not
  user-facing. No tool needed.

## Focus / advisory (deterministic planner)

The deterministic advisory layer (ADR-0013, FR-13/FR-14): the three
"what can I do now / where am I going / how do I spend this budget"
tools.

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `what_can_i_do_now` | server.py:1926 | `advisory_svc.what_can_i_do_now` (advisory.py:169) | `tasks:read` (combined: tasks + calendar + dependencies; no dedicated key) |
| `errands` | server.py:1959 | `advisory_svc.errands` (advisory.py:226) | `tasks:read` |
| `prioritize_within_budget` | server.py:1987 | `advisory_svc.prioritize_within_budget` (advisory.py:287) | `budgets:read` + `tasks:read` |

GAPs (focus):

- **No "focus mode" tool**: the SPA has a "Focus" surface; MCP has
  no equivalent. Not a service GAP per se, but a missing agent
  affordance: a tool that returns "the current focus task + its work
  note + its running timer" in one round-trip. Compose at the
  service layer (`tasks` + `notes` + `time_tracking`). Scope:
  `tasks:read`.

## Scheduler

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `recompute_schedule` | server.py:1057 | `scheduler.recompute` (scheduler.py:765) | (no write key; see GAP) |
| `get_schedule` | server.py:1089 | `scheduler.get_schedule` (scheduler.py:782) | `schedule:read` |
| `list_schedule` | server.py:1097 | `scheduler.list_schedule` (scheduler.py:790) | `schedule:read` |
| `set_task_schedule` | server.py:1018 (duplicates the task entry) | `tasks.set_schedule_fields` (tasks.py:289) | `tasks:write` |

GAPs (scheduler):

- **No `schedule:write` scope**: `recompute_schedule` is a service
  call that derives the `schedule` rows. The scope catalog has only
  `schedule:read`. Either alias under `tasks:write` (a recompute is
  triggered by, and reflected in, task plans), or add
  `schedule:recompute`. Recommend `tasks:write`: recompute is
  derived and idempotent; the user-visible mutation is on task
  pins.
- The unassignable / dispatch-gap diagnostic is folded into the
  `recompute_schedule` return; no separate tool needed.

## Invoices

Issuance pipeline only: issuer profile, draft, line, transmit, credit
note, SdI receipt ingestion. **No invoice list / read / draft-update
exposed from MCP.**

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `set_issuer_profile` | server.py:2952 | `invoice_svc.{get_default_issuer_profile,create_issuer_profile,update_issuer_profile}` (invoice.py:164/184/241) | `invoices:write` (danger) |
| `create_invoice` | server.py:3008 | `invoice_svc.create_draft` (invoice.py:379) | `invoices:write` |
| `add_invoice_line` | server.py:3024 | `invoice_svc.add_line` (invoice.py:507) | `invoices:write` |
| `transmit_invoice` | server.py:3049 | `invoice_svc.transmit` (invoice.py:955) | `invoices:write` |
| `invoice_credit_note` | server.py:3060 | `invoice_svc.create_credit_note` (invoice.py:1004) | `invoices:write` |
| `ingest_sdi_receipt` | server.py:3076 | `invoice_svc.ingest_receipt` (invoice.py:1077) | `invoices:write` |

GAPs (invoices): this is the **least-covered domain** relative to its
REST surface:

- **No `list_invoices`**: `invoice_svc.list_invoices`
  (invoice.py:1116) exists. Scope: would need an `invoices:read` key
  (catalog has only `invoices:write`).
- **No `get_invoice` / `list_lines`**: `invoice_svc.get_invoice`
  (invoice.py:363), `list_lines` (invoice.py:629). Same gap.
- **No `update_draft` / `update_line` / `delete_line` /
  `delete_draft`**. `invoice_svc.update_draft` (invoice.py:455),
  `update_line` (invoice.py:549), `delete_line` (invoice.py:586),
  `delete_draft` (invoice.py:645). All reachable via REST. Scope:
  `invoices:write`.
- **No `mark_paid`**: `invoice_svc.mark_paid` (invoice.py:1044).
  Scope: `invoices:write`.
- **No preview / XML / PDF**: `invoice_svc.get_preview`
  (invoice.py:1197), `get_xml_preview` (invoice.py:1207),
  `render_pdf` (invoice.py:1231). These return artifacts (XML / PDF
  bytes), so the MCP exposure shape is open (base64? text-only XML?).
  Recommend XML/preview as JSON, PDF stays REST-only.
- **Issuer-profile management is reduced**. REST has list /
  create / update / set-default / delete / set-conservation; MCP has
  only an idempotent `set_issuer_profile` upsert.
  `invoice_svc.list_issuer_profiles` (invoice.py:141),
  `set_default_issuer_profile` (invoice.py:275),
  `delete_issuer_profile` (invoice.py:299),
  `set_conservation_adhesion` (invoice.py:332) all need tools if
  the agent has to manage the fiscal identity. Scope:
  `invoices:write`.
- **Scope catalog gap**: add `invoices:read` (non-danger).

## Email

Account registration, sync, message list, task creation, send + reply.

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `create_email_account` | server.py:2046 | `email_svc.create_account` (email.py:84) | (no key) |
| `list_email_accounts` | server.py:2076 | `email_svc.list_accounts` (email.py:76) | (no key) |
| `sync_email_account` | server.py:2083 | `email_svc.sync_account` (email.py:254) | (no key) |
| `list_email_messages` | server.py:2105 | `email_svc.list_messages` (email.py:379) | (no key) |
| `email_to_task` | server.py:2119 | `email_svc.email_to_task` (email.py:408) | `tasks:write` (the mutation is a task creation) |
| `send_email` | server.py:2138 | `email_svc.send_message` (email.py:452) | (no key; sending leaves the boundary) |
| `reply_email` | server.py:2161 | `email_svc.reply_to_message` (email.py:488) | (no key) |

GAPs (email):

- **No `email:*` scope keys**: the catalog has nothing for email.
  Send / reply are outbound (data leaves the workspace) and should
  be a danger scope: add `email:send` (danger). Account /
  read / sync should be `email:read` and `email:write`. The mass of
  this surface (7 tools) probably warrants the three keys.
- **No `update_account` / `delete_account` / `set_secret`** -
  `email_svc.update_account` (email.py:128), `delete_account`
  (email.py:190), `set_secret` (email.py:160), `sync_all_accounts`
  (email.py:341). Scope: `email:write` (account rotation /
  deletion) once the key exists.
- **No `get_message`**: `email_svc.get_message` (email.py:397) is
  not exposed; agents pull the list then filter. Scope:
  `email:read`.

## Notifications / recurrences / reminders

The cyclic / outbound layer. Set per-channel pref, dispatch the
pending queue, materialize recurrences, scan reminders.

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `set_notification_pref` | server.py:3095 | `notif_svc.set_pref` (notifications.py:51) | (no key) |
| `dispatch_notifications` | server.py:3118 | `notif_svc.dispatch_pending` (notifications.py:138) | (no key) |
| `create_recurrence` | server.py:3126 | `notif_svc.create_recurrence` (notifications.py:228) | `tasks:write` |
| `spawn_due_recurrences` | server.py:3149 | `notif_svc.spawn_due` (notifications.py:286) | `tasks:write` |
| `scan_reminders` | server.py:3164 | `notif_svc.scan_reminders` (notifications.py:354) | `tasks:write` |

GAPs (notifications):

- **No `notifications:*` scope**. Add `notifications:read` (for the
  pref / reminder lists) and `notifications:write` (for
  pref-setting). Dispatching is a control-plane op; consider
  rolling it under `notifications:write` or making it a worker-only
  call (already is, this tool is escape-hatch).
- **No `list_prefs`**: `notif_svc.list_prefs`
  (notifications.py:87) exists; an agent that wants to surface "your
  current notification settings" can't. Scope:
  `notifications:read`.
- **No `list_reminders` / `add_reminder` / `remove_reminder`
  /  `list_notifications`**. `notif_svc.list_reminders`
  (notifications.py:436), `add_reminder` (notifications.py:452),
  `remove_reminder` (notifications.py:486), `list_notifications`
  (notifications.py:508). All reachable via REST, missing from MCP.
  Scope: `notifications:read` for reads, `notifications:write` for
  the add/remove pair.
- **Telegram link**: `services/telegram_link.py` has the full
  flow (`create_link_code`, `get_link_status`, `unlink`); nothing
  on MCP. Scope: `notifications:write` (telegram-link is a
  delivery-channel decision).

## Auxiliary

| Tool | Server line | Service entry | Scope key |
|---|---|---|---|
| `ping` | server.py:108 | (none; returns `flow-core <version>`) | (unscoped; liveness probe) |

---

## Cross-cutting gaps

These are **whole subsystems** present in `flow_core/services/` that
have **zero MCP surface**. Whether they should is a design call, not
just an oversight:

- **Auth (`services/auth.py`)**: `signup`, `login`, `login_mfa`,
  `verify_email`, `request_password_reset`, `reset_password`,
  `revoke_token`. Out of MCP scope by definition (the agent comes in
  *with* a token).
- **MFA setup (`services/mfa.py`)**: `setup`, `activate`,
  `disable`, status. Same shape as auth. Probably out of MCP scope.
- **Memberships (`services/memberships.py`)**: `list_members`,
  `add_member`, `set_member_role`, `remove_member`. The single-user
  v1 build does not need these; multi-user workspaces would. Scope
  (when added): a new `members:read` / `members:write` family;
  `members:write` is owner-gated.
- **Agent tokens / AI assistants (`services/agent_tokens.py`,
  `services/ai_assistants.py`)**: token mint, assistant CRUD,
  scope binding. These configure *the MCP transport itself*, so
  they are arguably out of MCP scope (chicken-and-egg). Keep
  REST-only.
- **Coordination read-helpers
  (`services/coordination.py:incoming_for_context`,
  `_human_recipients`)**: internal; only `list_handoffs`,
  `offer_task`, `claim_task`, `decline_task` are exposed. Adequate.
- **Advisory backups (`services/audit.py`)**: write-only by design.
- **Conservation / SdI side channels (`services/sdi*` and
  `services/email.access_token_for`)**: internal to invoice /
  email pipelines; not user-facing.

## Scope catalog mapping (consolidated)

| Scope key | Category | Tools it should gate (today's MCP surface) |
|---|---|---|
| `tasks:read` | read | `list_tasks`, `get_task`, `list_comments` (alias), `task_handoffs_list`, `what_can_i_do_now`, `errands`, `prioritize_within_budget` (+ `tasks:read` slice) |
| `tasks:write` | write | `create_task`, `update_task`, `archive_task`, `delete_task`, `restore_task`, `set_task_state`, `add_task_tag`, `remove_task_tag`, `move_task_to_project`, `assign_task`, `unassign_task`, `set_task_schedule`, `email_to_task`, `task_offer`, `task_claim`, `task_decline`, `create_recurrence`, `spawn_due_recurrences`, `scan_reminders` |
| `time:read` | read | `list_time_entries`, `get_time_entry`, `list_running_timers`, `time_report`, `time_report_by_task` |
| `time:write` | write | `start_timer`, `stop_timer`, `add_time_entry`, `update_time_entry`, `delete_time_entry` |
| `tags:read` | read | `list_tags`, `list_clients`, `list_projects`, `get_tag` |
| `tags:write` | write | `create_tag`, `create_client`, `create_project`, `update_tag`, `update_client`, `update_project`, `set_tag_scope` |
| `notes:read` | read | `list_notes`, `get_note`, `list_turns`, `list_attachments` (when `note_id` is passed) |
| `notes:write` | write | `create_note`, `get_or_create_task_note`, `create_task_note`, `update_note`, `archive_note`, `delete_note`, `restore_note`, `add_note_tag`, `remove_note_tag`, `start_conversation_session`, `append_message`, `transcribe_note`, `run_command`, `synthesize_speech` |
| `memory:read` | read | `memory_search`, `memory_status`, `memory_channels_list` |
| `memory:write` | write | `memory_write`, `memory_consolidate`, `memory_delete_blob`, `memory_erase`, `memory_channel_create`, `memory_channel_update`, `memory_channel_delete` |
| `calendar:read` | read | `list_calendars`, `list_holidays` (appointments live on `tasks` since mig 0094; use `tasks:read`) |
| `calendar:write` | write | `create_calendar`, `add_holiday`, `remove_holiday`, `set_user_calendar` (appointments on `tasks`; use `tasks:write`) |
| `schedule:read` | read | `get_schedule`, `list_schedule` |
| `comments:read` | read | `list_comments` |
| `comments:write` | write | `add_comment` |
| `dependencies:read` | read | `list_dependencies`, `graph` |
| `dependencies:write` | write | `add_dependency`, `remove_dependency` |
| `budgets:read` | read | `list_budgets`, `budget_consumption` |
| `budgets:write` | danger | `create_budget`, `update_budget`, `delete_budget` |
| `attachments:write` | danger | (none today; only `list_attachments` + `delete_attachment` exist; `delete_attachment` reasonably belongs under this key) |
| `workflows:write` | danger | `create_workflow`, `update_workflow`, `delete_workflow`, `set_default_workflow`, `set_project_workflow` (+ the three workflow reads need a new `workflows:read` key) |
| `delete:taxonomy` | danger | (none today; gates the not-yet-exposed `delete_client` / `delete_project` tools) |
| `dispatch:approve` | danger | `dispatch_approve`, `dispatch_deny`, `dispatch_tick` (+ `dispatch_requests_list` needs a non-danger `dispatch:read`) |
| `agent_runs:start` | danger | `agent_run_start`, `agent_run_cancel` (+ `agent_run_get` / `agent_runs_list` need an `agent_runs:read`) |
| `invoices:write` | danger | `set_issuer_profile`, `create_invoice`, `add_invoice_line`, `transmit_invoice`, `invoice_credit_note`, `ingest_sdi_receipt` (+ the missing list / get / update / delete / mark-paid tools, once added) |
| `billing:read` | danger | `billing_balance`, `list_rate_cards`, `list_usage` |

## Recommended scope catalog additions

To get full coverage of the existing MCP surface without leaving
unkeyed tools, the catalog needs the following keys (none today):

- `workflows:read` (non-danger; for `list_workflows`,
  `workflow_states`, `workflow_transitions`).
- `dispatch:read` (read; for `dispatch_requests_list` and the missing
  `dispatch_request_get`).
- `agent_runs:read` (read; for `agent_run_get`, `agent_runs_list`).
- `executors:read` / `executors:write`, the latter danger (changing
  `credit_rate_per_hour` is a finance lever indistinguishable from
  `budgets:write`).
- `billing:write` (danger; for `grant_credits`, `meter_usage`,
  `upsert_rate_card`, and the missing `set_storage_rate` /
  `set_byok_factor` once added).
- `invoices:read` (non-danger; for the missing `list_invoices`,
  `get_invoice`, `list_lines`, preview reads).
- `email:read`, `email:write`, `email:send`: three keys because
  send / reply leave the workspace boundary and warrant their own
  danger bucket.
- `notifications:read`, `notifications:write`: covers prefs /
  reminders / telegram link.

Until these are added, the safe default in the gate is "if a tool's
key is missing from the catalog, treat it as **off** for any
non-legacy assistant". That preserves the principle of least
authority and forces the catalog work to land before the gate is
flipped to "enforce".

## What is intentionally out of MCP scope

- **Binary upload / download** for attachments (server.py:2878
  comment). Multipart bytes don't survive JSON-MCP. Keep REST-only.
- **Auth flows** (`login`, `signup`, password reset, MFA setup).
  The agent comes in *with* a token; provisioning is a UI flow.
- **Agent-token / AI-assistant lifecycle**. Configuring the MCP
  transport itself from the MCP transport is a chicken-and-egg
  risk. Keep REST-only.
- **Admin-mode elevation primitives**: sudo / X-Admin-Mode is a
  per-request HTTP header pattern; MCP gates the equivalent on
  `users.is_admin` capability inline (see the channel-admin gate at
  server.py:2481).
