# v1.2 release notes

User-facing summary of what shipped across the v1.2.7..v1.2.22 sprint.
For the architectural / API-level detail look at the corresponding
commits on the `v1.2` branch.

## AI assistants (MCP, end-to-end)

A new page lives at **`/settings`** under the "AI assistants" card. It
replaces the old "MCP" guide that asked you to install a local server.

Workflow:
1. Click **New assistant**, give it a label, pick the scopes (read /
   write / danger split, default = everything except danger).
2. The reveal card shows the **client secret exactly once**, copy it.
3. In Claude Desktop / Cursor / any MCP client, add a custom MCP
   server pointing at `https://xenoflow.dev/mcp` with the secret as
   the bearer token (streamable-http transport).
4. The assistant can now call every `@mcp.tool` on Flow on your
   behalf, scoped to the workspace it was minted in.

Rotate the secret with the **Rotate secret** button: a fresh one is
minted, the old one stops working immediately, the assistant identity
stays the same so historical attribution survives. Delete removes the
assistant and cascades the bound bearer (the secret is invalidated).

See `docs/mcp-coverage.md` for the full inventory of tools each
assistant can reach (140 tools across 18 domains).

## Tasks (`/tasks`)

- **Two views, switchable**: list (the existing one) and a **kanban
  board** (new). Columns map to your workflow's states; cards are
  ordered by priority ascending (smaller number = higher priority on
  top). Mobile defaults to list, desktop to kanban; persisted per
  user.
- **Drag and drop**: drag a card to a different column to change its
  state. Illegal transitions (not reachable from the current state in
  your workflow graph) refuse the drop with a snap-back.
- **Per-card timer**: icon-only ⏱▶ / ⏱■ / ⏱▶▶ in the top-right of
  every card. Doesn't interfere with the drag.
- **Hidden columns**: flag a workflow state as Hidden on `/workflows`
  to skip its column in kanban by default; the "Show hidden" toggle
  reveals them.
- **Autosave**: title and description on the task detail save 1s
  after you stop typing (no Save button). Mirrors the notes editor.
- **Filter DSL** in the search input:
  - Free text matches title or tag name.
  - `@tagname` includes a tag, `!@tagname` excludes one.
  - `state:in_progress`, `state:!done` for workflow states.
  - `due:today | tomorrow | overdue | none | +Nd | -Nd | YYYY-MM-DD`.
  - `priority:<=N` (also `<`, `>`, `>=`, `=`).
  - `executor:human | llm_agent | offered`.
  - Spaces are AND, `|` is OR.
- **Tag chips** carry a kind-aware glyph: ▲ client, ■ project, ◆
  memory channel, ● generic. Same tag name on different clients is
  now distinguishable at a glance.
- **Client always present on a task**: creating a task auto-attaches
  the project's client tag server-side (no manual upkeep). A backfill
  migration fixed existing tasks on first deploy.
- **No "Show terminal" toggle**: terminal tasks are always visible in
  the list (the toggle remains in `/graph`, where it makes more
  sense).
- **Card width**: the tasks card no longer caps at 60rem; it uses the
  full available width.

## Time tracking (`/time`)

- **Live rate fallback**: entries with a NULL `rate_snapshot` (logged
  before the client's hourly rate was configured) now use the
  client's current rate at report time. The "Kiwi 24h25m40s · 0 EUR"
  symptom is gone. Historical entries with a real snapshot keep
  their frozen value.
- **Re-snapshot on task move**: editing an entry to point at a
  different task re-resolves the rate, currency, and billable flag
  from the new task's chain.
- **Default range = current month** (1st .. last day) instead of an
  empty unbounded query.
- **Pie chart** now has a tri-state group selector (task / project /
  client), persisted in `localStorage`, default project. The donut
  fetches its own report so changing the pie selector doesn't reset
  the table below.

## Graph (`/graph`)

- **Terminal tasks hidden by default**; "Show terminal" toggle to
  reveal.
- **Per-state filter** (chip-grid driven by the default workflow).
- **Hover tooltips** on every node: full title + state + priority +
  due + tags + AI marker. Click still navigates to `/tasks/{id}`.
- **Colored tag chips** in the filter row (matches the kind-aware
  glyph used in tags everywhere else).

## Clients & projects (`/clients`)

- **Hard-delete archived items**: a Delete-permanently button shows
  up on archived clients/projects. Cascades through tasks, notes,
  time entries, memory blobs, events, attachments (incl. S3 blobs).
  Invoices block the client delete (fiscal records; void them
  first).
- The **sidebar Focus picker** no longer surfaces archived
  clients/projects.

## Workflows (`/workflows`)

- Each workflow state has a new **Hidden** checkbox alongside
  Initial / Terminal. Hidden states are valid for transitions and
  for stored task state, but the kanban board hides their columns by
  default.

## Pomodoro

- The pomodoro lives in the **topbar** (no longer a floating
  bottom-right dock). Shows phase icon + mm:ss + a thin progress
  bar; click to open the controls popover.
- Popover supports **adjust running phase** (`-5m`, `-1m`, `+1m`,
  `+5m`, absolute mm:ss) and inline settings.
- Notification / Sound toggles are now **toggle-pill buttons** with
  the text label baked into the button (no more misaligned native
  checkboxes). Same pattern on `/tasks` "Show hidden" and "Select
  all", and on `/settings` pomodoro card.

## Reusable widgets

- **`TagPickerGrid`** — chip-grid tag selector used by `/memory` and
  `/notes`. Multi-select, optional search/group, glyph + color from
  the existing `TagChip`. Drop-in for any future route that wants the
  visual selector instead of a dropdown.

## Backend / infra

- ESO cleanup: `flow-google` / `flow-telegram` ExternalSecrets are
  no longer applied (the underlying SM entries don't exist yet,
  optional env refs on the backend tolerate the absence). Re-add when
  the provisioning lands per `PENDING-SETUP.md`.
- Migrations 0057 (workflow_states.is_hidden), 0058 (backfill task
  client tags), 0059 (ai_assistants table + agent_tokens.assistant_id
  FK + updated SECURITY DEFINER `authenticate_agent_token`).
- 294/294 service-layer + API tests green at v1.2.22.
- CI now enforces mypy strict on all five packages (flow_core,
  flow_api, flow_mcp, flow_worker, flow_sdi_inbound).

## Still pending (not in this sprint)

- **Voice notes**: recording + transcription pipeline (currently a
  voice note renders as a plain text note).
- **Kill the Executor model**: replace `executors` + `task_assignees`
  with handle-based actor refs on users / ai_assistants /
  llm_embedded. Multi-session refactor.
- **MCP scope enforcement**: the per-assistant scope list is captured
  at auth time but tool calls aren't filtered against it yet.
- **GUI responsive audit**: punch-list exists (sidebar at <820px,
  table conversion, touch targets), execution pending.
