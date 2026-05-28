# ADR-0039 — Fungal decomposition pipeline (humus producer)

Status: Proposed
Date: 2026-05-28
Tracks: task `4a718dc4-b220-40f5-9057-a009674d8143`
Depends on: ADR-0029 (note garden ecosystem), ADR-0012 (LLM abstraction)
Consumed by: ADR-0034 (humus policy in the walk), ADR-0035 (`fungal_lag`
sensor)

## Context

The manifesto's closed loop is: finished thinking is archived, the
archive *decomposes* into reusable atoms (humus), and that humus feeds
the next thought. Two later ADRs already assume a producer of humus:
ADR-0034 consumes the `humus_flag` / subtype to bias the LLM walk, and
ADR-0035's `fungal_lag` metric measures "archival → first distillation".
Neither could be ratified because the producer — the decomposition
pipeline (task `4a718dc4`) — had no ADR of its own; it existed only as
code. This ADR records the design and pins the contract those two ADRs
depend on.

## Decision

### Phase 1 — on-demand distillation (shipped)

`distill_note(note_id)` (`core/.../services/decomposition.py`, commit
`3ccf5fd`):

1. Reads the source note's body (reconstructed from `note_part(ord=0)`).
2. Calls the LLM provider (ADR-0012 seam) with a fixed system prompt:
   one-sentence lesson, up to five concrete claims, up to three
   keywords. No restate, language matches the input.
3. Persists the synthesis as a **new note** (`kind=text`) with
   `humus_kind='distillation'`, `humus_flag=true`, carried into the
   source's project (the junction project, migration 0016).
4. Flags the **source** `humus_flag=true`: it has been decomposed and
   the walk may now surface it as fertiliser.
5. Links distillation → source with an `atom_of` `note_note_link`
   (the distillation is an atom of the source).

**Idempotent** on `(source_note_id, humus_kind='distillation')`: a
second call returns the existing distillation untouched. Member role
required; the LLM call is metered.

### Humus schema (the contract ADR-0034 / ADR-0035 depend on)

Migration `0015_note_humus` adds two columns to `notes`:

- `humus_kind text` — the **subtype**, one of
  `{distillation, pattern, season}` (NULL for non-humus notes). This is
  a distinct column, **not** `note.kind`: a distillation note is still
  a normal `text` note for every other purpose (editable, deletable,
  linkable). ADR-0034's "explicit subtype" predicate reads
  `humus_kind IN (...)`, not `note.kind`.
- `humus_flag boolean` — the materialised "is humus" predicate, so
  retrieval (ADR-0034) and the sensors (ADR-0035) never recompute it. A
  partial index on `(org_id, humus_flag)` keeps the humus subset cheap
  to scan.

Two ways the flag is set:

- **Explicit**: the pipeline sets it on the distillation (and on the
  decomposed source) at write time.
- **Structural** (ADR-0034): a node archived > 30 days ago AND ≥ 3
  incoming `references` AND PageRank in the workspace top 20%. This is
  materialised by the nightly worker, not at write time. Tie-break for
  the "top 20%" cut: higher PageRank wins; on equal PageRank the older
  archival date wins (longer-fermented first), then lexical id as a
  deterministic final key.

### Trigger surface (shipped)

The pipeline is **inert without a trigger**, so Phase 1 wires two
on-demand triggers (commit `3ccf5fd`):

- `POST /notes/{id}/distill` → `{source_note_id, distilled_note_id,
  model_id, created}`.
- MCP tool `distill_note(token, org_id, note_id)` for agents.

**Trigger policy.** On-demand only, for now. Automatic
distill-on-archive is deliberately **deferred to Phase 3**: it is an
automatic LLM action (cost + the proposal-not-imposition constraint of
ADR-0034 / the Phase-3 umbrella), so it must arrive with confidence,
transparency and reversibility, not as a silent side effect of
archiving. The service documents that the caller decides when to
trigger.

### Phase 2 — pattern and season synthesis (planned)

Two batch producers, both emitting humus notes with the corresponding
`humus_kind`:

- **`pattern`**: per Leiden cluster (ADR-0031 v2 / task `8c0a8f08`),
  aggregate the cluster's distillations into a higher-order pattern
  note. Gated on Leiden landing.
- **`season`**: a quarterly synthesis over a time window, emitting a
  `season` note. A cron entry; the prompt and the link model are the
  same shape as Phase 1 (one helper + one schedule, as the service
  docstring anticipates).

### Reversibility and transparency

A distillation is an ordinary note: the user can read, edit, or delete
it, and the `atom_of` link is inspectable from either end. Re-running is
a no-op. Nothing about decomposition is hidden or irreversible, which is
the precondition for the Phase-3 automatic trigger to be acceptable
later.

## Consequences

- ADR-0034 and ADR-0035 lose their dangling dependency: the producer
  exists, the `humus_flag` / `humus_kind` contract is pinned, and
  `fungal_lag` has a real event to measure (archival → first
  `humus_kind='distillation'` emission).
- Until a user (or an agent) triggers distillation, the humus subset is
  empty and ADR-0034's walk policy is a no-op in practice — correct, not
  a bug: humus is opt-in until Phase 3 automates it.
- The metered LLM call means a workspace with no credits cannot
  distil; the trigger surfaces the billing error to the caller.

## Alternatives rejected

- **A new `note.kind = 'distillation'`.** Rejected: it would fork every
  kind-switch in the codebase (rendering, list filters, search) for a
  note that is otherwise plain text. The orthogonal `humus_kind` column
  keeps decomposition a *facet*, not a new entity.
- **Auto-distil on archive now.** Rejected for Phase 1: an automatic,
  metered LLM action that the user did not ask for violates
  proposal-not-imposition. Deferred to Phase 3 with the rest of the
  automatic-classification machinery.
- **Store the distillation inline on the source note.** Rejected: it
  would make the source mutable by a machine process and lose the
  separate-node provenance (the `atom_of` link, independent maturity,
  independent retrieval).
