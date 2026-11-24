# ADR-0075: A proposal is read before it is decided, and a decision never loses anything

Status: Accepted (2026-11-24, Angelo approved the direction and phase 1).
Phase 1 implemented (migration 0035); phases 2 and 3 open. Decision 2 was
widened by [ADR-0076](0076-an-agent-checks-and-revises-a-person-keeps-the-last-word.md):
an assistant may read a proposal whole too, to check or revise it.
Relates to: ADR-0032 (classify suggestions), ADR-0040 (link model), ADR-0043
(human-gated review state, amended 2026-11-12), ADR-0068 (one operation
registry for REST and MCP), ADR-0071 (knowledge-graph facts a person
decides), ADR-0073 (link proposals). Supersedes the sentence of ADR-0043
"No `'rejected'` is ever stored on `review_state`".

This record decides how the Garden presents everything the system proposes
about a person's notes: one kind of object, readable in full before the
decision, with the same verbs everywhere, and with every decision reversible
from a visible history. It records what was measured on 2.4.17 that made the
current surface unusable, the decision, the phases, and the alternatives
rejected.

## Context

Angelo, 2026-11-24, after using the Garden on production 2.4.17: he asked
for a distillation and could not see what it produced; rejecting it sent it
to a bin and the note looked lost; restoring it put it back in "Awaiting
review" where he still could not read it; the suggestion glyphs (scissors,
plus) read as destructive buttons, and he is afraid to click anything for
fear of breaking or losing data. Each point was checked against the code:

| what the person sees | what the code does | where |
|---|---|---|
| Distill, then nothing readable | the distillation is born `proposed`; the Garden page navigates to `/garden/review` | `web/src/routes/GardenRoute.tsx` `onDistill` |
| the inbox item links to the note, which does not open | the inbox links to `/notes/{id}`, and `get_note` answers 404 for a proposed note by design (ADR-0043 D2); the API test pins the 404 | `GardenReviewRoute.tsx`, `services/notes.py` `get_note`, `api/tests/test_garden_review_api.py` |
| only a preview is ever shown | the inbox serves 280 characters (`_PREVIEW_CHARS`) and no surface serves the whole body to the reviewer | `services/garden_review.py` |
| Reject loses the note | reject soft-deletes the proposal; emptying the workspace trash purges it although the trash view does not show it, and the retention sweep reaches it on its own | `garden_review.list_rejected` docstring |
| Restore leads nowhere | restore clears `deleted_at`, the node is `proposed` again, and is again unreadable | `notes.restore_note` |
| scissors, plus, arrow | the edge candidates (`link_add`, `link_prune`, `link_direct`) and the pattern and season candidates have no REST action: they render glyphs that look like buttons beside a muted "via agent" label | `GardenReviewRoute.tsx` |

The deeper cause is that the Garden has no single notion of a proposal.
These mechanisms each propose a change to the notes, and each has its own
endpoint, verbs, states and reversibility:

- classify suggestions (tags, links, maturity): `/garden/classify/{id}`,
  `/garden/apply`, accept or dismiss chips on the open note;
- link suggestions: `/garden/link-suggestions/{note_id}`;
- link proposals (ADR-0073): `/garden/link-proposals/*`, accept or reject in
  `LinkProposalsPanel`;
- garden candidates (distill, pattern, season; add, prune, direct a link):
  `/garden/candidates`, mostly without an action;
- synthesised notes (ADR-0043): `/garden/review/*`, approve or reject, reject
  being a deletion;
- knowledge-graph facts (ADR-0071): `/kg/review/*`, approve or reject.

A person cannot form a model of what happens to a note when six surfaces
answer the question six ways. The review gate of ADR-0043 exists so that a
person decides; a gate the person cannot see through decides nothing.

## Decision

### 1. One proposal contract

Every proposal shown to a person has the same shape, whatever produced it:

- `kind` (a closed type: `synthesis`, `link_add`, `link_remove`,
  `link_direct`, `tag`, `maturity`, `fact`), the subject notes with their
  titles, and the **effect stated in words and as a preview**: the full text
  that would become a note, the link that would be added or removed with
  both ends named, the tag, the maturity move from and to, the fact;
- the proposer (a rule name or a model id) and the evidence it gives;
- the state, one of `pending`, `accepted`, `declined`, and the version
  (the caller presents the version it read and a stale one is refused with
  the current one, as today).

Each mechanism keeps its storage, because each carries invariants of its
own (the temporal edges of the knowledge graph, the remembered pairs of
link proposals). Each publishes an adapter that maps its rows to the
contract and carries out the three verbs; adapters are found by presence,
not listed centrally. `GET /garden/proposals` (cursor-paginated) and
`POST /garden/proposals/{id}/accept|decline|reopen` are registry operations
(ADR-0068), so MCP and REST are one operation.

### 2. The person deciding reads the whole proposal

`GET /garden/proposals/{id}` returns the full proposed text and, for a
synthesis, its sources with their bodies, so the reading view can place
source and synthesis side by side. It is human-only, like approve and
reject since the 2026-11-12 amendment, and it is not a retrieval surface:
proposals stay excluded from search, listings, agents and the graph exactly
as ADR-0043 D2 requires. The perimeter gains one reader, the person whose
decision the gate waits for, which is the reader it was built for.

### 3. Declining is a decision, not a deletion

`notes.review_state` gains `declined`. The effective predicate
(`note_effective`, its one implementation) excludes it exactly as it
excludes `proposed`, pinned by the same matrix test. A declined proposal
never sets `deleted_at`, so neither the trash nor the retention sweep can
reach it. It stays in the Decided list, readable, and can be reopened
(`pending` again) or accepted directly. Removing it for good is a separate,
explicit, confirmed act. The source-humus rules of the 2026-11-12 amendment
move from reject to decline unchanged. Every other mechanism exposes the
same reopen through its adapter; where it already remembers a rejection
(link proposals, facts), reopen clears that memory.

### 4. Every control says what it does, and every decision can be undone

Controls are words naming the effect ("Accept: add link A → B", "Decline",
"Reopen"), never a glyph alone. An item no surface can act on shows no
control-shaped element. After a decision the item moves to Decided and an
Undo is offered in place. Accepting a `link_remove` keeps the removed link
restorable from Decided. The one irreversible act is the permanent removal
of a declined proposal, behind a confirmation that names what is lost.

### 5. One place, and the note tells its own story

The Garden gets one Proposals view with two lists, To decide and Decided,
filterable by kind and by note. Distill lands on the reading view of the
synthesis it produced. The note detail shows the proposals about that note
and their state ("Distillation awaiting your review", with the link), and a
"What happened to this note" timeline read from `activity_log`, in words:
maturity moves by a person and by the sweep, the humus mark and its removal,
proposals accepted and declined about it.

### 6. No candidate without an action

The candidates that today have no REST action (pattern, season, the three
edge operations) become proposals with the verbs above, carried by the same
registry operations. Until a kind has its action it is not shown in the
Garden; it is recorded as a gap between the MCP surface, where an agent can
carry it out, and the web, where a person cannot, and not displayed as a
dead control.

## Phases

| phase | delivers | closes |
|---|---|---|
| 1 | decision 2 for syntheses, decision 3 (migration, predicate, matrix test), the Decided list with Reopen, Distill landing on the reading view, the note detail showing its distillation state | every defect in the Context table except the glyphs |
| 2 | decision 1 and the adapters for the other mechanisms, decision 4, the one Proposals view, the note timeline of decision 5 | the six surfaces and their vocabularies |
| 3 | decision 6 | the candidates without an action |

Each phase ships with end-to-end tests that select by role and walk the
lifecycle a person walks: propose, read, decline, find it in Decided,
reopen, accept, undo.

### Phase 1 as built (2026-11-24)

| decision | where | how it is checked |
|---|---|---|
| 2, reading | `garden.review.read` (`GET /garden/review/nodes/{id}`, MCP `garden_review_read`), person only; the web view `/garden/review/:id` | `api/tests/test_garden_review_api.py`, `web/src/routes/GardenProposalRoute.test.tsx` |
| 3, declined | `notes.review_state = 'declined'`, `notes.review_decided_at`, `ck_notes_review_state` (migration 0035); `note_effective.GATED_STATES` is the one definition, read by the blob perimeter, the classification queue and the fact extractor | the state matrix and the catalog comparison in `core/tests/test_note_effective_predicate.py`; every door of `core/tests/test_gated_note_body_surfaces.py` runs once per gated state |
| 3, reopen and removal | `garden.review.reopen` (`notes:write`), `garden.review.remove` (person only, declined only, through the purge emptying the bin uses) | `core/tests/test_garden_review.py` |
| 3, the trash | a declined note has no `deleted_at`, so neither emptying the bin nor the retention sweep reaches it; existing rejections were migrated to `declined` | the API test empties the bin and finds the decline still listed |
| 5, partly | Distill opens the reading view; the source note lists what was generated from it and its state (`garden.review.for_source`) | `web/src/routes/GardenProposalRoute.test.tsx`, API test on `/garden/review/by-source/{id}` |

Two consequences found while building it. A pattern or season declined the
old way stayed in the unique index on its signature while its idempotency
check skipped it, so generating it again failed; as a stored state the
decline is found by that check, which returns it. And the restore of
`/notes/{id}/restore` no longer has a special case for proposals: it answers
404 on one, like every ordinary door, and reopening is the review surface's
verb.

Not in phase 1: the timeline of decision 5, the controls and glyphs of
decision 4 outside the review pages, and the other proposal mechanisms.

## Consequences

- The review gate becomes usable without weakening it: no proposal reaches
  search or an agent before a person accepts it.
- `deleted_at` keeps one meaning (a person threw a note away), and a
  rejected proposal is no longer silently purged.
- Six vocabularies become one; the i18n catalogue loses the per-mechanism
  verbs.
- Migration: existing soft-deleted proposals (`review_state='proposed'` and
  `deleted_at` set) become `declined` with `deleted_at` cleared, so the
  proposals already rejected reappear in Decided instead of waiting for a
  purge.

## Alternatives rejected

- **Restyle the review page** (labels instead of glyphs) and keep the
  lifecycles: the proposal stays unreadable and a rejection stays purgeable,
  which are the two defects that lose work.
- **Open proposed notes on `/notes/{id}` behind a banner**: the read path is
  shared by search, agents, part reads and fact extraction (the doors closed
  by task a186c989); relaxing it reopens all of them to serve one reader.
- **Keep reject as soft-delete and exempt proposals from the purge**: two
  meanings for `deleted_at` and a second rule site for what may be purged.
- **One physical `proposals` table for every mechanism**: a large migration
  with no gain a person can see over a contract, and it would have to carry
  each mechanism's invariants as optional columns.

## What this does not cover

- The quality of what the models propose; that is ADR-0043 D4 and the
  benches.
- The maturity lifecycle itself (ADR-0029, ADR-0032): decision 5 makes its
  moves visible, it does not change them.
- Bulk decisions (accept all of a kind); the contract allows them, this
  record does not add them.
