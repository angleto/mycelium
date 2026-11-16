# ADR-0071: The graph reads the notes on its own, and a person decides what it believes

Status: Accepted (2026-11-16), one point left open (below)
Relates to: ADR-0044 (the temporal knowledge graph; this record carries out
its deferred "wiring extraction into the autonomous ingest sweep behind a
flag"), ADR-0043 and its amendment of 2026-11-12 (nothing a model writes
about the notes takes effect before a person approves it), ADR-0056 (a note
opted out of automatic indexing is opted out of every automatic reader),
ADR-0068 (the review operations are declared once and bound by REST and MCP).
Task 6d9f2128; unblocks 2ae39b5b (entity resolution).

This record decides how the knowledge graph gets a source that nobody has to
remember to run, what a model's reading of a note is allowed to become
without a person, and what the pass that does the reading may spend.

## Context

Measured on production on 2026-11-16: the graph of ADR-0044 is empty. The
entity search returns nothing for "a" or "e" on the only workspace in use,
and `extract_facts` has one caller, the `kg_extract` tool, which nobody
calls. Three facts made "just switch on the autonomous pass" wrong:

- **A proposed fact could never be decided.** `approve_fact` and
  `reject_fact` existed in the service and on no surface: no route, no tool,
  no screen. A fact born `proposed` would have stayed invisible for good.
- **Autonomous facts would have been born effective.** Their review state
  followed `garden_review_gate_enabled`, which defaults to off. The
  amendment to ADR-0043 made every generated note proposed regardless of
  that flag, and left the graph out on purpose; so the first autonomous pass
  would have written effective facts about the notes with nobody having
  read them.
- **`reject_fact` rejected anything.** It delegated to `invalidate_fact`
  without checking the fact was a proposal, so "reject" could tombstone a
  fact a person had asserted.

## Decision

1. **An autonomous read is always proposed.** `_review_state_for(True)`
   returns `proposed` unconditionally. `garden_review_gate_enabled` is
   retired: after ADR-0043's amendment it governed only this, and a setting
   whose one effect is to let a model's output through unseen is not one to
   keep.
2. **A person decides, on every surface.** `kg.review.pending` (`kg:read`),
   `kg.review.approve` and `kg.review.reject` (`HUMAN_ONLY`) are registry
   operations, so REST (`/kg/review/*`) and MCP (`kg_review_*`) carry them
   with one scope and one shape, and the garden review page shows them
   beside the generated notes. Both actions take the `expected_version` the
   reviewer read and are idempotent. They act on proposals only: a fact a
   person asserted is absent here (`kg.fact_not_found`), not forbidden.
3. **A rejection is final and remembered.** Rejecting sets
   `review_state='rejected'` and `invalidated_at` in one write, after which
   the row is frozen by the existing trigger. The effective predicate now
   excludes `rejected` as well as `proposed`: with only `invalidated_at` to
   go on, a transaction-time read before the rejection would have shown a
   proposal as a past belief. The autonomous pass does not propose a triple
   a person rejected.
4. **The pass reads what a person wrote and wants seen, once per text.**
   It is a step of the garden loop (`kg_autonomous_extract_enabled`, off by
   default, and `garden_loop_enabled`), after every other step, so a pass
   that runs out of credits costs none of them. A note qualifies when it is
   live, not archived, not a proposal or a rejection, `index_scope='org'`,
   not being transcribed, holds some non-blank text, and has been untouched
   for `kg_extract_settle_minutes`. `kg_note_extraction` (migration 0028)
   records each read against a hash of the note's text; a note is read
   again only when that hash changes. The hash is over the text because
   `update_note` rewrites a part in place without bumping its version: the
   first signature, over part ids and versions, missed every ordinary edit,
   and the test written for it is what found it.
5. **A read is paid for once, and what is paid for is kept.** The model
   call and the writes run in two savepoints. If the call fails nothing was
   charged; if the writes fail they roll back alone and the debit stays,
   because the provider was paid. An answer with no JSON in it is a failed
   read, not an empty one. A failed read is retried up to
   `kg_extract_max_attempts`, and each attempt is a new metering key
   (`kg_extract:<org>:<note>:<hash of the body>:auto<n>`), so a retry is
   charged like the call it is. One relation with a backwards validity
   window is dropped on its own instead of failing the whole note. Each
   note is read under a transaction-scoped advisory lock and the record is
   re-read after taking it, so two passes over one workspace do not both
   pay for the same note.
6. **What a pass may spend is stated in tokens, and each note pays its own
   estimate.** `kg_extract_tokens_per_pass` bounds a pass; each note is
   charged an estimate from its own size (`estimate_read_tokens`, the
   billing byte-per-token ratio plus an output allowance that is not yet
   measured). Notes are taken longest-settled first and the pass stops at
   the first one that does not fit, so a large note is never overtaken
   forever; one larger than the whole budget is recorded `too_large`
   instead of blocking the queue, and comes back after an edit or once the
   budget is raised above its estimate. The money bound is the workspace's
   existing daily credit cap and kill-switch (`autonomous_budget`), which
   pause the whole garden loop. Before each call the pass checks the wallet
   against the estimate (`MeteredLLM.ensure_affordable`, new): `meter`
   refuses only after the provider has answered, which for an unattended
   loop meant paying the provider on every tick for a debit the wallet then
   refused. Running out of credits ends the pass without marking any note
   failed.
7. **The metering key of a person's `kg_extract` names the text.** It was
   `kg_extract:<org>:<note>`, so a re-read after an edit replayed the first
   debit and was never charged; it is now `kg_extract:<org>:<note>:<hash of
   the body>`.
8. **Nothing the pass writes shows before a person approves it.** The pass
   creates an entity only for a fact it proposes, and `kg_entities` lists an
   entity only when an effective fact touches it or no fact touches it at
   all. A fact a person retracted (`invalidate_fact`) is remembered like a
   rejected one and not proposed again.

## What the pass writes and deletes

Writes, all in the workspace's own rows under RLS, one transaction per note:
`kg_entity` (new entities only; an existing one is reused, never updated),
`kg_edge` (new proposed facts only; a triple already open is reused),
`kg_entity_source` (note provenance), `kg_note_extraction` (the record of
the read), one `activity_log` row per successful read (`kg_extract`,
with `autonomous: true`), and the usage record and wallet debit of each
metered call. The per-note advisory lock writes nothing.

Deletes: none. It never modifies or deletes a note, a part, a fact or an
entity. The only delete path into the graph remains `erase_by_source`,
unchanged. A person's decision updates one `kg_edge` row (`review_state`,
and on reject `invalidated_at`); nothing is deleted then either.

## Open

**Should `kg_extract`, asked by a person, also be born proposed?** Today it
writes effective facts, as it always has. The amendment to ADR-0043 says
"asking for a summary is not vouching for it" for notes, and the same
argument applies to facts; against it, `kg_extract` is the only way a person
can put facts into the graph at all, and making it proposed turns every
manual extraction into a second step. A related consequence follows from the
current answer: when a person runs `kg_extract` on a triple the pass has
already proposed, the open triple is reused and stays proposed, so the
person's own extraction does not make it effective. This record does not
decide either; it is Angelo's call.

## Consequences

- The graph can fill up without anyone remembering to ask, and nothing a
  model reads into it counts until a person approves it.
- An edited note is read again and may propose new facts; facts proposed or
  approved from its earlier text stay as they are. Retiring facts whose
  source text changed is not done here.
- Entity resolution is still exact-name only, so "Angelo" and "Angelo Leto"
  stay two entities. That is task 2ae39b5b, which this unblocks.
- Each pass hashes the text of every note in the workspace inside the
  database to find what changed. That is linear in the workspace's text,
  once per garden tick; not measured on production, where the workspace in
  use is one person's notes.
- A retried approve or reject that echoes the version it read gets
  `stale_version` after the first one succeeded, as for generated notes
  (ADR-0043): a stale view is an error even when the end state would
  coincide.
- Proposed facts whose source note was later trashed or opted out of
  indexing stay in the inbox; deciding them is still meaningful, and
  hiding them would leave them pending for good.
- The review page has no end-to-end test, as the garden review never had:
  no route creates a proposed fact, so a browser test would need a fixture
  path the product does not have.

## Alternatives rejected

- **Turn on `garden_review_gate_enabled` and keep it.** A flag that can be
  turned off is a way for a model's output to go live unseen; the amendment
  to ADR-0043 already removed that switch for notes.
- **Use `kg_entity_source` or `kg_edge` as the record of what was read.** A
  note that states no fact leaves no row in either, so it would be read and
  paid for on every pass.
- **Bound a pass by a number of notes.** Notes differ in size by orders of
  magnitude, so a count bounds nothing a model is billed for.
- **Read on note creation, from the write path.** It would put a metered
  model call in the latency of every save and read text still being typed.
