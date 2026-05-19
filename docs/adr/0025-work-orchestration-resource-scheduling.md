# ADR-0025: Resource-aware scheduling and human/LLM work orchestration

Status: Accepted (design; phased delivery)
Date: 2026-05-19

## Context

`/schedule` runs a Critical Path Method (CPM) pass over the task
dependency DAG (earliest/latest start-finish, slack, critical path).
CPM assumes **infinite resources**: a task starts as soon as its
predecessors finish. Reality:

- A **person** is a unit-capacity, serial resource bound by a working
  calendar; switching tasks has a real cost.
- An **LLM agent** is a K-parallel resource, bounded by provider
  rate limits and a credit budget; tasks may overlap only when
  parallelizable (no shared resource, no precedence between them).

So the schedule must become **resource-constrained (RCPSP)**. When the
bottleneck is the resource and not logical precedence, the binding
constraint is the **critical chain** (CCPM), not the critical path;
today's critical path is therefore optimistic.

The broader ambition: Flow is a personal-work orchestrator that should
also orchestrate LLM agents — run them in parallel at maximum
efficiency, delegate the right number of tasks, and pass coordination
messages LLM↔LLM, LLM↔human, human↔human.

## Decision

Locked with the product owner (2026-05-19):

1. **Human capacity model**: working calendar (reuse the internal
   `/calendars` working-hours/holidays) + an explicit per-executor
   **context-switch cost** applied between heterogeneous consecutive
   tasks. Humans are serial (one active task).
2. **Objective**: **multi-policy**, selected per `recompute` run
   (e.g. `fastest` = min makespan, `cheapest` = meet deadlines at min
   LLM credit, `balanced`, `throughput`). The scheduler reports
   projected makespan AND projected credit cost so policies are
   comparable. This leverages the existing billing meter (LLM work is
   already metered — ADR-0019).
3. **Scope**: the full arc including the **agent execution runtime**,
   delivered in verifiable phases (below). Optimal RCPSP is NP-hard;
   we use a deterministic priority-rule heuristic (serial/parallel
   schedule-generation scheme), consistent with the "deterministic
   schedule" contract — no opaque solver.

### Architecture (strata, each built on existing Flow systems)

- **Executor** (new first-class): `kind` human|llm_agent. Human →
  a User + a working calendar + `context_switch_cost`. LLM agent →
  provider/model binding, `max_parallel`, `credit_budget`, capability
  tags, rate profile. Tasks already carry `executor_kind`,
  dependencies and effort estimates.
- **Scheduler**: CPM pass (priorities/slack) → resource-constrained
  list scheduling honouring precedence + capacity (humans serial on
  calendar with switch penalty; LLM pool = per-agent `max_parallel` +
  global credit budget + rate) under the selected policy. Outputs a
  feasible per-executor timeline, the resource-aware critical chain,
  and projected makespan + cost.
- **Admission control**: keep each agent's in-flight ≤ `max_parallel`
  and Σcost ≤ budget; a Little's-law-informed WIP target ("the right
  number to delegate"); capability matching task→agent.
- **Agent execution runtime**: a scheduled `llm_agent` task triggers
  an agent run in `flow_worker`, driven against Flow's **own MCP
  control surface** (already complete) scoped to the task context
  (note↔task link, memory channel). Metered (billing), bounded
  (budget/steps/tool-allowlist), pausable/killable. Results return as
  task state + a note/memory artifact.
- **Coordination**: a handoff is a typed message bound to a DAG edge.
  On completion, the producer's artifact + message is delivered to
  each dependent task's executor — injected into an LLM's context, or
  delivered to a human via the existing notification + note↔task +
  memory substrate. Same primitive for LLM↔LLM, LLM↔human,
  human↔human (contract-net delegation over a shared blackboard =
  Flow memory).
- **Governance**: autonomous LLM execution on real data needs
  guardrails from day one — credit budget caps, tool allowlist via
  MCP scoping, human-in-the-loop approval gates for sensitive tools,
  and it must respect the effective-role sudo model (the RBAC choke
  point fixed this session).

### Phasing (each phase ships green independently)

- **P1**: Executor model + resource-aware scheduler (human
  calendar+switch cost, LLM pool) + multi-policy objective; `/schedule`
  shows a feasible plan + projected makespan/cost + critical chain.
- **P2**: Executor registry + admission-control dispatch (assignable
  plan respecting per-agent WIP/budget/capability; no execution yet).
- **P3**: Agent execution runtime over MCP in `flow_worker` (one LLM
  task end-to-end: spawn → work → artifact → complete; metered,
  bounded, killable).
- **P4**: Coordination/handoff protocol on notifications + note/memory
  (LLM↔LLM, LLM↔human, human↔human); contract-net delegation.
- **P5**: Closed loop (dispatch → execute → reschedule on
  completion/variance) + approval gates + UI.

## Consequences

- `/schedule` becomes truthful under finite capacity; the displayed
  critical chain reflects resource contention.
- New executor/scheduling tables + a scheduler engine rewrite; the
  CPM pass is retained as the heuristic input, not the final answer.
- Builds on systems already in the repo: `executor_kind`, the
  scheduler service, the MCP control surface, the billing meter,
  memory channels, note↔task links, notifications — coherent, not
  greenfield.
- This is a multi-phase epic sequenced AFTER the current UX/bugfix
  pipeline (the in-flight tags/memory/time backend pass → frontend
  sweep → E2E → push) and after, or interleaved with, the Gmail/
  Telegram connectors epic (those activate the email/telegram memory
  channels; orchestration is independent of them).
- Estimate is deliberately not given as a single number; each phase
  is scoped and gated on its own (ruff/mypy/pytest/E2E green).
