# ADR-0014 Personal domain and budget envelope

Status: accepted. User's choice (modeled in v1).

## Context

The advisory queries (ADR-0013) require tasks to carry real-life
attributes: duration (already present), location,
context/preconditions, necessity, and a monetary cost with a budget for
expense prioritization (e.g. home expenses). The v2 domain was oriented
to work/clients/invoicing.

## Decision

Model now, reusing the existing taxonomy (ADR-0003), with no parallel
entities:

- Task attributes: `monetary_cost?`, `location?`, `necessity`
  (must/should/nice); context/preconditions via `generic` tags with a
  namespace convention (e.g. `ctx:requires-computer`, `place:hardware`).
- A project can be personal (a non-billable project): same tag/profile
  model, no separate domain.
- `Budget`: an org-scoped envelope per period (month/quarter/year/
  custom) and category, with an allocatable amount and currency; tasks
  attach via `budget_id`; consumption/residual computed by the service
  layer.
- Selection within budget is a **deterministic constrained selection**
  (priority/value-density knapsack, must-have first), not an LLM
  judgment.

## Consequences

- A single conceptual model (tag/project) covers work and personal
  life; no duplication.
- Personal budgets still live inside the user's org (an org can be a
  single-person workspace): consistent with multi-tenant and RLS.

## Alternatives rejected

- A separate, parallel "personal" domain: duplicates taxonomy and
  isolation with no benefit.
- Cost/budget as free, untyped metadata: prevents correct constrained
  selection and currency/period checks.
- Deferring budgets post-v1: the user explicitly asked to model them
  now (the third advisory archetype).
