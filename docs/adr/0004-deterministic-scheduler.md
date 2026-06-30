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
