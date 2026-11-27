# ADR-0004 Deterministic scheduler, not RCPSP

Status: accepted. Corrects an algorithmic inconsistency in an earlier
draft.

## Context

A draft prescribed a "CPM engine that respects calendars and
capacity". That is a contradiction: CPM assumes unlimited resources and
fixed durations; with per-user capacity, multiple assignees and
effort-derived durations it becomes RCPSP, NP-hard, with no
forward/backward pass nor a single well-defined critical path. The
`schedule` schema (one slack, one critical-path flag) was bound to the
wrong object. The user then clarified the real model: tasks they must
do in person are not concurrent; tasks delegated to an LLM can be; and
they do not have the gift of ubiquity (see ADR-0008).

## Decision

No generic RCPSP. Engine = **deterministic logical CPM** over working
calendars (ES/EF/LS/LF, slack, logical critical path, honest because
contention-free) + **deterministic per-person serial placement** of
non-delegated `executor=human` tasks, around fixed appointment-tasks
(tasks with `start_at` + `duration_minutes`, ADR-0008 addendum), with a
stable, deterministic priority rule (a four-level P1..P4 priority,
P1 = highest and scheduled first; then earliest due date, earliest
created, id as the final tie-break). `executor=llm_agent` tasks are off
the human timeline (parallel, precedence only). Required: plan vs
actuals (`remaining_effort_h`, `actual_start`, terminal state),
`schedule_mode`/constraint with drag write-back that survives recompute,
verifiable determinism, summary rollup. The 4 dependency inequalities
are in working time (see functional-requirements FR-4).

## Consequences

- Deterministic, O(V+E), milliseconds on hundreds of tasks on an ARM
  node; slack and critical path well-defined because logical.
- Cross-task overload is handled by per-person serialization (what the
  user wants) plus an overload indicator, not by an opaque solver.

## Alternatives rejected

- RCPSP / MS-Project-style heuristic leveling: heuristic, unstable and
  unexplainable; a poor trade-off for "usable out of the box".
- CP-SAT (OR-Tools): optimizing leveling, valid but optional post-v1
  (not interactive-instant, less explainable output).

## Amendment (2026-11-27): what the dates mean

### Context

Read on the production workspace on 2026-11-26, the plan's dates did not
mean what a reader takes them to mean:

- A parent's own estimate was scheduled on top of its subtasks. The one
  task on the critical path was an epic whose 624 h estimate ran after,
  and independently of, the subtasks that are its work.
- A task with no estimate was scheduled as 0 minutes without saying so:
  1348 of 1605 rows had zero duration.
- A due date was a sort key and nothing else, so no row could say whether
  its task would finish by it.
- Slack had two definitions: the scheduler measured `LS - ES` against the
  end of the whole plan and clamped it at 0, the advisory layer (ADR-0013)
  measured the distance to the task's own deadline.
- 6 rows had a latest start after their latest finish. The backward pass
  computed "0 minutes before" as "add 0 minutes", which snaps forward: a
  zero-length task whose latest finish was 18:00 got a latest start of
  09:00 the next day.
- The resource-aware critical chain of ADR-0025 held 258 tasks and did not
  contain the critical path it was defined to extend.

### Decision

- **A parent is a span.** A task with subtasks in the same plan enters the
  CPM graph as a start node and a finish node: each subtask starts no
  earlier than the start (SS 0), the finish is no earlier than each
  subtask's finish (FF 0). The parent's own estimate is not scheduled. A
  dependency touching the parent attaches to the end it names: FS and SS
  into it hold its start, FF and SF into it hold its finish, FS and FF out
  of it read its finish, SS and SF out of it read its start. On a parent,
  SNET and MSO hold the start of the span; MFO and a manual pin are not
  applied, since its dates are its subtasks'. Its leveled window is the
  span of its subtasks' placements and its projected cost is 0 (the cost
  is its subtasks').
- **A dependency that contradicts the structure is left out and counted.**
  One between a task and its own ancestor, or a chain that leaves a span
  and comes back into it, is not a cycle in the dependency graph, so the
  dependency service accepts it, but it is one once a parent spans its
  subtasks. Such dependencies are dropped in dependency-id order until the
  graph is acyclic, and the summary reports how many
  (`dependencies_ignored`). One bad link does not stop the plan of a
  whole workspace.
- **A missing estimate is unknown, not zero.** The task is still placed as
  a point (there is no other duration to give it), but its row says
  `estimate_missing`, every row whose dates depend on it says
  `dates_lower_bound`, and the summary counts the unestimated tasks. A
  milestone, an appointment and a closed task miss nothing.
- **A due date is a finish constraint.** A task's own latest finish is its
  due date when it has one and the end of the plan when it has none; its
  successors can only bring it earlier. The end of the plan stands in for
  "no date", so it does not bound a task that has one.
- **One slack.** `slack = latest finish - earliest finish` in signed
  wall-clock minutes (`services/slack.py`), shared by the scheduler and
  the advisory layer. Negative slack is a miss by that much, and a task
  with slack at or below 0 is on the logical critical path. Each row also
  carries `due_margin_minutes`: by how much the *leveled* finish meets or
  misses the task's own due date, and the summary counts the misses.
- **Zero working minutes before an instant is the latest working instant
  at or before it** (`WorkCalendar.subtract`), the mirror of `add`, which
  snaps forward. Returning the instant unchanged was tried and rejected:
  a successor that can start at 14:00 after lunch then gave its
  predecessor until 14:00 instead of 13:00, an hour of slack that does
  not exist.
- **The critical chain is removed** (column, API field, MCP field, Schedule
  view column), as amended in ADR-0025.

### Consequences

- The schedule row gains `estimate_missing`, `dates_lower_bound` and
  `due_margin_minutes` and loses `on_critical_chain` (migration 0039).
  `slack_minutes` can now be negative.
- The figures move: slack against a due date instead of the plan end;
  successors of a parent wait for its subtasks rather than for its own
  estimate; every SF dependency now constrains the predecessor against
  the successor's latest finish (the backward pass read its latest start).
- The human serial placement still orders a person's tasks by their CPM
  earliest start and the priority rule, not by the leveled finish of
  their predecessors; only the agent pool waits for a predecessor's
  leveled end. That is unchanged here and is its own task (be2fff43).

### Alternatives rejected

- Copying each dependency of a parent onto every subtask: exact for FS
  into and out of the parent, but it holds every subtask where the span
  needs only its first or its last one (SS out of a parent, FF into it).
- Keeping the parent's estimate as work of its own beside its subtasks:
  it is what produced the 624 h epic, and it counts the same work twice.
- Failing the recompute on a dependency that contradicts the structure,
  as on a real cycle: the dispatch tick would stop for the whole
  workspace on one link that the dependency service accepted.
- Measuring slack in working minutes: the advisory layer has no calendar
  to state it in, and a person reads "late by two days" in calendar time.
