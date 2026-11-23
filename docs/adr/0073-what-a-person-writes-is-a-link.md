# ADR-0073: What a person writes in a body is a link, and the graph reads it without guessing

Status: Proposed (2026-11-23)
Relates to: ADR-0038 (the UUID-prefix chip; this record reopens its
deferred item #F), ADR-0039 (the decomposition pipeline, whose existence
was the condition #F waited for), ADR-0040 (the typed note link model,
which this record leaves unchanged), ADR-0043 and ADR-0071 (a model's
output is born proposed and a person decides), ADR-0056 (the indexing
class on the row), ADR-0068 (one operation registry for REST and MCP),
ADR-0031 and ADR-0033 (edge weights and anti-monoculture in the graph).

This record decides how a reference a person writes inside a note or a
task becomes an edge of the graph and a backlink on its target, how that
edge is kept exactly in step with the text, how suggestions for links
nobody wrote are made and judged, and what "better than a backlink panel"
means here in terms that can be measured.

## Context

### What exists

A body refers to another entity in three ways, all written as plain
Markdown so the text round-trips:

- a mention link, `[label](@note:<uuid>)` / `@task:` / `@tag:`, inserted
  by the `@` typeahead (`web/src/lib/mentions.ts`);
- a chip, an 8-hex id prefix in backticks, inserted by the `[[` typeahead
  and written by hand and by agents (ADR-0038);
- a bare id or prefix in prose, which the renderer leaves inert and the
  one-shot `normalize_note_uuids` pass rewrites into a chip when it
  resolves.

None of the three reaches the graph. Nothing on the server parses a body
for references: backlinks (`note_links.list_note_links`, the "Linked here"
panel) and every graph computation (`graph.compute_note_edge_weights`,
PageRank, `graph_local.bounded_neighborhood`, the `graph` retrieval
branch) read only the typed rows of `note_note_link` and
`note_task_link`, which exist only when someone calls the link operation.

### What was measured

On production data, 2026-11-23, archived notes included:

- **No body contains a mention link.** A substring scan of every part
  for `@note` and `@task` (`list_notes(q=...)`, which is an `ILIKE` over
  every part's body and title) returned nothing. The same instrument
  returns the expected notes for an 8-hex id, so it reads bodies. The
  references in use are chips and bare ids.
- **Written references and typed links are complementary, not
  redundant.** Seven notes known to be cited were probed (chosen
  because they were known to be cited, so this is not a random sample
  and the rates below describe hubs, not the corpus). 26 other notes
  carry one of their ids in the text; 9 of those 26 pairs also have a
  typed link in either direction, 17 have none. Conversely 9 typed links
  of the same seven notes have no written citation. Of the 35 related
  pairs, the typed graph sees 18 and the text sees 26.

The instrument counts an id occurring anywhere in a part, so it cannot
tell a chip from a bare id or from an id inside a code fence; the
projection below has to make that distinction, and its first backfill is
the measurement that replaces this one.

### The bar

The reference point is the backlink model of file-based Markdown tools
such as Obsidian: a link is `[[Note name]]`, resolved by name; a backlink
panel lists every file linking to the current one with the surrounding
line; "unlinked mentions" are occurrences of the note's name in text, by
string match, recomputed on demand. Doing that is not the goal. Each
property below is one where this design is meant to be measurably
stronger, and the instrument that checks it is part of the decision.

## Decision

### 1. A written reference is a row of a derived projection

A new org-scoped table, `body_reference`, under the same RLS as every
tenant table, holds one row per reference occurrence:

- the source: a note part or a task description, with the body hash it
  was computed from;
- the token as written, its syntax (`mention`, `chip`, `bare`) and its
  ordinal among equal tokens in the source;
- the target kind (`note`, `task`, `tag`), the target id when resolved,
  and a status: `resolved`, `ambiguous` (the prefix matches more than one
  live entity) or `dangling` (it matches none);
- an anchor in the rendered domain (W3C text-quote: exact, prefix,
  suffix), computed by `md_anchor`, the module that already maps rendered
  quotes back to source offsets;
- `first_seen_at`, the commit that first held the occurrence, NULL for
  occurrences that predate the projection.

The table is a projection and nothing else: no surface writes it, every
row is recomputed from the body, and dropping it loses no information.
It is not a fifth kind of `note_note_link` (see the alternatives).

A bare token counts as a reference under exactly the gate the normaliser
uses: outside fenced and inline code, links and URLs, and only when it
resolves. Reading it in place is what the normaliser's rewrite was for,
so the projection makes that rewrite of a person's text unnecessary.

### 2. One rule for what a reference is

The extraction lives in one core module. The renderer keeps its own
recogniser, because it decorates text that has not been saved yet; the
two are held together by a shared fixture corpus that both the Python
and the TypeScript suites run, and that fails on any input where they
disagree. Disagreement is a failing test, not a tolerated drift.

### 3. A resolution is fixed on the occurrence and is never guessed

An occurrence resolved when it was written keeps its target for as long
as its token is unchanged in the source. A prefix that later collides
with a new entity does not move it: the reference means what it meant
when it was written. An occurrence that is ambiguous when written stays
`ambiguous`, is shown as such on both sides, and is resolved only by a
person choosing the target; that choice is stored on the occurrence. No
heuristic picks a target.

### 4. The projection commits with the text, and staleness is visible

The projection is a second consumer of the dirty sets that already keep
the search index in step with the text (`note_search`, `task_search`):
the mapper listeners and the explicit marks at the Core-update choke
points of `note_parts`, drained in `db.tenant_session` before commit.
A backlink therefore exists exactly when the body that contains it has
committed, in the same transaction.

Completeness of that mechanism depends on every write path marking its
part, and a forgotten path is the failure it cannot see by itself. So
each source records the body hash its rows were computed from, and the
garden loop runs a verifier that compares it with the live body, rebuilds
what differs and reports how many it found. The count is a garden-health
sensor whose expected value is zero; a non-zero value names a write path
to fix where it originates.

### 5. Backlinks are a registry operation with context

The incoming side of a note, a task or a tag gains a `written` section
beside the typed links: each source, the sentence the reference sits in
(from the anchor), a link to that paragraph, the syntax, and whether the
pair also has a typed link. It is declared once on the operation
registry, so REST and MCP serve the same shape. Sources and targets that
are trashed, awaiting review or rejected are excluded by
`effective_note_clause`, as the typed backlinks already are; archived
ones are shown and labelled, as the chip already does.

### 6. Written references enter the graph as their own evidence

`compute_note_edge_weights` gains written references as a fourth evidence
source, soft-OR'd with the existing three, with its own base weight.
They also seed `graph_local.bounded_neighborhood`, and so the `graph`
retrieval branch and PageRank. The weight is not chosen here: it starts
equal to `related` and is fixed by the retrieval evaluation (see 8),
behind a setting that is off until that evaluation has run. The
backlinks of 5 do not wait for it, since they show what was written and
rank nothing.

A source at `index_scope='none'` is projected (backlinks are navigation
for the people who can already read it, and ADR-0056 makes the scope an
indexing class, not a read boundary) but contributes nothing to weights,
proximity or retrieval, which are automatic indexing.

### 7. Links nobody wrote are proposals, judged by a person, from any proposer

Suggestions are made by proposers. A proposer is an identity that
submits link proposals through one registry operation; the built-in
proposers run in the garden loop, and any other proposer uses the same
operation over the public API with a scoped credential. The core imports
no proposer and works fully with none but its own.

- A proposal is born `proposed` and takes effect only when a person
  accepts it (ADR-0043, ADR-0071).
- Accepting creates a typed link of the kind the person picks. It never
  edits either body: turning an unlinked mention into a chip would be an
  automatism rewriting a person's text.
- A rejection is remembered per (source, target) pair and no proposer
  proposes that pair again; a person can still link it by hand.
- Every decision is attributed to the proposer that made the proposal,
  so each proposer has a measured precision: accepted over decided, with
  its confidence interval (`eval_stats.wilson_ci`).
- The built-in proposers in v1: the existing `link_add` candidates
  moved onto this seam, and an unlinked-mention proposer that matches a
  note's title in other bodies, normalised and on word boundaries, with
  a gate that refuses titles too short or too common to be evidence (a
  title's document frequency in the workspace, not a fixed length).
- A proposer is shown at most a bounded number of open proposals per
  note at a time, so a noisy one cannot bury the review queue while its
  precision is still being measured.
- **The origin survives acceptance.** A typed link created by accepting a
  proposal records the proposer's identity and the proposal's evidence
  reference beside the person who accepted it, and every read of the
  link (REST and MCP) returns all three. `created_by` alone would name the
  person and erase the proposer, so a system reading the graph back
  would take a conclusion it proposed for independent evidence of
  itself. Three origins stay distinguishable on read: written in a body
  (this projection), linked by hand, accepted from a proposer.
- **Exposure is recorded, not only decisions.** Each time a proposal is
  shown, the record keeps to whom, where and at what position, and a
  proposal that expires undecided is recorded as such. Accepted over
  decided is enough to rank proposers in the queue; it is not a label set
  a proposer can learn from, because counting only what was decided
  rewards a proposer for where it already looks.
- **A proposer's score orders the queue and is not shown beside the
  proposal.** A person shown a confidence anchors on it, and the
  precision measured from their decisions would then measure the score
  as much as the proposal.

### 8. What "better" means, and what measures it

| Property | Name-based backlinks | This design | Instrument |
|---|---|---|---|
| A rename keeps the link | depends on rewriting every linking file | by construction: targets are ids | test: rename, backlinks unchanged |
| An ambiguous target | resolved by a path heuristic | `ambiguous`, a person chooses | test: colliding prefixes stay ambiguous; later collision does not move a resolved one |
| Context of a backlink | the line | the sentence, anchored, opens the paragraph, survives edits | `md_anchor` round-trip over the fixture corpus |
| What a link means | one kind | four typed kinds plus written references as a separate layer | typed and written counts both reported per pair |
| What can be linked | files | notes, tasks, tags | test per target kind |
| Unlinked mentions | string match, recomputed, no memory | proposals with remembered rejections and per-proposer precision | precision and interval on garden health |
| Links affect search | no | weights, proximity, PageRank | retrieval A/B (below) |
| A stale link is detectable | the file is the truth | drift verifier | sensor value, expected 0 |
| An agent sees the same | through a plugin | the same registry operation on REST and MCP | registry parity test |

The gates, all on the revision they report:

- **Projection exactness** (blocking): the fixture corpus, including
  code fences, inline code, links, URLs, hex that is not an id, colliding
  prefixes, the three syntaxes and edits that add, move and remove
  tokens; extraction is idempotent and a body without a token has no
  rows.
- **Freshness** (blocking): for every write function of `note_parts`,
  `notes` and `tasks`, a test that writes a reference through it and
  reads the backlink in the same transaction's aftermath. A new write
  function without such a test fails a test that enumerates them.
- **Retrieval** (non-blocking, as every evaluation here): the existing
  offline evaluation with and without written references in the graph;
  the setting of 6 is turned on only if recall@10 and MRR are not lower
  beyond the bootstrap interval, and the run is recorded with its
  revision.
- **Proposals**: precision per proposer from decisions; the built-in
  unlinked-mention proposer stays on only if, after its first 50
  decisions, the lower bound of its interval is above the floor a person
  sets then. The floor is not chosen before there are decisions to
  read.

### 9. Backfill

One idempotent pass projects every existing part and task description.
Occurrences found by it have `first_seen_at` NULL. Its report (counts by
syntax and status, ambiguous and dangling occurrences listed) is the
corpus-wide measurement that replaces the sample in the context.

## Consequences

- Every reference written so far, by a person or by an agent, becomes a
  backlink at backfill, with no change to any body.
- The graph gains its densest source of evidence, the one people produce
  without thinking about the graph, and it enters ranking only after the
  evaluation says it helps.
- `normalize_note_uuids` is no longer needed to make bare codes
  navigable. Retiring it is a separate change.
- Two recognisers exist (core and renderer), held together by a test of
  agreement rather than by care.
- The write path carries one more step before commit; it is a parse and
  one batched prefix lookup per dirty source, no model call. Its cost is
  measured on the backfill and stated in the code that bounds it.
- External proposers can improve suggestion quality without the core
  depending on them, and their contribution is measured by the same
  decisions as the built-in ones.

## Alternatives rejected

- **`[[Note name]]` as the canonical link.** Name-based: a rename has to
  rewrite every linking body, two notes with one name need a tie-break,
  and it would be a second syntax beside the chip, with a migration and
  no gain the id does not already give. The `[[` keystroke stays as the
  typeahead that inserts a chip.
- **Written references as a `mentions` kind of `note_note_link`.** It
  mixes what a person asserted with what is derived from text: `link_prune`
  would propose deleting a citation that is still in the body, the
  uniqueness of (parent, child, kind) collapses occurrences and their
  contexts, and tasks and tags would need yet another table anyway.
- **Resolving references at read time only.** Backlinks would need a
  scan of every body on every read, and the graph would still have no
  input.
- **A database trigger that parses bodies.** A second implementation of
  the Markdown rule, in PL/pgSQL, beside the one the renderer and
  `md_anchor` already mirror.
- **Re-resolving every occurrence on every read.** A new entity sharing a
  prefix would silently move old references to a target their author
  never meant.
- **Creating typed links from written references automatically.** A
  citation does not say which of the four kinds the person means, and
  choosing one for them is a model or a rule speaking for the person.
- **Inserting chips for accepted unlinked mentions.** An automatism
  editing a person's text; accepting creates a typed link instead.

## Open

- Whether a source at `index_scope='none'` should store the sentence of
  each reference, or only its location (decision 6 stores it, on the
  reading of ADR-0056 as an indexing class).
- Aliases for titles, which would let the unlinked-mention proposer
  match more than the title, are out of scope here.
