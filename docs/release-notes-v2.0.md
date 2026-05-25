# v2.0 release notes

User-facing summary of what's new on the `v2.0` line (the current
default branch) relative to the retired v1.x line. The v1.x branches
and tags are no longer published; the v2.0 line carries all forward
development. For commit-level detail, browse the `v2.0` history.

## Migrations: single squashed baseline

The Alembic chain that had grown to 104 incremental revisions across
v1.x is collapsed into one baseline (`0001_baseline.sql`). New installs
run a single migration instead of replaying the full history; existing
v1.x deployments keep working (the baseline mirrors the post-v1.0
schema, the squash is transparent to running databases at the head
revision). See `docs/migrations.md`.

## Italian e-invoicing: passive cycle

The SDI integration is now bidirectional. The active (outbound) cycle
that v1.x already shipped is joined by a **passive (inbound) router**:
`POST /sdi/notification` accepts SdI-delivered XML, validates it,
records it into `received_invoices`, and rejects malformed bodies with
a 400 (no more 500 on XMLSyntaxError). A dedicated `flow-sdi-inbound`
image is built alongside backend / worker / frontend in CI.

## Tasks

- **Per-task checklist** as a second tab on the task detail. Items are
  indexed in search and exposed over MCP (CRUD tools mirror the REST
  endpoints).
- **Eisenhower importance/urgency are now mandatory axes** (default
  Low/Low on creation); `priority` is *always* derived from them,
  client- and server-side. No more "priority drift" between API and
  SPA.
- **MoSCoW necessity**: `Necessity.nice` renamed to `Necessity.could`
  (must / should / could / wont).
- **Filter bar consolidation**: date group + segmented identity +
  `TagPickerGrid` (the chip-grid selector previously only on
  `/notes` / `/memory`) + distinct bulk-action strip. Scope filter no
  longer hides undated tasks when Date focus is off.
- **Bots facet** (humans / bots) matches the card badge, not just the
  assignee identity.
- **Task detail remembers active tab per task** in `localStorage`
  (so reopening a task with an open Checklist tab keeps that tab).

## Notes

- **Promote button** on notes and a "N tasks" chip on the note row.
- A single note can derive **multiple tasks** (each task keeps its
  `note_id` provenance link).

## Identity, actors, agent auth

- **Assignee picker** heals stale identities, prettifies legacy
  handles, and shows the actor's `display_name` on the chip. Bare MCP
  tokens still surface AI authorship correctly.
- **Self-assign** and **AI assistant assignee** are both available
  directly from the picker.
- The REST bearer path now accepts agent tokens (`flow_at_`) in
  addition to user JWTs, so a single endpoint serves both human and
  agent callers.

## Editor / garden

- **LaTeX rendering** in the Tiptap editor (so `$V(0)$` in a task or
  note renders the same as in `/garden`) and in the markdown renderer
  on `/garden`, including GFM tables, fruit titles and focus mode.

## CLI + Neovim plugin

`flow-cli` is now a first-class workspace member with editing surface,
MFA enrolment, browse / search / advisory commands, time report and a
dynamic completion. `flow.nvim` ships Telescope pickers, `:Flow`
commands, writable task / note buffers (`PATCH` on `:w`), live timer
and refreshing pickers. Both surfaces shell out to the same REST API.

`flow-cli` and `flow.nvim` are **tagged in lockstep with Flow itself**
(currently `v2.0.x`): a tag push on this repo mirrors the CLI to the
Homebrew tap and re-tags the nvim plugin mirror, so `brew upgrade
flow-cli` and `:Lazy update flow.nvim` always land on a version that
matches the running backend.

Defaults updated: `flow login` targets `https://flow.xeno.garden/api`
out of the box (no more bare hostname or `localhost:8000`).

## Governance

- License switched to a dual model: **AGPL-3.0-or-later with §7(b)
  attribution** (the "Based on Flow" notice in user-visible locations)
  plus a separate commercial option. Contributions require a DCO
  sign-off **and** the Flow Contributor License Agreement
  (`CONTRIBUTING.md`, `CLA.md`, `NOTICE`).
- "Flow" name and logo are reserved trademarks; forks must rename.

## Operational notes

- **Default branch on the remote is now `v2.0`.**
- v1.0 / v1.1 / v1.2 branches, all v1.x tags, and the v1.x workflow
  runs have been removed from the GitHub remote. Clones with stale
  remote-tracking refs should run `git remote prune origin`.
- CLI / nvim brew formula and Lua spec point at `v2.0` for `head`
  installs.
