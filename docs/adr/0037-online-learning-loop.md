# ADR-0037 — Online learning loop on garden suggestions

Status: Accepted — implemented v1 2026-06-19 (task `49d24048`); see "Implementation notes (v1)".
Date: 2026-05-27
Tracks: task `756e078e-404f-406e-bae7-f7238c4d5014`
Depends on: ADR-0032 (`garden_classify`), ADR-0033 (anti-monoculture), ADR-0036 (event bus)

## Context

The garden's classifier (ADR-0032) needs to learn from the user's
accept / reject / override decisions over time. The vision manifesto
makes this non-negotiable on three axes: the learning is *event-
sourced* (every step is replayable), *workspace-scoped* (no
cross-tenant leakage), and *reversible* (the user can rewind the
priors to an earlier snapshot if the loop drifts in a direction
they dislike).

## Decision

### Feedback table

```sql
CREATE TABLE classification_feedback (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id            uuid NOT NULL,
  user_id           uuid NOT NULL,
  node_id           uuid NOT NULL,
  suggestion_type   text NOT NULL CHECK (suggestion_type IN ('tag','cluster','link','maturity')),
  suggestion_value  jsonb NOT NULL,      -- tag_id, cluster_id, {target_id, kind}, ...
  action            text NOT NULL CHECK (action IN ('accept','reject','override','ignore')),
  override_value    jsonb,               -- only for action='override'
  ts                timestamptz NOT NULL DEFAULT now(),
  model_version     text NOT NULL,
  signals_snapshot  jsonb NOT NULL       -- copy of the signals that produced this suggestion
);
CREATE INDEX ON classification_feedback (org_id, user_id, suggestion_type, ts DESC);
```

Every row is also an event on the bus (ADR-0036, kind = `commit` or
`reject`), so the bus stream and this table stay coherent — the
table is the materialised projection that the learning loop reads
from, the bus is the transport.

### Update rule

We pick a **logistic-style Bayesian update with explicit
saturation** over a sparse, per-user, per-feature prior matrix.

For each suggestion feature `f` (e.g. "tag X co-occurs with tag Y in
this user's notes"):

```
prior_{t+1}(f) = prior_t(f) + eta * (1 - 2 * sigmoid(prior_t(f))) * sign(action)
```

with `eta = 0.08` for tags, `0.05` for links, `0.03` for cluster,
`0.10` for maturity. Saturation is what prevents the runaway
diagnosed in ADR-0033 M3: the prior asymptotes around ±2.5,
contributing a multiplicative factor on `[exp(-2.5), exp(2.5)]` =
`[0.08, 12.2]` to the base signal.

### Personal vs structural priors

- **Structural priors** (Adamic-Adar over tags, PageRank, Leiden
  clusters) are global per workspace and updated *only* by the
  nightly materialisation worker. The learning loop never writes
  here.
- **Personal priors** are per-user and live in
  `classification_personal_prior` (`user_id, feature_key, value,
  updated_at`). The classifier multiplies the structural baseline
  by the user's personal factor at retrieval time.

The separation guarantees the worker can recompute the structural
layer without conflict.

### Time decay

A nightly job applies geometric decay (`prior *= 0.995`) to every
personal prior older than 30 days. A prior untouched for a year
shrinks to ≈ 0.16 of its peak — old preferences fade without
disappearing.

### Snapshots and rollback

- The bus already persists every event. A snapshot is materialised
  daily into `classification_personal_prior_snapshot` (`user_id,
  snapshot_at, blob`).
- `POST /garden/learning/rollback {to: ISO-8601}` restores the
  closest snapshot before that timestamp, replays bus events up to
  the cut, and writes a new snapshot. The user gets a one-line
  diff ("the system was slightly more biased toward tag X
  yesterday").
- The classifier's behaviour after rollback is fully reproducible.

### Telemetry surfaced to the user

Exposed in the sensors dashboard (ADR-0035):

- `accept_rate_classify` per suggestion type and per signal.
- Hotspots of `action=reject` (the suggestions the user keeps
  declining). Surfacing them lets the user override at the source
  (e.g. "this tag is wrong for me" → soft mute).
- Drift visualisation: which features moved the most in the last
  30 days, shown as a small bar chart in the audit panel.

### Hard constraint: never suppress a legitimate candidate

Personal priors only re-rank; they never prune below the confidence
floor. A candidate that clears the floor on the structural baseline
is always shown, even if the personal prior is strongly negative —
the user picks "don't suggest again" explicitly, never silently.

## Consequences

- The classifier becomes stateful at the user level. Backup story
  must include the priors table.
- A new user has uniform priors (`= 0`). The first month is mostly
  exploration; the dashboard's `accept_rate_classify` is expected
  to be low and the thermostat in ADR-0033 raises ε.
- Privacy: priors are user-scoped, but events on the bus carry
  `actor_id`. Multi-user workspaces should warn that an owner can
  see another member's reject hotspots through the audit panel.
- Performance: per-request prior lookup is a sparse SELECT; we
  cache per (user_id, suggestion_type) for the duration of a
  request.

## Implementation notes (v1, 2026-06-19, task 49d24048)

What shipped (`services/garden_learning.py`, `classification_personal_prior`
table, read-back in `classify_node`, decay in the garden worker):

- **Update rule corrected.** The formula above,
  `prior += eta*(1 - 2*sigmoid(prior))*sign`, has its delta term equal to 0
  at `prior=0` and drives the prior *toward* 0 from both sides — a
  contraction to the origin, not learning, contradicting this ADR's own
  intent ("asymptotes around ±2.5", runaway prevention). The implementation
  uses the intended saturating-growth form
  `prior += eta*sign*(1 - sigmoid(sign*prior))`: aligned feedback moves the
  prior in-sign with diminishing steps (saturates), opposing feedback makes
  a large correction; a hard clamp at ±2.5 is the explicit ceiling. Factor
  `exp(value)` ∈ [0.082, 12.2] as specified.

- **Which features learn (v1).** Only the surfaces where a per-feature prior
  actually re-ranks a candidate list: `tag` (per `tag_id`) and `link` (per
  link target id). `maturity` and `cluster` create no prior — maturity's
  auto-promotion is a *structural* decision that must not be moved by a
  personal prior (the "never suppress a legitimate candidate" constraint),
  and a Leiden cluster id is ephemeral. Their feedback still lands in
  `classification_feedback` for telemetry. `auto` is never a personal signal.

- **Read-back is floor-preserving.** `classify_node(user_id=...)` multiplies
  each candidate's confidence by its prior factor and re-sorts, but the
  confidence floor is checked on the *structural* confidence, so a
  structurally-valid candidate is always shown (re-ranked, never pruned).

- **Reversibility = rebuild-from-log.** The prior table is a deterministic
  projection of the append-only `classification_feedback` log;
  `rebuild_from_feedback` reconstructs it. The daily-snapshot table +
  `POST /garden/learning/rollback` endpoint + the drift/reject-hotspot
  visualisations are deferred to a follow-up (the reversibility *guarantee*
  holds without them).

- **Activation.** Decay runs in the garden worker behind
  `garden_loop_enabled` (OFF in prod today) + `garden_learning_decay_enabled`
  (default ON). Read-back is always live on the interactive classify path.

## Alternatives rejected

- **Plain SGD on a black-box model.** Loses transparency; we cannot
  show the user "why" a suggestion ranked first.
- **No saturation.** The empirical work in the bitvision project
  showed runaway in under three weeks. Rejected with prejudice.
- **Cross-user learning.** Aggregating across users into a shared
  model crosses the tenant boundary and reproduces the central-
  authority anti-pattern the manifesto refuses.

## Resolved questions

**A "diff me against the average user in my workspace" mode for shared
workspaces?** No, for the same reason ADR-0035 rejects cross-workspace
benchmarks: the privacy story does not survive even within one
workspace. "The average member" reveals aggregate behaviour of named
colleagues (a 2-person workspace makes the average a thin disguise for
the other person's priors). The calibration need ("is it the system or
me?") is served instead by the existing reject-hotspot view and the
drift bar chart, both of which are about the user's own history, not a
comparison to others.
