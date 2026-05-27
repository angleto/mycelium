# ADR-0031: Mindmap layout + edge-weight model

Status: Accepted (v1 shipped)
Date: 2026-05-27
Relates to: ADR-0029 (note garden ecosystem), ADR-0028 (identity-first
addressing), ADR-0005 (hierarchical memory). Implements the design
described in Flow tasks 7e99c724 (layout v1), 4467acb4
(`note_edge_strength`), 8c0a8f08 (PageRank / PPR / Leiden), 5bf31b63
(Walk LLM + Mindmap tab), 56d80038 (garden iconography).

## Context

The garden tab "mindmap" renders the workspace's note graph. Until
v1 of this ADR landed, the layout was a deterministic
cluster-on-a-ring (notes grouped by primary tag, untagged on the
outer ring) and edge thickness was a fixed per-kind value. Both
choices were correct as a starting point but actively obscured the
two properties the user wants to see:

- **Some notes are hubs.** They sit in multiple lines of thinking;
  they should gravitate toward the centre of the canvas, with a
  size cue that reflects their centrality.
- **Some links are stronger than others.** Two ideas joined by an
  `atom_of` AND by three shared generic tags are more glued together
  than two ideas joined by a single `references`. The line thickness
  should communicate this without changing the kind's visual
  identity (a dashed `references` must stay dashed).

The substrate for these signals lives in three places: the typed
note↔note link table (kinds: `atom_of`, `references`, `replies_to`,
`supersedes`), the tag system (a tag is "evidence" two notes share
context), and -- eventually -- the activity / time graph (Proposal A:
notes that co-appear on a task or a time slot are practically
linked even when no manual edge exists).

## Decision

A two-axis encoding, both derived from a single scalar **edge
weight** `w ∈ [0, 1]` per pair of notes:

- **Position**: a force-directed layout pulls heavy edges short and
  injects a centripetal gravity proportional to each node's
  centrality. Hubs settle at the centre; isolated leaves drift to
  the periphery; clusters self-organise without a hard-coded ring.
- **Stroke thickness**: `strokeWidth = base_for_kind + 0.6 · w^0.7`.
  The kind's signature (solid trunk for `atom_of`, dashed scent for
  `references`, …) stays the source of identity; the weight adds a
  millimetre on top, so reinforcement reads as thickness, not as a
  type change.

### Weight formula

```
w_local = soft_or(w_kind, w_tag, [w_coact])
soft_or(...) = 1 - ∏ (1 - w_i)
```

Soft-OR saturating combine, not a sum: two evidence sources don't
push past 1 (the value retains its meaning as "probability of
strong tie"), and a missing source is neutral (it contributes
`1 - 0 = 1` factor, leaving `w` unchanged).

Per-source contributions:

- `w_kind`: a fixed base from the link's typed kind. v1 values:
  `atom_of 0.85`, `supersedes 0.7`, `replies_to 0.6`, `references 0.4`.
  An ADR's worth of weighting profile (discovery-heavy, structural-
  heavy, work-heavy) is a future tunable; v1 keeps a single
  hard-coded set.
- `w_tag = 1 - 1 / (1 + 0.4 · sharedGenericTags)`. Adamic-Adar-style
  preference for *rare* shared tags is a v2 refinement; v1 uses
  raw shared-tag count (cheap, no full-tag-degree pass).
- `w_coact` (v2+): a normalised co-task / co-time-slot count, sourced
  from Proposal A's activity log. Out of v1 (no backend changes).

### Layout: force simulator

A three-force model that produces an "organic" graph drawing while
staying deterministic given a seed:

1. **Repulsion** (Coulomb-like, `F ∝ -k/d²`) between every pair of
   nodes. O(N²); fine for typical gardens (< 300 visible notes).
2. **Attraction** (spring) along each edge with rest length
   `L₀ / (0.3 + 0.7w)`: a heavy edge wants the nodes ≈ 80 px apart;
   a light edge accepts ≈ 270 px. The spring constant `k` also
   scales with `w` so heavy edges integrate faster.
3. **Centripetal gravity** toward `(0, 0)` proportional to
   centrality: `F_g(n) = β · (1 + 4·c(n)) · r(n)` with `c(n) ∈ [0, 1]`.
   - **v1**: `c(n) = degree_manual(n) / max_degree_manual`. Cheap,
     deterministic, no backend.
   - **v2**: `c(n) = PPR_seeded(n)` in focus mode, `PageRank(n)` in
     global view (unblocked by 8c0a8f08).

Integration: 250 ticks, `alpha`-cooling at 0.985 per tick (stops
early when alpha < 0.05), per-tick step clamp at 18 px so a single
repulsion impulse can't catapult a node off-canvas. The seed is a
golden-angle spiral on sorted-by-id nodes; the result is identical
on every reload for the same graph.

User drag positions persist in `localStorage` and *always* win over
the simulator. The garden tab is the only consumer; no cache is
needed cross-component.

### Out of v1

- **Bezier perpendicular offset** for parallel-edge avoidance:
  requires a custom xyflow edge renderer. Future cosmetic.
- **Halo centrality**: the gravity already concentrates hubs;
  layering a halo ring on top of the existing entropy halo is
  cosmetic and would compete with the maturity glyph.
- **Pin/unpin explicit UX**: today a drag persists in localStorage,
  which is *effectively* a pin. A semantic "pin" toggle with an
  unpin gesture is reserved for when the simulator runs on every
  notes change (it doesn't today; see below).
- **Leiden clusters** colouring (8c0a8f08): the v1 primary-tag
  cluster is good enough; promoting it to Leiden requires the
  cluster id from the backend.
- **Semantic bias via embeddings** (c7d0bb4c): an extra spring
  between nodes with `cos(embedding) > θ`. Optional v3.

### Sync semantics

The simulator runs **once per workspace mount**, plus on the
narrower condition `anyUnplaced && weightedLinks.length > 0` during
sync (a new note appears and no stored position exists). This means
toggling focus, filtering by tag, or creating a link between two
already-placed nodes does NOT re-shuffle the canvas under the
user's hands. The garden is a tended forest, not a churning
simulation.

## Consequences

- **First consumer of the GardenIcon set** (56d80038) is the
  `LinkedTasksPanel` / `LinkedNotesPanel` chips, which use the
  forest glyphs as the kind icon. The mindmap itself stays on
  the existing per-kind stroke + colour vocabulary (see
  GardenMindmap.tsx); migrating it to glyph-labelled edges is a
  separate UX call.
- **v2 unlocks a `note_edge_strength` table or materialised view**
  (task 4467acb4): the `edgeWeightV1` helper in the SPA becomes a
  thin map over `(source, target, weight)` rows from the API; the
  layout formula is unchanged.
- **v2 also unlocks PPR-seeded gravity** when focus is active:
  the centripetal pull biases the visible subgraph toward the
  user's current line of thinking, which is what makes the "walk
  LLM" feature (5bf31b63) usable on top.
- **Co-activity weight** (Proposal A) becomes a third factor in
  the soft-OR once the activity log carries the requisite shape.

## Alternatives rejected

- **Linear sum of factors** instead of soft-OR: trivially exceeds
  1, loses the "probability" interpretation, and overweights pairs
  that happen to be reinforced by every available source.
- **D3-force as a dependency**: adds ~30 KB gzipped for a behaviour
  we can implement in 150 LoC, with no surface beyond the three
  forces this ADR specifies. Reconsidered if v2 needs the WebGL
  variants or the heavy quadtree optimisations.
- **Re-running the simulator on every notes change**: jitters the
  canvas under the user's hands; defeats the purpose of an organic
  layout that the user can mentally anchor. The gated sync above
  is the compromise.
- **Replacing the per-kind stroke vocabulary with a single
  weighted stroke**: loses the at-a-glance "this is a reply" /
  "this is a citation" / "this is the trunk" cue. Thickness as
  modulation, not as identity.

## Roadmap

1. v1 (shipped, this ADR): force layout + soft-OR weights, all
   frontend. Tasks 7e99c724 + 56d80038 (icons + first consumer).
2. v2 (blocked by 4467acb4 + 8c0a8f08): backend `note_edge_strength`
   + centrality endpoint; SPA reads them; weighting profiles
   selectable per workspace.
3. v3 (blocked by c7d0bb4c): embedding-derived semantic bias; the
   Walk LLM tab (5bf31b63) renders a sentier illuminé over the
   layout.
