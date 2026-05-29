# ADR-0032 — `garden_classify(node_id)`: the proposal engine

Status: Proposed
Date: 2026-05-29 (rewrites the 2026-05-27 contract sketch; grounds it in
shipped Phase-1 code and decides the maturity-automation policy)
Tracks: task `3f11faca` (spec), parent `f6c9977f` (Fase 3)
Depends on: ADR-0029 (note garden), ADR-0031 (edge weights / centrality)
Informs / informed by: ADR-0030 (bge-m3), ADR-0033 (anti-monoculture),
ADR-0034 (humus), ADR-0036 (event bus), ADR-0037 (online learning)

## Context

`garden_classify` is the keystone of Fase 3. It is the single
server-side surface that, for a node in the graph, proposes the
enrichments a person otherwise does by hand: which tags to add, which
notes to link, whether the idea has matured. Without it every other
adaptive feature (the learning loop, anti-monoculture, the
"fewer manual operations" north star, the automatic evolution of an
idea from seed to mature) has nothing to consume. The adjudication
framework (ADR-0027) and the agent runtime (ADR-0025) already run
proposals; `garden_classify` is the missing *generator*.

The substrate it stands on is **already shipped** and must be used as
it is, not as a future ideal:

- `graph.compute_note_edge_weights` — v1 `note_edge_strength`, on-demand,
  soft-OR of per-kind link weights + Adamic-Adar over shared generic
  tags. Co-activity (Proposal A) is not yet a contributor.
- `graph.compute_pagerank` / `compute_personalized_pagerank` — global +
  seeded centrality, on-demand, deterministic power iteration.
- `link_prediction.suggest_links_for_note` — returns ranked
  `LinkSuggestion{note_id, score, signals, rationale}`, already
  excludes the already-linked set, already damps hubs.
- `note_links.set_maturity` (manual, audited) and the
  `worker/garden.py` sweep calling `tick_maturity_transitions`
  (auto `seed→growing`, `growing|mature→dormant`, `dormant→growing`).

And what is **not** there yet, which v1 must therefore not assume:

- Leiden clusters: no schema, no worker, no column. Cluster suggestion
  is dark until ADR-0031 Phase 2 materialises it.
- bge-m3 embeddings: scaffolded but off (`embed_model_v2` empty); the
  corpus is still e5-small (384d). Embedding-NN signals work but at
  legacy quality.
- A learned link-kind / link-prediction model: the mix is heuristic.
- The event bus (ADR-0036) and personal priors (ADR-0037): spec-only.

The earlier sketch of this ADR assumed all four existed. This revision
phases the contract so v1 ships on today's substrate and degrades
gracefully, and v2 lights up signals as the substrate fills in.

## Decision

### The three non-negotiable constraints (they shape every choice below)

1. **Proposal, not imposition.** `garden_classify` is **read-only**: it
   never mutates a node. Mutation happens only through a separate,
   user-attributable, reversible `apply` event. The one carve-out is
   automatic maturity promotion, and it is allowed *only* because it is
   reversible and label-only (see below) — it is an action that can be
   undone, never a deletion or an overwrite of content.
2. **Transparency.** Every suggestion carries `signals_used` and a
   `rationale`: which features produced it and why. No black box.
3. **Reversibility.** Every `apply` and every auto-promotion is an event
   in `classification_feedback` (ADR-0037); the priors and the maturity
   state are restorable to a prior snapshot.

A suggestion kind that cannot satisfy all three is out of scope,
however useful.

### Endpoint contract

```
GET  /garden/classify/{node_id}?kinds=tags,links,maturity[,cluster]
POST /garden/classify            # batch, body {node_ids:[...], kinds:[...]}
POST /garden/apply               # the mutating, reversible counterpart
```

`garden_classify` response (one entry per node):

```jsonc
{
  "node_id": "uuid",
  "node_kind": "note | task",          // v1 scope; note_part/blob deferred
  "suggestions": {
    "tags":     [{ "tag_id": "...", "confidence": 0.0, "rationale": "..." }],
    "links":    [{ "target_id": "...", "link_kind": "references|atom_of|...",
                   "confidence": 0.0, "rationale": "..." }],
    "maturity": { "value": "growing|mature", "confidence": 0.0,
                  "rationale": "...", "auto_apply": false },
    "cluster":  null                   // v2; null + reason in signals_used
  },
  "signals_used": ["tag_adamic_adar", "linkpred_ppr", "pagerank_pct",
                   "manual_degree"],   // only signals actually active
  "model_version": "garden-classify-v1",
  "generated_at": "ISO-8601"
}
```

`apply` request: `{node_id, suggestion_type, suggestion_value, action}`
where `action ∈ {accept, reject, override, ignore}`. On `accept`/`override`
it performs the mutation through the existing services
(`taxonomy.add_tag`, `note_links.link_notes`, `note_links.set_maturity`),
writes the `classification_feedback` row (ADR-0037), and returns the new
entity version. `apply` is idempotent on `(node_id, suggestion_type,
suggestion_value)` and never crosses the (org, project) boundary.

### Node-kind scope (v1)

- **note**: tags + links + maturity. Full, because the note↔note graph
  and note maturity live in the substrate today.
- **task**: tags only. Tasks carry tags and are indexed into memory
  blobs (`task_search`), so tag cooccurrence is available; tasks are not
  in the note-link graph, so links/maturity do not apply.
- **note_part, blob**: deferred. They have no independent identity in
  the link graph; classify them via their parent note.

### Signals (v1, heuristic, on today's code)

- **tags** — `graph.adamic_adar_pair` over the candidate tag's
  co-occurrence with the node's existing generic tags (rare tags weigh
  more, the discount is the ADR-0033 M1 mechanism, native here), plus a
  nearest-neighbour vote over the per-tag medoid embedding (e5-small
  now, bge-m3 when ADR-0030 lands — graceful, same code path).
- **links** — wrap `link_prediction.suggest_links_for_note`. v1 leaves
  `link_kind` as the conservative default `references`; the MLP that
  predicts the *kind* from `(edge_weight, shared_tags, embedding)` is v2.
- **maturity** — see the dedicated decision below.
- **cluster** — **null in v1**, with `"cluster": "leiden_not_materialised"`
  recorded in `signals_used` so the reader knows it is dark, not empty.
  Lights up in v2.

### Maturity: from suggestion to *automatic* promotion

This is the decision that the rest of the file builds toward, because it
is what makes ideas evolve on their own (the explicit Fase 3 goal:
the system advances the idea so the person does not have to).

**Vocabulary.** The lifecycle states are the DB enum `NoteMaturity`:
`seed → growing → mature → dormant`. The botanical icon set
(seed/sprout/branch/leaf/compost, ADR-0029 visual identity) is
presentation only; it is not the state machine.

**What is already automatic.** `worker/garden.py` advances the
*freshness* axis with no human action: `seed→growing` on a touch within
`seed_to_growing_days`, `growing|mature→dormant` after
`growing_to_dormant_days` untouched, `dormant→growing` on the next touch.
Freshness is a function of time, and time is observed automatically.

**What is deliberately manual today, and why it must change.**
`growing→mature` is the one transition the worker refuses
(`garden.py`: "mature is never set automatically, the user decides").
That refusal is correct for a *freshness* worker — maturity is not a
freshness property, it is a *value/centrality* property, and a clock
cannot measure value. The fix is not to let the clock promote, it is to
give the promotion a real signal. That signal is exactly what
`garden_classify` computes.

**The mature-candidacy signal.** A note is a candidate for `mature` from:

- `pr_pct` = the percentile of the note's global PageRank in the
  workspace (from `compute_pagerank`), and
- `deg` = its manual note↔note link degree (human curation invested in
  it), saturating at the ADR-0029 threshold of 3:
  `deg_term = min(1, deg / 3)`.

```
conf_mature = min(pr_pct, deg_term)        # AND semantics: both must hold
```

`min` encodes a deliberate change from ADR-0029's looser
"≥3 links OR top-decile PageRank". A hub with zero human links is not
mature *thinking*, it is just popular; a heavily linked note nobody else
reaches is a private silo. Auto-promotion, which mutates state without a
click, must clear the stricter bar (both). The looser OR condition is not
discarded — it feeds the *proposal* tier at lower confidence.

**Two-tier apply policy** (the core product choice):

| `conf_mature` | behaviour |
|---|---|
| ≥ `MATURE_AUTO` (default **0.85**) | **auto-promote** `growing→mature`: the garden worker performs it, writes the audit row, emits a `classification_feedback` event with `action='auto'`. Reversible, label-only. |
| `[MATURE_SUGGEST, MATURE_AUTO)` (default 0.65) | surface a one-tap proposal chip; no state change until the user accepts. |
| `< MATURE_SUGGEST` | not surfaced. |

Recommendation and default: **auto-promote on the high tier**. The north
star is "progressively fewer manual operations"; a proposal that always
needs a tap is still a manual operation. Auto-promotion honours the three
constraints because (1) it is reversible and per-workspace disablable, so
it is an undoable action, not an imposition; (2) it is transparent — the
audit row reads `matured: PageRank p93, 5 manual links` — ; (3) `mature`
is a non-destructive label that changes ranking/visual weight, never
content, and `set_maturity` is already audited. Workspaces that prefer
confirmation set `MATURE_AUTO = 1.0`, which collapses the high tier into
the proposal tier.

**Demotion stays conservative.** `garden_classify` never auto-demotes
`mature→growing/seed`. The only downward move remains the worker's
freshness `→dormant` (recoverable on touch). Quality, once recognised,
is not silently revoked; if a note should drop back the person does it by
hand, or the next promotion cycle simply does not re-assert it.

**Reversibility mechanics.** Each auto-promotion is a
`classification_feedback` row (ADR-0037) and a maturity audit entry; the
daily prior snapshot plus the per-note maturity audit make
`POST /garden/learning/rollback {to}` restore a prior maturity landscape
deterministically.

### Confidence calibration

v1 confidence is a fixed, documented, monotone transform of the raw
signal score (conservative by design); it is *not* learned. The defaults
(`tags ≥ 0.55`, `links ≥ 0.45`, `maturity` tiers above) are educated
guesses. v2 replaces the fixed transform with the per-signal accept/reject
calibration of ADR-0037, and the per-user personal prior re-ranks
candidates without ever pruning one below the structural floor.

### Anti-monoculture seam

v1 already carries ADR-0033 M1 natively (the Adamic-Adar discount is in
the tag and link scores). M2 (MMR diversity), M4 (ε-greedy cross-cluster
exploration) and M5 (biodiversity thermostat) are a **post-processing
wrapper over the v1 candidate list** and land with v2 (M4/M5 need Leiden
clusters). The wrapper consumes `garden_classify` output and re-ranks; it
does not change this contract.

### Sync vs async, caching

- **Sync** `GET /garden/classify/{node_id}`: soft 800 ms budget. The
  underlying graph computations are already sub-second on a typical
  workspace (<1k notes, <10k edges), so v1 computes on demand and caches
  the result in a small `garden_classification` table keyed by
  `(node_id, model_version)` with a 24 h TTL. No materialised view, no
  bus dependency.
- **Async** `POST /garden/classify`: a job id; the nightly worker
  backfills classifications for new/changed nodes. Interactive agents
  must not call the batch form.

### MCP surface

One tool `garden_classify(node_id, kinds?)` and one `garden_apply(...)`,
returning/accepting the shapes above. An LLM agent shares its user's
quota and its RLS scope; it can *propose and apply with attribution*,
which is precisely how an agent becomes an inhabitant of the graph
(Fase 3) without escaping the audit trail.

### Rate limiting

Per-user 60 sync/min and 5 batch/day; per-org 600 sync/min; an LLM agent
draws from its user's pool (no separate quota).

### Audit log

Every response is persisted with `signals_used` and the suggestions; the
row id is the `event_id` reused by ADR-0036 (bus) and ADR-0037 (learning)
once those land. Until then the row stands alone and is sufficient for
v1 transparency and rollback.

### Testability

- Fixture: a ~50-node seed garden with hand-curated expected tags, links
  and maturity verdicts (including at least one clear auto-mature
  candidate and one near-miss that must stay a proposal).
- Oracle: a golden-file snapshot of the v1 output. Accept-rate drift > 10%
  fails the regression suite.
- Determinism: PageRank/PPR are deterministic; the only stochastic signal
  (ε-greedy, v2) takes an injected RNG seed in tests.

## Consequences

- One surface to consume and one to mutate through; new signals slot into
  `signals_used` without breaking callers.
- v1 is buildable now with **no new infrastructure**: it composes shipped
  `graph` + `link_prediction` + `note_links` services and adds the
  classify/apply endpoints, the cache table, and the maturity-promotion
  branch in the garden worker.
- Ideas evolve without intervention: the freshness axis was already
  automatic; the value axis (`growing→mature`) now is too, under
  reversible, transparent control. This is the concrete mechanism behind
  "the person focuses on creativity, not on bookkeeping".
- The SPA's local heuristics (mindmap maturity glyph, tag chips, bloom
  halo) keep working; `garden_classify` is additive.
- e5-small caps embedding-signal quality until ADR-0030's bge-m3 cutover;
  classify degrades gracefully (structural signals carry v1).
- Confidence defaults are guesses until ADR-0037 calibrates them; the
  conservative thresholds bias toward under-suggesting, which is the safe
  failure mode for a system that mutates maturity automatically.

## Alternatives rejected

- **Per-signal endpoints** (`/suggest-tags`, `/suggest-links`, …).
  Composing four calls into one decision pushes complexity into every
  client. Rejected.
- **Keep `growing→mature` manual** (the status quo). It contradicts the
  Fase 3 goal of automatic idea evolution and keeps a per-note manual
  operation forever. Rejected; replaced by the reversible auto-promotion
  above.
- **Auto-promote on `OR` (the ADR-0029 criterion).** Too eager for an
  unattended mutation: it would mature popular-but-uncurated hubs and
  curated-but-peripheral silos. Kept as the *proposal*-tier trigger, not
  the auto-commit trigger.
- **Make classify mutate directly** (no separate `apply`). Collapses the
  proposal/imposition boundary and loses the clean place to write the
  feedback event. Rejected.
- **Block v1 on Leiden + bge-m3 + the learning loop** (the old sketch).
  Couples the keystone to three unshipped systems and stalls all of
  Fase 3 behind them. Rejected in favour of phased degradation.
- **No audit log.** Without `signals_used` we cannot explain a
  suggestion, cannot drive the learning loop, and cannot roll back an
  auto-promotion. Rejected as a violation of transparency and
  reversibility.

## Open questions

- Where to expose `MATURE_AUTO` / `MATURE_SUGGEST` and the ADR-0033 knobs:
  a per-workspace garden-tuning advanced panel (lean) vs the main settings
  page. The everyday user should never see them.
- Whether `task` maturity is meaningful at all (tasks have their own
  workflow state machine). v1 says no; revisit if users want a "this task
  encodes a durable lesson" signal that would itself feed decomposition.
