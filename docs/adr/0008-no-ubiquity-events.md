# ADR-0008 No-ubiquity: events entity

Status: accepted. Explicit user requirement.

## Context

The system arranges appointments on the user's behalf. The user does
not have the gift of ubiquity: they cannot have two engagements with
different clients at the same time. WorkingCalendar alone does not
model fixed-time appointments.

## Decision

New `events` entity (appointment): org-scoped, client/project tag,
participants, time-pinned interval, location. Constraint: no
overlapping intervals for the same participant; creating or moving an
appointment that overlaps for the same person is **rejected**.
Appointments are exclusive fixed reservations on a person's timeline
and the scheduler (ADR-0004) places non-delegated human tasks around
them.

## Consequences

- The scheduler treats events as hard constraints; the same person's
  human tasks do not overlap each other nor the appointments.
- A notification is raised on a double-booking attempt.

## Alternatives rejected

- Modeling appointments as tasks: a task is flexible and schedulable,
  an appointment is fixed-time and exclusive; different semantics.
- A non-blocking overlap warning only: the requirement is that the
  system not set concurrent engagements, hence rejection.

## Addendum (migration 0094, 2026-05-24): appointment unified onto `tasks`

The original "different semantics" rationale above was overweighted.
Tasks already carry an `assignee_id`, a billing axis, and a workflow
state — none of which conflict with being an appointment. Maintaining
two parallel entities for what the user perceives as "things on my
day" forced duplicated capture flows, duplicated list/calendar
queries, and a special-case in every cross-cutting concern (today
view, focus, reminders, recurrence, archive).

We unify by adding to `tasks`:

- `start_at timestamptz NULL`,
- `duration_minutes integer NULL`,
- `recurrence jsonb NULL`,
- pairing CHECK: `(start_at IS NULL) = (duration_minutes IS NULL)`,
- positivity CHECK: `duration_minutes IS NULL OR duration_minutes > 0`,
- GiST EXCLUDE constraint
  `no_overlap_event_tasks_per_assignee` keying on
  `(assignee_id, tstzrange(start_at, tasks_event_end(start_at,
  duration_minutes)))`,
- IMMUTABLE helper `tasks_event_end(timestamptz, integer)` so the
  range expression is index-legal (`timestamptz + interval` is STABLE
  in Postgres because a generic `interval` may carry month/year units
  whose length depends on TimeZone; the minute-only helper avoids it).

Semantic mapping:

| Task shape                                  | Role         |
|---------------------------------------------|--------------|
| `start_at IS NULL AND due_date IS NULL`     | Plain todo   |
| `start_at IS NULL AND due_date IS NOT NULL` | Reminder / deadline |
| `start_at IS NOT NULL AND duration_minutes IS NOT NULL` | Appointment |

`due_date` (date) keeps its legacy meaning of *deadline*. Appointments
do not use it; they pin the timestamp via `start_at`. Reminders use
`due_date` alone (no calendar slot to block).

### No-ubiquity in multi-user

Flow is a multi-user product (the current single-user deploy is a
deployment choice, not a system property). The original "no two
appointments at the same time" requirement remains, scoped per
identity: the EXCLUDE constraint keys on `assignee_id` (FK into
`identities`), so two appointments collide only when they share the
same assignee. Two collaborators, or a human user and an AI
assistant, may freely hold overlapping appointments — each one's
calendar is independent.

### What gets dropped, and when

Not in this addendum. The `events` table, its router, service,
scheduler hard-constraint wiring, Google Calendar sync, and MCP tool
surface stay in place. They will be migrated in a follow-up:
internal callers will switch to reading appointments off
`tasks WHERE duration_minutes IS NOT NULL`, after which `events` and
`event_participants` are dropped.

### Out of scope (left for a future ADR if needed)

- N-to-N participants. A task today has a single `assignee_id` and an
  optional set of `task_collaborators`. The latter does **not**
  participate in the no-ubiquity constraint (they collaborate but the
  appointment is "on" the assignee). If a future requirement asks for
  appointments shared exclusively across multiple identities (the
  original `event_participants` model), reintroduce the join table
  on top of the appointment-task and extend the EXCLUDE to span
  participants. We deliberately did not anticipate this here.
