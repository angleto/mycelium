# ADR-0048 — Fuel-table retention: pruning is hygiene, not metabolism

Status: Accepted (2026-07-17)
Task: 68052297 (from the memory-system audit, note `bdc62d7a` §2.2).
Relates to: ADR-0016 (tier = latency, never retention), ADR-0035 (garden
sensors), ADR-0041 (§12 the system is never destructive on its own),
migration 0081 (`retrieval_trace` + `note_edge_usage`).

## Context

The memory system writes several append-mostly telemetry tables on every
use — the "fuel path": `retrieval_trace` (one content-free row per
non-probe search, the raw signal of the edge-usage fold),
`search_clicks` (the recall@k sensor's click log), `activity_log` (the
audit spine), `classification_feedback`, `event_outbox`.

The 2026-07-17 audit found a default-config hole: `retrieval_trace`
writing defaults ON, but its ONLY pruning (the 90-day window delete)
lived inside `refresh_edge_usage`, which rides the garden sweep behind
`garden_loop_enabled=False`. A stock deployment — production included —
therefore accumulated an unbounded write-only table, invisible to the
sensors (nothing watched storage). The 90-day window itself was a code
constant with no ADR: an undecided absence by the project's own
documentation discipline.

Why this is not a §12 violation to fix: ADR-0041's "never destructive on
its own" protects *originals* — the user's thinking. Fuel rows are
derived, content-free telemetry whose entire value is consumed by
aggregation inside a declared window ("aged-out traces can never
contribute again", `services.edge_usage`). Deleting them past the window
destroys nothing the system promised to keep.

## Decision

1. **A dedicated `fuel_retention` worker job runs UNCONDITIONALLY**
   (registered like `revisions_retention`, daily cadence), pruning:
   - `retrieval_trace` rows older than
     `retrieval_trace_retention_days` (default 90), **floored at
     `EDGE_USAGE_WINDOW_DAYS`** so retention can be raised but can never
     undercut the aggregation window — the job deletes only rows the
     fold could never read again, whether or not the fold ever runs;
   - `search_clicks` rows older than `search_click_retention_days`
     (default 365 — a longer window because clicks feed the recall
     sensor and a future active-learning loop).

   The fold's own retention delete stays, and now honours the SAME
   effective window (`max(setting, EDGE_USAGE_WINDOW_DAYS)`), so an
   operator who raises retention above 90 days is not silently undercut
   on garden-enabled deployments; the two deletes are idempotent with
   each other. Pruning is *hygiene*, not metabolism: it must not depend
   on `garden_loop_enabled`, exactly as revision GC does not.

2. **`activity_log` is append-only BY DECISION**, not by omission: it is
   the accountability spine (audit actor_kind, coactivity input, the
   garden "what changed" timeline) and its rows are the system's own
   episodic record of actions. No TTL. If its growth ever becomes a real
   cost, the answer is archival/partitioning, not deletion — revisit
   with a new ADR. `classification_feedback` and `event_outbox` likewise
   keep their current (append/consumed) contracts; they are
   learning-ledger and outbox respectively, not fuel.

3. **A `trace_backlog` garden sensor** (ADR-0035 style: value, no floor,
   "show never judge") counts `retrieval_trace` rows older than the
   effective window — the storage blind spot made visible. Healthy ≈ 0;
   a growing reading means the pruner is not running.

## Consequences

- Default deployments stop accumulating unbounded traces; prod gets the
  same fix on the next rollout with no migration (pure code + config).
- The retention windows are per-deployment settings with safe floors;
  the constants are now decided here rather than implied by code.
- The sensor makes the failure mode observable; per "show never judge"
  it does not alert.

## Alternatives rejected

- **Scheduling the edge-usage fold unconditionally instead.** The fold
  is metabolism (it mutates graph weights) and stays deliberately behind
  `garden_loop_enabled`; hygiene must not smuggle the metabolism in.
- **TTL on `activity_log`.** Rejected above: audit is not fuel.
- **A Postgres-side policy (pg_cron / partition drop).** More moving
  parts than a worker job the codebase already has a template for, and
  invisible to the app's sensors/tests.
