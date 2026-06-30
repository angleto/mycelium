# ADR-0043 — Human-gated review state for AUTONOMOUSLY-generated nodes

Status: Accepted (2026-06-21) — D1–D3 + D5 implemented behind
`garden_review_gate_enabled` (default off). Task: e87daff4 (pattern/season +
distill human-approval gate). Driven by Angelo's requirement: a summary the
system generates AUTONOMOUSLY (unsolicited) must NOT enter the corpus until a
human approves it; the model that produced it must be visible; reject must
never pollute; autonomy is *earned* once a model proves reliable.
"L'architettura migliore e più solida, niente accrocchi."

## Implementation status (2026-06-21)

Shipped behind `garden_review_gate_enabled` (default off, so byte-identical
until a workspace opts in):

- **D1** — `notes.origin_model_id` + `notes.review_state` (migration 0056,
  NULL-default, partial index on the rare `proposed` rows).
- **D2** — the `review_state IS DISTINCT FROM 'proposed'` exclusion on every
  retrieval/listing surface: the `HumusStage` source, the `memory.retrieve`
  base predicate (covers lexical + dense + humus in one place), `list_notes`,
  `get_note` (with an `include_proposed` inbox bypass), the `task_search`
  note branch, the `@`-lookup picker, and the free-wander `humus_note_ids`
  set. Regression-tested absent-then-present-after-approve.
- **D3** — `decomposition.distill_note` / `extract_cluster_pattern` /
  `synthesize_season` take `autonomous: bool = False` and stamp
  `origin_model_id` always + `review_state='proposed'` only for an
  autonomous, gate-enabled run; `services/garden_review.py` owns
  `approve_node` (→ `commit` event), `reject_node` (soft-delete + `reject`
  event carrying `origin_model_id`), `list_pending`. Audited + idempotent.
- **D5** — MCP `garden_review_pending|approve|reject` + REST
  `GET /garden/review/pending`, `POST /garden/review/approve|reject`.

Deferred (each a tracked follow-up, none on the closure path for e87daff4):

- **The autonomous scheduler** that calls the generators with
  `autonomous=True` (no caller passes it today, so no `proposed` note is born
  in practice yet). It rides the garden loop and is gated separately; per
  Angelo it is the *last* step, after the gate + a model proven reliable.
- **D4 earned autonomy** — the per-model `accept_ratio` health sensor and the
  per-workspace auto-approve policy (`approval_required | auto | off`,
  mirroring `AutonomousDispatch`). The `reject` event already carries
  `origin_model_id` so the ratio is derivable. Until D4 lands the gate is
  always approval-required.
- **D5 SPA review inbox** — the "Proposed by the garden" panel.
- **Graph-centrality node-set exclusion** — proposed humus is withheld from
  the *walk* (`humus_note_ids`) and all direct-visibility surfaces; excluding
  it from PageRank/Leiden centrality math (`compute_recency`, the Leiden node
  set, the classify corpus) is a second-order refinement (it skews ranking,
  not visibility) and is gated/offline, so it is a follow-up.

## Scope — what is gated, and what is NOT (critical)

The gate applies **only** to the garden's **autonomous, unsolicited**
decomposition: the background sweep that runs distill / pattern / season on
its own (`actor_kind='system'`), which the user never asked for. **Everything
the user initiates is LIVE at birth, exactly as today** — a note typed in the
SPA, a note dictated to Claude and written **via MCP**, or an on-demand
distill/pattern/season the user **explicitly invokes** (the user is in the
loop and sees the result in the response). The discriminator is the *trigger*
(autonomous system sweep vs. user-initiated), NOT "did an LLM write the text".
The user creates many notes through the MCP; those are user-initiated and are
**never** gated.

## Context

`decomposition.py` (distill / pattern / season) generates a NEW humus note
and sets `humus_flag=True` **immediately** (decomposition.py:177, :371), so
it enters the ADR-0034 retrieval walk the instant it is written. A weak
summariser therefore pollutes retrieval until a human notices and deletes it
— the exact failure mode we must prevent. Originals are never lost (sources
are read-only, archived-only, additive, idempotent), so the gap is purely
the *missing approval gate* + *missing model provenance on the artifact*.

An adversarial survey of every existing approval/proposal primitive found
NONE is the right home (each is a mismatch or an overload):

- **DispatchRequest / AutonomousDispatch** (ADR-0028): gates task
  *execution* before spending credits — the opposite direction (the note is
  already generated; we gate its *visibility*, not a run). Wedging a
  generated note in would fabricate task rows and lie about
  `projected_credit_cost`.
- **Event bus + classification_feedback** (ADR-0036/0037): models accept/
  reject of *facets on existing nodes* (tag/link/maturity). A new node is a
  *structural* proposal; its payload, its non-idempotent rollback, and a
  *per-model* (not per-feature) reliability signal all strain the facet
  model.
- **Annotation suggestions / Adjudication**: edit-of-existing-text /
  multi-agent convergence; a reject would leave a zombie note.
- **`maturity` / `humus_flag`**: overloading either conflates *ripeness of
  thought* (garden lifecycle) or *is-humus* with *is-approved*. This is the
  kludge we reject.

## Decision

Introduce a **first-class, orthogonal review state for nodes** — the
generalisation of "proposal-not-imposition" from facets (ADR-0037) to whole
nodes. It is the foundational primitive for any *autonomously generated*
node (humus today; future generators reuse it). State lives on the node (a
cheap retrieval filter); events ride the existing bus (audit/learning);
the earned-autonomy policy mirrors `AutonomousDispatch` — each reused for
what it is actually good at, none overloaded.

### D1. Two new columns on `notes` (orthogonal to maturity / humus_flag)

- `origin_model_id: str | None` — the LLM `model_id` that generated the node
  (NULL for human-authored). Closes the *transparency* requirement: the
  model is now on the artifact, not only in the transient MCP response.
- `review_state: str | None` — NULL for the normal case: every human/legacy
  note AND every **user-initiated** creation (SPA, MCP/dictation, on-demand
  distill the user invoked) — always effective, unchanged from today.
  `'proposed'` is set ONLY by the autonomous garden sweep when it generates a
  summary unsolicited; `'approved'` once a human accepts it. No stored
  `'rejected'`: a reject soft-deletes the node (see D3), so a rejected summary
  never lingers.

A note is **effective** (eligible for retrieval/listing) iff
`review_state IS DISTINCT FROM 'proposed'` AND `deleted_at IS NULL`.
`humus_flag` keeps its exact current meaning ("this is decomposed humus");
it is set at generation as today, but `review_state='proposed'` withholds
it from the walk until approval. No concept is overloaded.

### D2. One uniform exclusion predicate on every retrieval/listing surface

Every place that surfaces notes adds `review_state IS DISTINCT FROM
'proposed'`: the `HumusStage` (retrieval/stages/humus.py), `memory.retrieve`
note path, `notes.list_notes` / `get_note`, and the `task_search` note
branch. A pending proposal is invisible everywhere except the proposer and
the review inbox — it cannot pollute search, the walk, or any listing.
(Enumerated so none is missed; a regression test asserts a `proposed` note
is absent from each surface and present after approval.)

### D3. Generate → propose; human approve/reject; reversible + audited

- The decomposition functions decide `review_state` from the caller: the
  **autonomous sweep** (`actor_kind='system'`, unsolicited) creates the note
  `review_state='proposed'` (NOT effective); a **user-initiated** call
  (MCP / SPA / on-demand) creates it `review_state=NULL` (live now, exactly as
  today). Both stamp `origin_model_id=<model>` for provenance + `humus_flag`
  as today. So the gate never touches a note the user asked for.
- `approve_node(note_id)`: `review_state -> 'approved'`; audited; emits a
  bus `commit` event (ADR-0036, the event layer — reused for audit, NOT for
  state); the note is now effective.
- `reject_node(note_id, reason?)`: soft-delete the note (never pollutes);
  audited; emits a bus `reject` event carrying `origin_model_id`. Reversible
  via the existing recovery/restore path like any soft-delete.

The bus carries the *events* (the audit/replay stream already does
propose→commit/reject); the *state* is the column. State and event stay in
their own layers — the strain the survey found in "make the bus hold the
state" is avoided.

### D4. Earned autonomy from a per-model accept-ratio

The approve/reject events yield `accept_ratio(model_id) = approved /
(approved + rejected)` — the reliability signal Angelo asked for ("se il
modello è affidabile"). Surfaced in /garden/health (a sensor) and per model.
A workspace MAY later opt a model above a threshold into **auto-approve**
(its generations are born `review_state='approved'`) — the policy pattern of
`AutonomousDispatch` (approval_required | auto | off), per workspace, default
**approval_required**. Autonomy is earned + reversible, never assumed.

### D5. Surfaces

- MCP: `distill_note` / `extract_cluster_pattern` / `synthesize_season`
  already return `model_id` and stay callable from Claude Desktop; they now
  produce a `proposed` node. New MCP + REST: `garden_review_pending` (list),
  `garden_review_approve`, `garden_review_reject`. Each carries
  `origin_model_id` so the human sees the model before deciding.
- SPA: a "Proposed by the garden" review inbox (note id + model + body
  preview + approve/reject). Out of this ADR's core; tracked as the SPA
  follow-up.

## Consequences

- A new migration adds the two columns (NULL-default, so every existing note
  is effective and unchanged — byte-identical behaviour for human notes).
- The whole feature is gated: generation-as-proposal + the review surface
  ship behind a flag; with it off, behaviour is unchanged (no `proposed`
  notes are ever created).
- General: any future autonomous node generator reuses `origin_model_id` +
  `review_state` + approve/reject; this is the Fase 3 "proposta-non-
  imposizione for whole nodes" substrate, not a humus one-off.

## Alternatives rejected

A separate `node_review` table (node_id FK + state + model + decided_by):
more general across node kinds, but forces a join / NOT EXISTS on every
retrieval surface (D2) — a real per-query cost — for a generality (non-note
nodes) we do not need today. The column approach keeps the hot filter a
plain predicate; a table can supersede it if non-note nodes ever need the
gate. Overloading `maturity`/`humus_flag`/`DispatchRequest`/the bus-state:
rejected above (the survey's accrocco findings).
