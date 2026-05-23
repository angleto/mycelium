# ADR-0028: Identity-first addressing and explicit task ownership

Status: Proposed
Date: 2026-05-23
Relates to: ADR-0025 (orchestration), ADR-0015 (RLS roles), ADR-0002
(multi-tenant), ADR-0017 (i18n). Supersedes the Stage B compromise
documented in `models/task.py:108-114` (the "kill Executor" refactor
#21).

## Context

Task assignment in Flow today is sprawled across five partially
overlapping mechanisms (cfr. mapping in `flow_core/services/tasks.py`,
`scheduler.py:287-302`, `dispatch_loop.py:252`,
`agent_runtime.py:135-146`):

1. `task.executor_kind` (enum `human | llm_agent`): routes the
   scheduler.
2. `task.executor_user_id` (uuid → `users.id`): authoritative input
   for the human-calendar resolver.
3. `task.assignee_handle` (string `@handle`): Stage B addition (#21),
   resolved against `users.handle` OR `ai_assistants.handle` at write
   time.
4. `TaskAssignee` (M:N user × task): "collaborators"; the scheduler
   falls back to the first assignee when `executor_user_id` is NULL.
5. `coordination.offer_task` / `claim_task` + `task.offered` flag:
   contract-net negotiation.

Three deeper levels lurk underneath:

- **Intent** (who *should* work): the user expresses it.
- **Plan** (who the solver *picked*): `Schedule.assigned_executor_id`.
- **Execution** (who is *running now*): `AgentRun.executor_id`.

A fourth concern is missing as an explicit field: **accountability**
(who answers if the task fails or escalates). Today it is implicit:
either the first `TaskAssignee` by uuid order, or the workspace owner.
This is exactly the field the new audit `actor_kind` work (commit
`a4ede7a`) and the adjudication framework (ADR-0027) both want to
notify and surface, and both currently must infer.

A fifth annoyance: handles are polymorphic (user OR ai_assistant) but
addressed through two separate tables. Every lookup is a two-step
disjunction (try `users.handle`, fall back to `ai_assistants.handle`),
and the SPA's task picker has to know both surfaces.

The `#21` Stage A→B→C plan promised to drop `executor_kind` /
`executor_user_id` once `assignee_handle` reached parity. Stage C is
the right moment to close every loose end at once rather than leaving
Stage C-minimal accountability still implicit and the polymorphism
still string-typed.

## Decision

### D1. Introduce `identities` as a tenant-scoped lookup table

A new first-class table:

```
identities(
  id              uuid PK,
  org_id          uuid NOT NULL (RLS-scoped per ADR-0002),
  kind            text NOT NULL CHECK (kind IN ('user', 'ai_assistant')),
  handle          text NOT NULL,
  user_id         uuid NULL REFERENCES users(id) ON DELETE CASCADE,
  ai_assistant_id uuid NULL REFERENCES ai_assistants(id) ON DELETE CASCADE,
  -- exactly one of user_id / ai_assistant_id is non-NULL
  CHECK ((user_id IS NOT NULL) <> (ai_assistant_id IS NOT NULL)),
  UNIQUE (org_id, handle)
);
```

One row per `(org x user-membership)` and one per `ai_assistant`.
Users with 3 workspaces get 3 identity rows; that is a feature, not a
bug: handles can diverge per-workspace if the user wants. Lifecycle
managed by the service layer: `auth.signup` /
`workspace.add_member` create the identity for the new (org, user)
pair; `ai_assistants.create` creates one for the assistant.

`org_id NOT NULL` makes RLS uniform (no special-casing global rows in
the policy). Same posture as every other tenant table.

### D2. Task points to an Identity, plus an explicit Owner

```
ALTER TABLE tasks
  ADD COLUMN owner_id    uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  ADD COLUMN assignee_id uuid     NULL REFERENCES identities(id) ON DELETE SET NULL;

DROP COLUMN tasks.executor_kind;
DROP COLUMN tasks.executor_user_id;
DROP COLUMN tasks.assignee_handle;
```

- **`owner_id`** is the accountability axis. Always a real user, never
  an AI. `ON DELETE RESTRICT`: you cannot delete a user that still
  owns tasks; you must transfer ownership first. The default at
  creation is `created_by` (already on the task). Reassignable via an
  explicit operation, audited.
- **`assignee_id`** is intent: who should work on it. Polymorphic via
  Identity, so a single FK replaces the `handle` string lookup. NULL
  means "unassigned"; `ON DELETE SET NULL` lets the identity drop
  cleanly without orphaning the task (e.g. an assistant being
  retired).

`TaskAssignee` (M:N collaborators) stays. Its semantics narrow to
"people involved beyond the assignee", which is what it was used for
in practice when `executor_user_id` was the primary.

### D3. The executor model survives — at the resource layer

`Executor` (calendar, switch cost, capability tags, credit budget,
max parallel) stays as **resource model** for the scheduler /
admission control. Its FK to `users.id` is unchanged for human
executors; it does not point to `identities` because the scheduler's
need is resource semantics, not addressing semantics.

The `Schedule.assigned_executor_id` (plan output) and
`AgentRun.executor_id` (execution-time) stay where they are. ADR-0025
P1-P5 is not touched.

### D4. Kind comes from `identities`, not from a duplicate on task

Wherever the scheduler / dispatch / agent_runtime today read
`task.executor_kind`, they instead JOIN to `identities` via
`assignee_id` and read `identities.kind`. NULL `assignee_id` defaults
to `kind='human'` for routing (an unassigned task is implicitly a
human-pool task, same as the pre-refactor default).

### D5. MCP / SPA surface

Two new MCP tools:

- `set_task_owner(task_id, owner_id | owner_handle)` (owner-gated;
  the new owner must be a member of the workspace).
- `set_task_assignee(task_id, assignee_id | assignee_handle | null)`
  (assignee can be cleared; assignee_handle resolves against
  identities under the current org).

The existing `assign_task` (the M:N collaborator add) keeps its
semantics and is renamed in docs but the MCP name stays for
backward-compat. Old tools that took `assignee_handle` accept either
the handle (resolved to an Identity) or an `assignee_id` directly.

SPA: the task card shows the assignee as the primary chip (today) and
the owner as a secondary chip next to it. UI refresh kept minimal in
M1; deeper layout is a follow-up.

### D6. Phasing

1. **0084**: `identities` table + backfill (one row per existing
   membership + one per existing ai_assistant). RLS enabled. Service
   `identities.py` with `ensure_for_user`, `ensure_for_ai_assistant`,
   `lookup_by_handle`.
2. **0085**: `task.owner_id` + `task.assignee_id` columns +
   backfill. Owner = `created_by` for every existing task. Assignee
   = identity matching the existing `assignee_handle` (or NULL).
3. **Refactor**: `services/tasks.py`, `scheduler.py`,
   `dispatch_loop.py`, `agent_runtime.py`,
   `api/routers/tasks.py`, MCP tools, tests.
4. **0086**: drop `task.executor_kind`, `task.executor_user_id`,
   `task.assignee_handle`. Now the schema is consistent.

Each migration ships independently green; the refactor commit between
0085 and 0086 is reviewable on its own.

## Consequences

- Three concepts cleanly separated: addressing (Identity), ownership
  (`owner_id`), resourcing (Executor + Schedule + AgentRun).
- Polymorphic assignee becomes a single FK; SPA / MCP picker query
  one table.
- Notify-on-escalation has a real target (`owner_id`), no more
  inference.
- `assignee_handle` string-FK soft becomes `assignee_id` proper FK
  (referential integrity at the DB level).
- Cost: ~15 file edits in the refactor commit, three migrations, +1
  table. ADR-0025 untouched.
- Risk: the refactor straddles core/api/mcp/worker; pre-flight (ruff,
  mypy, pytest) is the only safety net. Mitigation: commit per stage,
  each stage green.

## Alternatives considered

- **Stage C-minimal**: only complete #21 (drop the mirror columns,
  keep `assignee_handle` as string FK soft). Rejected: leaves
  accountability implicit, leaves polymorphism string-typed. Saves
  half a day at the cost of half-fixing both concerns.
- **Polymorphic association on `task` directly** (`assignee_kind` +
  `assignee_id`, no Identity table). Rejected: classic ORM
  anti-pattern. No referential integrity (the uuid does not FK to a
  table). Every JOIN becomes branching, every audit query becomes
  conditional.
- **Global (non-tenant-scoped) Identity** with a partial unique
  index. Rejected: non-uniform RLS (special-case global rows in
  every policy), inconsistent with the rest of the schema. The
  per-(org x user) replica is cheap (a user has 1-3 workspaces in
  practice).
- **Owner = `actor_id` at creation** (whoever opens the
  `tenant_session`). Rejected: an agent_run that creates a task on
  behalf of a user would set the owner = the human anyway because
  RLS still attributes `actor_id` to the human; but conceptually we
  want it to mean "the user, not the actor". `created_by` is the
  right field (already on the task).
