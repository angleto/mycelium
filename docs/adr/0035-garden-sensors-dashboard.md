# ADR-0035 — Garden health sensors dashboard

Status: Proposed
Date: 2026-05-27
Tracks: task `56d6ee64-4471-4c3b-95d3-e1518cfd7bb4`
Depends on: ADR-0032 (`garden_classify`), ADR-0037 (online learning loop)

## Context

The Phase 3 garden must surface *whether it's getting more useful
over time*. Without sensors the loop becomes opaque: we ship
adaptive automation, the user feels something change, and there's
no honest way to discuss it. The principle is "the system shows,
never judges" (manifesto): the sensors are visible, not
gamified, not gated.

## Decision

### Metrics

Seven structural metrics, all derived from existing event streams.
None of them is a vanity metric (no "you wrote N notes this week").

1. **`accept_rate_classify`** — accept / proposed ratio on
   `garden_classify` suggestions. Windows: 7d, 30d. Health floor:
   ≥ 0.40 sustained.
2. **`time_to_first_link`** — median seconds between note capture
   and first incoming or outgoing link (manual or accepted
   suggestion). Lower is healthier mycelium.
3. **`recall_at_k`** — for queries whose user clicked a result
   inside the top-K, the fraction whose clicked node was the top-1
   prediction of a held-out re-rank. Window: 30d. Computed on real
   queries only; never on synthetic probes.
4. **`tag_entropy_local`** — Shannon entropy of generic tags in
   each node's neighbourhood, aggregated per project. Time-series
   per project + global. Floor ≥ 1.2.
5. **`leiden_modularity`** — global modularity score on the
   weighted graph. Tracked as a time-series; the *direction* matters
   more than the absolute value.
6. **`fungal_lag`** — median time between a note's archival and the
   first distillation/pattern emitting from it. Health: < 14 days.
7. **`density_delta_7d`** — change in links-per-node over the last
   7 days, as a percentage. Positive = mycelium thickening.

### API

`GET /garden/health` returns the seven values plus the floors:

```jsonc
{
  "generated_at": "...",
  "window_days": 30,
  "metrics": {
    "accept_rate_classify": { "value": 0.42, "floor": 0.40, "trend_7d": [...] },
    "time_to_first_link":   { "value": 11000, "floor": null, "trend_7d": [...] },
    ...
  }
}
```

A separate `GET /garden/health/timeseries?metric=...&window=...` for
the sparkline data.

### UI

A `/garden/health` page in the SPA: one card per metric, each with

- a tiny sparkline of the last 30 days,
- the current value and the floor (if any),
- one-line plain-language note on what it means.

No traffic-light icons. Below floor renders the value in muted
red, above floor renders in default text colour: it's a reading,
not a verdict. A "what changed" timeline below the cards shows
recent classifier model bumps, learning-loop snapshots, big edits
to the corpus.

### Persistence

All seven metrics are derivable from the event stream + Postgres.
A nightly job recomputes them and writes to a `garden_health_daily`
table; the API reads the latest row + the last 30 days for the
sparkline.

## Consequences

- Sensors are honest only if the learning loop is honest. ADR-0037
  must persist `classification_feedback` with the full signal
  snapshot; without that, `accept_rate_classify` is a half-truth.
- The "show, don't judge" stance is a contract: future PRs that add
  rankings or leaderboards over these metrics are rejected.
- Privacy: all metrics are workspace-scoped (RLS). No
  cross-workspace aggregates surface anywhere.

## Alternatives rejected

- **Gamification (streaks, badges).** Drives the wrong behaviour —
  users write notes to keep the streak alive, not to think.
  Rejected with prejudice.
- **Single "garden health score".** Hides which dimension is
  failing; turns the dashboard into a guilt-trip.
- **Real-time push updates.** The metrics are smoothed over days;
  pushing them live would just add noise.

## Open question

Whether the dashboard should reveal *per-other-workspace* anonymised
benchmarks ("the median garden has X"). Probably no, for two
reasons: cross-tenant data flow is a hard line, and the median
garden does not exist (the variance across users is the whole
product). Defer.
