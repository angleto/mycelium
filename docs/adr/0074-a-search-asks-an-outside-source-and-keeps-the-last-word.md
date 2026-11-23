# ADR-0074: A search can ask a source outside the workspace, and the workspace keeps the last word

Status: Proposed (2026-11-23)
Relates to: ADR-0005 (hybrid retrieval and RRF), ADR-0034 (a parallel
retrieval source with a fixed boost and a hard cap, the precedent this
record generalises), ADR-0035 (search clicks), ADR-0037 (an online
learning loop that is event-sourced, workspace-scoped and reversible),
ADR-0056 (the indexing class on the row), ADR-0062 (a score has a scale),
ADR-0068 (one operation registry for REST and MCP), ADR-0073 (written
references, and proposals from any proposer).

This record decides how a search in the workspace can include results
held by an information service outside it, such as books, documents
loaded at volume and analyses derived from them. It also decides who
owns what between the two systems, how the combination is measured and
learns as data grows, and how the same design lets the outside service
answer alone for an organisation if that one day proves better, without
rewriting anything.

## Context

### Two systems, two roles

The workspace is where people and agents communicate and coordinate:
notes, tasks, comments, decisions, the links people write or accept.
Ideas are born and developed there. An information service is a
different kind of system. It ingests sources at volume, extracts,
resolves entities, analyses, and answers with evidence. Building that
second kind of system inside the workspace would blur what each one is
for. Its tools for answering and analysis would compete with the
service's tools and lose, and the workspace would carry their weight.
The two together must give what neither gives alone, and a reader must
always be able to tell which one a piece of information came from.

### What exists, measured on the code at 1daf4f4d

- **Unified search** (`task_search.search_unified_with_meta`) runs three
  independent branches (task, note, blob). Each is a `memory.retrieve`
  over memory blobs with RRF inside the branch. The branches run in
  sequence and their results merge by score.
- **A candidate is a memory blob.** `retrieval.types.Candidate` is keyed
  by `blob_id: UUID`, and `merge_candidates` dedupes on it. A result
  that is not a row of this database has no identity in the pipeline.
- **Fusion weights are fixed.** `RRFFusionStage.weights` is a constant
  map per branch, and nothing learns it.
- **A precedent for a parallel source exists.** ADR-0034's atom source
  is late-fused with a fixed small weight and then capped at a fraction
  of the slots, so it cannot crowd out live results. It is the right
  shape, but it only works for sources whose items are blobs.
- **`RetrievalMeta` already reports degradation** (the embedding is
  missing, a floor fired, a rerank was skipped and why), so that an
  empty or weaker result is never silent.
- **Clicks are logged, impressions are not.** `search_clicks.hit_id` is
  a UUID of a task, note or blob, and nothing records what was shown.
  Without exposure, a click cannot be credited to a ranking. Position
  bias then makes raw clicks a biased training signal.
- **The gold sets are labelled on workspace ids only.** An offline A/B
  run against them counts every relevant outside result as irrelevant.
  The measurement would be biased against the very thing it is meant to
  judge.

## Decision

### 1. One owner per datum, and a reference everywhere else

| Owned by the workspace | Owned by the outside service |
|---|---|
| what people and agents write, say, decide and coordinate: notes and their parts, tasks, comments, links written or accepted, approvals | sources it ingests (books, documents, corpora), everything it derives (extractions, entities, analyses, answers with evidence) |
| identity of its entities, and who may read them | identity of its items, and its own access labels |
| fast lookup over what it owns: search, typeahead, exact id lookup, backlinks | answering, analysis, evidence reports |

- A datum has exactly one owner. The other system holds a **reference**
  to it: a workspace entity id with a text-quote anchor, or an outside
  item reference. At most it holds a revocable copy, under the erasure
  contract.
- An item the outside service derived from workspace content keeps the
  workspace entity as its origin.
- The workspace does not grow answering or analysis tools. Its existing
  atom decomposition (ADR-0039) and knowledge-graph extraction
  (ADR-0044) stay at their current scope and receive no new features.
  Whether to retire them in favour of the outside service is decided by
  measurement, in a separate record.
- Lookup, typeahead, exact id resolution and backlinks (ADR-0073) are
  coordination functions, so they stay local under every configuration
  in this record. Coordination must never wait on a network hop.

### 2. A source protocol, not an integration

The workspace calls a **retrieval source** through a versioned HTTP
contract of its own. No particular service is wired in. Any service
that implements the contract can be a source.

- **Request:**
  - the query;
  - a deadline;
  - k;
  - the language;
  - an opaque per-organisation tenant reference;
  - the person's credential for the source (decision 9);
  - optional filters.
- The source decides what a person may see among its own items, from
  a credential it can verify itself. Neither system takes the other's
  permissions as authoritative.
- The workspace's service credential alone authorises only items that
  every member of the organisation may read.
- The actor identity is stable per person across requests. The source
  may use what a person has already examined to adjust its answers to
  that person's later questions. An identity that rotates per request,
  or one shared across an organisation, would break that adjustment.
- The tenant maps to one isolated data plane of the source, and the
  mapping is configured on the source's side. A wrong tenant value is
  refused rather than read against another plane.
- **Response:** a ranked list of items, each with:
  - `ref` (opaque to the workspace);
  - `refers_to`, either an outside item or a workspace entity (kind, id
    and a text-quote anchor);
  - title;
  - a snippet with its anchor;
  - rank;
  - an opaque source score;
  - provenance (which source, and which derivation inside it);
  - a way to open the item, either a URL or a resource the source
    serves over its own MCP.
- The contract is versioned, and a response with an unknown major
  version is refused, not interpreted.
- The repository ships a **reference source**, a small real server
  over a fixture corpus, plus contract tests. The workspace's
  behaviour against a source is tested end to end, not against a stub.
- MCP is not used between the two servers. MCP is how agents and people
  talk to each system, and each system keeps its own MCP surface. The
  workspace's MCP does not proxy the outside service's tools: that would
  bind the outside service to the workspace and duplicate its
  authorisation and audit.

### 3. The workspace decides what a caller may see, always

- A source item whose `refers_to` is a workspace entity is **resolved
  by the workspace** under the caller's row-level security. The usual
  checks apply: trashed, proposed and rejected entities are excluded,
  and so is `index_scope='none'`.
- An item that does not resolve is dropped and counted in the
  metadata. The source's view of workspace permissions is never
  trusted.
- A resolved item dedupes with the local hit for the same entity. One
  entity takes one row, and the row records every origin that found it.
- The generic hit key becomes `(origin, ref)`. Local hits have origin
  `local` and their existing ids. The blob-keyed `Candidate` stays
  inside the local branches. The cross-origin merge happens above them,
  in unified search, and no outside item is forced into a `blob_id`.
- Outside items are not persisted beyond the response. The workspace
  logs their `ref` (decision 6), not their content. Licensed material,
  such as a book, never becomes a workspace row by being searched.
  Because nothing is cached, an item whose source was erased upstream
  disappears from the next response, with no erasure to propagate.
- A snippet and its anchor are a pointer for the reader, not evidence.
  When an outside item turns into something the workspace keeps, such
  as a link accepted through the proposer seam of ADR-0073 decision 7,
  it keeps the source as its origin, with the source's evidence
  reference.

### 4. Execution: concurrent, bounded, and never silent

- The source call starts at the beginning of the search, concurrently
  with the local branches. It is an HTTP call and does not share the
  database session.
- The wait is bounded by a per-organisation deadline. A circuit breaker
  per source stops calling a source that keeps failing, and probes it
  again on a schedule.
- `RetrievalMeta` gains one entry per source:
  - status: `ran`, `timeout`, `unavailable`, `breaker_open`,
    `disabled` or `refused_version`;
  - items returned, items resolved, items dropped;
  - latency.
- A degraded search returns the local result and says so. It never
  returns a shorter list without saying why.
- Scale: the cost the workspace adds per query is one concurrent call
  and a resolution query bounded by k. Nothing in the workspace grows
  with the size of the source's corpus.

### 5. Fusion by rank, weights per organisation, and a cap

- Outside and local lists fuse by RRF on ranks. Source scores are
  opaque and have no common scale with local scores (ADR-0062), so they
  are never compared.
- Each source has, per organisation, an RRF weight and a cap on the
  share of slots it may take, as in ADR-0034. The starting values are
  conservative and written in configuration.
- Every hit carries its origin to the caller. On every surface, REST,
  MCP and the web, a reader can tell an outside item from a workspace
  entity.

### 6. Learning from use, without learning from position

- **Exposure is logged.** For every search: an id, the ranked list
  shown, the origin and ref of each item, and its position.
- **Clicks are generalised** to `(origin, ref)` and reference the
  exposure they came from.
- **Weights move only by interleaving.** On a configured fraction of
  searches the result is a team-draft interleaving of two rankings: the
  current weights and a candidate setting. A click credits the ranking
  that contributed the clicked item. This cancels position bias by
  construction, which raw click rates cannot do.
- A candidate setting is promoted only when the sign test over decided
  interleavings (`eval_stats.binomial_tail_pvalue`) rejects "no
  preference". Until then the current setting stays.
- Weight history is append-only and reversible, with the same
  properties ADR-0037 gives the garden loop: event-sourced, scoped to
  the organisation, and able to rewind.
- **The source learns too, if the organisation allows it.** Exposure
  and click events about a source's own items are sent back to that
  source. Nothing about local items is sent. These events are signals
  of use with recorded position and exposure, so they can be used off
  policy for ranking. They are not verified labels. With this, the
  source's ranking and the fusion weights both improve as data and use
  grow, and the workspace does not have to change for either to happen.

### 7. Evaluation that does not prejudge the outcome

- **Offline:** the gold sets gain labels on outside refs by pooling. A
  judge labels the union of the top k from both arms, so an outside
  result is not irrelevant merely because nobody had labelled it. The
  gate is no regression on the local-only gold, with recall@10 and MRR
  inside the bootstrap interval. The configuration is checked with the
  source enabled and with it absent.
- **Online:** the interleaving of decision 6, which answers the
  question that matters ("did people prefer the combined ranking?") on
  real queries.
- The source's precision is reported per source, in the same garden
  health surface as the other sensors.

### 8. The path to "the outside source answers alone"

- The set of sources per organisation is configuration, and `local` is
  one of them, with a weight like any other.
- Suppose a source indexes the workspace itself, as a reader of it under
  the erasure contract and respecting `index_scope`. Then letting the
  source answer alone means setting the weight of local search to zero
  for that organisation. That is a configuration change, and the same
  interleaving gate of decision 6 decides it.
- The local index keeps being maintained in that configuration. It is
  the fallback when the source is degraded (decision 4), and it serves
  the coordination functions of decision 1. Moving from "both" to "the
  source alone" therefore removes no capability and creates no single
  point of failure for lookup. Moving back is the reverse change.
- Decision 3 holds in every configuration: the workspace still resolves
  and filters what the source returns.

### 9. Opt-in, per organisation

- A source is disabled until a person with the owner role enables it
  for an organisation. Enabling records the source, its contract
  version, whether feedback is shared, and who enabled it.
- Each person who wants the source's restricted items links their own
  account once, with a delegation the source issues and can verify. The
  workspace is not the identity provider. A person who has not linked
  an account receives only what the organisation-wide credential
  authorises.
- Credentials and delegations are stored encrypted
  (`crypto.encrypt_secret`), as the payment connectors store theirs.
- Query text leaves the workspace only for organisations that enabled a
  source.
- Entities at `index_scope='none'` are never sent to a source, never
  accepted back from one, and never shown from one.

## Consequences

- Unified search gains a cross-origin merge above the existing branches.
  The local pipeline, its stages and its blob-keyed candidates are
  unchanged.
- The local branches should also run concurrently with each other. That
  is a separate change, measured on its own.
- New tables: retrieval sources per organisation, exposure, generalised
  clicks, and weight history. The existing `search_clicks` rows migrate
  to origin `local`.
- The garden health sensor `recall_at_k`, which reads clicks today, is
  recomputed over exposure. That makes it more correct even with no
  outside source enabled.
- An outside service can be offered to people who do not use the
  workspace, because nothing in the protocol depends on the workspace.
- The workspace takes on the cost of operating a network dependency in
  search: timeouts, a breaker, and version checks. Decision 4 bounds
  that cost and makes it visible.

## Alternatives rejected

- **Store outside items as memory blobs, so the existing pipeline
  finds them.**
  - It copies content the workspace does not own, licensed books
    included.
  - It makes the workspace a second owner, which breaks decision 1.
  - Its index grows with the outside corpus, which is the opposite of
    the scale property in decision 4.
- **A stage inside `memory.retrieve`.** The pipeline's identity is a
  blob id, and every outside item would need a fake one. The merge
  belongs above the blob-keyed branches.
- **Normalise source scores and sort by them.** There is no common
  scale. A calibration learned per source would still drift whenever
  the source changes its model.
- **Learn the weights from raw click-through.** Position bias would
  reward whatever was already on top. Interleaving is the instrument
  that removes it.
- **Proxy the outside service's MCP tools through the workspace's
  MCP.** This binds the two systems, duplicates authorisation and
  audit, and blurs the roles of decision 1.
- **Let the outside service replace workspace search outright, with no
  local index.** Lookup and coordination would then depend on a network
  hop, and a degraded source would make the workspace unusable.
  Decision 8 reaches the same end state for answering while keeping
  coordination local.

## Open

- **Citing an outside item from a note.** A body reference syntax for
  an outside `ref`, projected like the references of ADR-0073, would let
  an idea developed in a note point at the evidence it grew from. It
  depends on ADR-0073 being built first.
- **The fraction of searches that are interleaved**, and the minimum
  number of decided interleavings before a promotion. These are set
  when exposure has been logged long enough to estimate the click rate.
