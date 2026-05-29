# ADR-0034 — Humus policy in the LLM walk

Status: Accepted (ratified 2026-05-29)
Date: 2026-05-27
Tracks: task `e980e5f9-6028-4ae0-9850-9564c1e8a602`
Depends on: ADR-0029 (note garden), ADR-0030 (bge-m3), ADR-0032 (`garden_classify`), ADR-0039 (decomposition pipeline — the humus producer)

## Context

The garden manifesto closes the cycle: archived material *decomposes*
into smaller atoms (claims, definitions, examples, decisions, season
syntheses), and that humus feeds the next thought. Today the
decomposition writes the artefacts (distillation notes, pattern
notes) but no retrieval path uses them differently from a regular
note. The closed loop is conceptual, not actual.

This ADR decides when and how humus enters the prompt of an LLM
walk (focused or free wander, see task `5bf31b63`).

## Decision

### What qualifies as humus

A node is humus if it carries the `humus_flag` (set at write time by
the decomposition pipeline, ADR-0039) AND satisfies one of:

- explicit subtype: `note.humus_kind in {distillation, pattern,
  season}` — note this is the dedicated `humus_kind` column (ADR-0039),
  NOT `note.kind`; a distillation is still a plain `text` note for every
  other purpose,
- structural: the node was archived more than 30 days ago AND has
  at least 3 incoming `references` links AND PageRank in the top
  20% of the workspace. Tie-break for the top-20% cut is defined in
  ADR-0039 (higher PageRank, then older archival date, then id).

The flag is materialised on a `humus_flag` column on `notes` so
retrieval doesn't recompute the predicate. The decomposition pipeline
sets the explicit case at write time; the nightly worker materialises
the structural case; users can toggle it manually (rare).

### When humus enters context

Two surfaces, two policies:

- **Focused walk** (seeded retrieval, RAG inside the assistant):
  humus is a *parallel* retrieval source, late-fused into the final
  list via RRF (ADR-0005) with a small boost. The cap is 30% of the
  context budget; the rest is live notes. The boost is fixed (k=10
  in RRF), not learned, so humus quality cannot game the loop.

- **Free wander** (drift exploration in the mindmap tab): humus is
  treated as a first-class wander source. The wander biases toward
  high-centrality humus nodes (PageRank * humus_flag) because the
  point is precisely to surface the long-fermented atoms.

### How retrieval sees humus

Late-fusion RRF over two ranked lists:

```
list_live: top-N from the current corpus
list_humus: top-N from the humus subset
final = rrf_fuse(list_live, list_humus, k=10, boost_humus=1.0)
```

Anti-monoculture (ADR-0033) still applies on the *final* list, so
even a strong humus result loses ground to a diverse candidate from
a different cluster.

### Transparency

Every result that came via the humus source carries a small leaf
icon and a tooltip ("from archived material, surfaced 27 days
later"). The assistant footer in chat lists the humus contributions
separately from live notes, so the user can audit.

### Calibration against runaway

- Hard cap: 30% of slots in the focused walk, 50% in free wander.
- Soft cap (ADR-0033 M5): if humus's accept rate is materially
  lower than live notes for 14 days, the cap auto-drops by 5
  percentage points.

## Consequences

- The decomposition pipeline (`4a718dc4`) has a new contract:
  populate the `humus_flag` and the subtype. Without it this ADR is
  noop.
- The RRF cost goes up by one retrieval source (negligible: the
  humus index is a subset of the existing index).
- New telemetry (ADR-0035): humus accept-rate, humus-only walks,
  humus age distribution.
- Risk: the user reads the leaf icon and dismisses humus on
  principle ("old stuff"). Mitigation in copy: "fertiliser, not
  archive".

## Alternatives rejected

- **Always-on boost on the main retrieval list.** Indistinguishable
  from "score-rerank everything by recency", which produces the
  exact monoculture anti-pattern we're trying to avoid.
- **Opt-in toggle only.** Loses the cycle: the user won't toggle it
  and the humus stays unread. Rejected.
- **Similarity-driven gating only.** Reasonable but harder to
  reason about and to debug; the explicit cap is simpler and the
  thermostat already adjusts for under-use.

## Resolved questions

**Should humus be visible in `garden_classify` as a suggestion source?**
Yes. Humus participates in the `links` signal of ADR-0032 like any
other node, but a candidate surfaced *because* it is humus is tagged
with provenance `humus` (the same leaf marker used in the walk), so the
UI can show "a long-fermented atom you might link to" and the user can
tell exploitation of fresh links from resurfacing of humus. No separate
signal enum is needed — provenance on the existing `links` signal is
enough, and it keeps the anti-monoculture rescoring (ADR-0033) operating
over one unified candidate list rather than two.
