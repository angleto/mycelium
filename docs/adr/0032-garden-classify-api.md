# ADR-0032 — `garden_classify(node_id)` API contract

Status: Proposed
Date: 2026-05-27
Tracks: task `3f11faca-fbf6-488c-96f4-0a15af36787c`
Depends on: ADR-0029 (note garden), ADR-0030 (bge-m3), ADR-0031 (mindmap weights)

## Context

Phase 3 of the garden roadmap ("automatismi adattivi") needs a single
server-side surface that, for any node in the graph (note, task,
note part, blob), proposes structured enrichments: candidate tags,
cluster assignment, candidate links, maturity. Today the SPA mixes
ad-hoc heuristics (Adamic-Adar on tags, degree-based maturity) with
zero audit trail; an MCP client cannot ask "what would the system add
to this node?" without re-implementing the heuristics. The
adjudication framework (ADR-0027) and the executor registry
(ADR-0025) already cover the *running* of suggestions; what is missing
is the *generator*.

Three product constraints follow from the manifesto and the spec:

- **Suggestion, not imposition.** `garden_classify` returns proposals
  with a confidence band; the apply step is always a separate event,
  user-attributable, reversible.
- **Transparency.** Every suggestion carries `signals_used`: which
  features produced it (cluster vote, embedding similarity, tag
  cooccurrence, link prediction). The reader can audit *why*.
- **Reversibility.** The history of `garden_classify` calls and the
  user's accept/reject decisions is event-sourced so the learning
  loop (ADR-0037) can rewind to a known state.

## Decision

### Endpoint contract

```
GET /garden/classify/{node_id}?kinds=tags,cluster,links,maturity
POST /garden/classify (batch, body: {node_ids: [...], kinds: [...]})
```

Response schema (one entry per node):

```jsonc
{
  "node_id": "uuid",
  "node_kind": "note | task | note_part | blob",
  "suggestions": {
    "tags":    [{ "tag_id": "...", "confidence": 0.0..1.0, "rationale": "..." }],
    "cluster": { "leiden_cluster_id": "...", "confidence": 0.0..1.0 },
    "links":   [{ "target_id": "...", "link_kind": "atom_of|references|...", "confidence": 0.0..1.0, "rationale": "..." }],
    "maturity":{ "value": "seed|growing|mature|dormant", "confidence": 0.0..1.0 }
  },
  "signals_used": ["cluster_vote", "embed_sim", "tag_cooc", "linkpred_ppr"],
  "model_version": "garden-classify-v1",
  "generated_at": "ISO-8601"
}
```

Confidence is calibrated on a per-signal basis using the running
accept/reject ratios (see ADR-0037). Thresholds are configurable per
workspace; default cutoffs are conservative
(`tags >= 0.55`, `links >= 0.45`, `maturity >= 0.65`).

### MCP surface

The MCP gateway exposes one tool, `garden_classify(node_id, kinds?)`,
returning the same shape. The batch variant is reserved for the
worker that backfills classifications nightly; an interactive agent
should never call it on its own.

### Signals

- Tags: weighted cooccurrence (Adamic-Adar denominator) +
  embed-NN over the per-tag medoid (bge-m3 1024d, ADR-0030).
- Cluster: Leiden cluster id of the node's PageRank-weighted ego
  network (ADR-0031 phase-2 materialisation).
- Links: PPR seeded at the node, top-K targets above similarity gate,
  link-kind decided by an MLP on (link kind, edge weight, shared
  tags).
- Maturity: rule + recency heuristic (seed: no manual links; growing:
  ≥1 manual link, age ≤ 14d; mature: ≥3 manual links or PageRank in
  the top decile; dormant: no edits in 90d).

### Sync vs async

- Sync (`GET /garden/classify/{node_id}`): single-node, soft 800 ms
  budget. Returns whatever is cached + a recompute if older than
  24h.
- Async batch (`POST /garden/classify`): job id, polled by the
  worker; results land in the `garden_classification` materialised
  view.

### Rate limiting

- Per-user: 60 sync calls / minute, 5 batch jobs / day.
- Per-org: 600 sync calls / minute.
- An LLM agent shares its user's quota (no separate pool).

### Audit log

Every `garden_classify` response is persisted with `signals_used`
and the resulting suggestions; the row id is the `event_id` used by
the bus (ADR-0036) and the learning loop (ADR-0037).

### Testability

- Fixture: a 50-node seed garden with hand-curated expected tags,
  clusters and links.
- Oracle: a single golden-file snapshot. Drift > 10% on accept-rate
  fails the regression suite.

## Consequences

- One API to consume, one schema to evolve. New signal sources slot
  into `signals_used` without breaking callers.
- The SPA's local heuristics (mindmap node maturity, tag chips,
  bloom halo) keep working unchanged; `garden_classify` is additive.
- The cost of the bge-m3 embedding pipeline now matters: a new node
  must be embedded before classification yields full quality. The
  classify endpoint degrades gracefully (returns tag/cluster
  suggestions while the embedding is pending).
- Confidence calibration is a moving target; without the learning
  loop (ADR-0037) wired up, the defaults are educated guesses.

## Alternatives rejected

- **Per-signal endpoints** (`/garden/suggest-tags`, etc.). Composing
  four calls into one decision is the consumer's job; we'd be moving
  complexity into every client. Rejected.
- **Async-only**. Sync UX (chip suggestions on hover) needs sub-second
  responses. Worker-only would force the SPA to cache aggressively
  and would block MCP agents from interactive use.
- **No audit log**. Without `signals_used` we cannot debug why a
  suggestion exists, and we cannot drive the learning loop honestly.
  Rejected as a violation of "transparency, not magic".
