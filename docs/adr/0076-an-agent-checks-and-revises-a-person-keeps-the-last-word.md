# ADR-0076: An agent checks and revises what a model proposed, and a person keeps the last word unless they hand it over

Status: Accepted (2026-11-24). Phase A implemented with this record; phase B
open.
Relates to: ADR-0043 (human-gated review state, amended 2026-11-12, which
foresaw "an approval delegated to an adversarial verification" as a later
step), ADR-0068 (one operation registry for REST and MCP), ADR-0071 (facts
a person decides), ADR-0073 (link proposals), ADR-0075 (a proposal is read
before it is decided).

This record decides how the evolution of the garden can be delegated to an
assistant working through MCP: reading a proposal whole, checking it against
its sources, writing a better version of it, and writing syntheses with its
own model. It also decides how a person hands the last word to an assistant
for the decisions they consider simple, and keeps it for the rest.

## Context

Angelo, 2026-11-24: the small production models (mistral among them) write
the distillations; he wants to ask Claude, through MCP, to verify one, or to
improve it, and then approve it himself. In general the agent must not have
the last word, but there are simple cases where he is happy to let it.

Measured on 2.4.18, with an assistant credential carrying a scope list:

| operation | the assistant |
|---|---|
| `distill_note` with its own text | allowed |
| `extract_cluster_pattern`, `synthesize_season` | allowed, internal model only |
| `garden_review_read` (the whole proposal) | refused, `mcp.scope_denied` |
| editing a pending proposal | impossible: every write door answers 404 on a gated note |
| approve or decline a proposal, a link proposal, a fact | refused, unless it carries out a comment the person wrote |

So an assistant could add proposals but could neither read nor correct the
ones a small model wrote, which is the work it is best placed to do.

## Decision

### 1. An assistant reads a proposal whole

`garden.review.read` moves from person-only to `notes:read`. The review gate
of ADR-0043 keeps proposals out of search, listings, the walk, the graph and
every surface an agent grounds an answer on; it does not need to keep them
from an agent that opens one by id in order to check it, and the response
says what it is (`review_state`), so it cannot be mistaken for a note of the
memory.

### 2. A check is a report attached to the proposal, and it decides nothing

`garden.review.check` (`notes:write`) attaches to a proposal a verdict
(`supported` or `issues`), a summary, and one finding per claim: the claim,
its status (`supported`, `unsupported`, `contradicted`, `unclear`), the
passage of the source that supports or contradicts it, and a comment. The
check records the version it read and who wrote it (identity and model). A
check on an earlier version is shown as such. Checks are stored in
`proposal_check`, workspace-isolated, deleted with the proposal.

### 3. A revision replaces the proposal it improves

`garden.review.revise` (`notes:write`) takes a pending or declined proposal
and the revised text, and creates a new proposal of the same kind, drawn
from the same sources, recording what it revises (`notes.revises_note_id`)
and which model wrote it. The original is declined with the reason
`revised`, kept among the declined and reopenable, and not counted against
the model that wrote it: a person has not judged it. For a pattern or a
season the signature moves to the revision, so the synthesis of that source
set is the revision. An approved note is not revised here: it is an ordinary
note, edited like one.

### 4. An assistant writes patterns and seasons with its own model

`extract_cluster_pattern` and `synthesize_season` accept the text and the
model id, as `distill_note` already does, with the same cap and the same
`external:` provenance.

### 5. The person sees the work before deciding

The reading view shows the checks (verdict, findings in words, who and on
which version) and, for a revision, the original it replaces, so the person
compares and decides.

### 6. The last word is handed over per kind of decision, by the person

A workspace setting lists the decisions an assistant may take on its own.
It is a closed set of kinds (approving or declining a synthesis, a link
proposal, a fact), empty by default, written only by a person. A decision an
assistant takes under it is recorded with the assistant as the decider,
listed as decided by an agent, and reversible like any other. Which kinds
count as simple is the person's choice, made in the setting, not in code.

## Phases

| phase | delivers |
|---|---|
| A | decisions 1 to 5 |
| B | decision 6, with the measure of how often the person reverses an agent's decision |

### Phase A as built (2026-11-24)

| decision | where | checked by |
|---|---|---|
| 1 | `garden.review.read` at `notes:read`; its description tells an agent that a proposal is not memory | `api/tests/test_garden_review_api.py`, with an assistant credential |
| 2 | `proposal_check` (migration 0036), `garden.review.check` (`POST /garden/review/check`, MCP `garden_review_check`); the verdict is refused when it disagrees with the findings | `core/tests/test_garden_review.py`, the API test |
| 3 | `notes.revises_note_id` (migration 0036), `garden.review.revise` (`POST /garden/review/revise`, MCP `garden_review_revise`); the signature moves to the revision | the API test asks for the same pattern again and gets the revision; it was observed failing with the decline charged to the model |
| 4 | `synthesized_text` and `origin_model_id` on `extract_cluster_pattern` and `synthesize_season`; the cap and the provenance rule are `decomposition.external_text` and `external_model_id`, shared by every text an agent writes | `core/tests/test_garden_review.py` |
| 5 | the reading view shows the checks (verdict and findings in words, the model, stale when on an earlier version), the original under a revision, and the revision of a replaced note | `web/src/routes/GardenProposalRoute.test.tsx`, observed failing with both panels removed |

### After phase A, on production (2026-11-25)

- The MCP gateway expands the short ids it hands out through the prefix
  resolver, which applied the review gate; an assistant could list a
  proposal and not address it. Name resolution now includes gated notes
  (2.4.20); the tools still decide what they may do with one.
- One source of 93,218 characters made a reading 96,184 characters, more
  than a client takes as one result. An agent's reading shares a budget of
  24,000 characters among the sources, says which were cut and how long they
  are, and `garden.review.read_source` reads the rest in windows; the
  person's reading asks for the whole (`full_text`).
- A person pasting a proposal's code found nothing, because the lookup
  applied the gate too, and the review pages showed no code to copy. The
  lookup resolves a proposal when asked to resolve a code (`include_gated`,
  never for a list of things to link to) and routes it to the reading view;
  the reading view and the review list show the code with a copy button.

## Consequences

- The strong model a person already uses through MCP becomes the corrector
  of the small models running in production, without either taking effect
  unseen.
- The accept ratio of ADR-0043 D4 keeps counting a person's verdicts only: a
  revision is not one.

## Alternatives rejected

- **Let an assistant edit a pending proposal in place**: the person would
  approve a text without seeing that, or how, it changed, and the record of
  what the small model wrote would be lost.
- **Keep the read person-only and let the assistant read through the source
  note**: the source is not the proposal; what needs checking is what the
  model wrote.
- **Let a check without objections approve**: that is decision 6 without the
  person choosing it.

## What this does not cover

- Correcting facts of the knowledge graph and link proposals; they follow
  the single proposal contract of ADR-0075 phase 2.
- Whether a check is right: a check is evidence for the person, and its
  reliability is what phase B measures.
