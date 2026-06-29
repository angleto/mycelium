# ADR-0044 — Temporal knowledge graph (Track B)

Status: Accepted (2026-06-29)

## Context

mycelium's retrieval is a strong hybrid (dense + lexical RRF, rerank, the
"micelio" note graph with PPR/Leiden/walk, humus). What it lacked, and what
the open-source field (Cognee, Zep/Graphiti) wins on, is two things:

1. **LLM-extracted entity/relation facts** in a typed, queryable knowledge
   graph — so cross-entity multi-hop questions ("which projects did X and Y
   share") are native, not approximated by text similarity.
2. **Bi-temporal reasoning** — every fact carries *when it is true in the
   world* and *when the system believed it*, with contradictions handled by
   invalidate-not-delete and answerable as-of any instant. This is the
   least-saturated axis on the memory benchmarks.

The note graph (`note_note_link`) is the wrong home for this: it is a closed
4-verb garden metaphor, is hard-wired into graph.py centrality with
"byte-identical" invariants, and has no temporal/validity columns. So Track B
adds a **separate** temporal KG, reusing the existing seams (metered LLM,
review/proposed gating, RLS, audit, identity provenance) rather than new
infrastructure.

## Decision

Two new org-scoped, FORCE-RLS tables (migration 0067), modelled on
`entity_revision`'s seal-don't-rewrite pattern:

- **`kg_entity`** — typed nodes (`entity_type` closed CHECK: person /
  organization / project / place / product / event / concept / other),
  resolved/deduped per `(org, type, normalized_name)` (UNIQUE), with
  `origin_model_id` + `created_by` provenance.
- **`kg_edge`** — typed relation facts. `predicate` is an **open** normalized
  vocabulary (no CHECK — a KG relation space is not enum-like). Bi-temporal:
  - valid-time `valid_from` / `valid_to` (true in the world);
  - transaction-time `created_at` (asserted) … `invalidated_at` (stopped
    believing; NULL = currently believed) + `invalidated_by` +
    `superseded_by_edge_id`.
  - `review_state` ('proposed' when extracted autonomously, ADR-0043).

Invariants enforced at the DB:

- **At most one current fact per triple**: partial unique index
  `uq_kg_edge_current ON (org, subject, predicate, object) WHERE
  invalidated_at IS NULL`.
- **Invalidate-not-delete**: a BEFORE UPDATE trigger
  (`kg_edge_no_update_invalidated`) freezes a row once `invalidated_at` is set,
  so tombstoned history can never be rewritten (mirrors entity_revision's
  sealed-immutable trigger, migration 0006).

Two distinct write semantics (the key correctness point):

- **`supersede_fact`** = a *temporal update* (X moved from OldCorp to NewCorp):
  close the old fact's `valid_to`, chain `superseded_by_edge_id`, add the new
  fact. The old fact is NOT invalidated — it remains true for its window and
  **as-of-queryable**.
- **`invalidate_fact`** = a *correction* (the fact was wrong / a reject): set
  `invalidated_at`. It leaves every read on both axes.

Reads use the effective + as-of predicate `invalidated_at IS NULL AND
review_state IS DISTINCT FROM 'proposed' AND (valid_from IS NULL OR valid_from
<= t) AND (valid_to IS NULL OR valid_to > t)`, t = `as_of` or now.

Extraction (`services/kg.extract_facts`) runs the per-org **metered** LLM
(`resolve_llm`, op='extract'), parses triples defensively (no provider
JSON-mode), resolves entities, and writes facts — born 'proposed' when
autonomous (adjudicated via the ADR-0043 gate), effective when user-initiated.
Multi-hop / as-of query is exposed over MCP: `kg_extract`, `kg_entities`,
`kg_neighbors(entity, depth, as_of)`.

## Consequences

- Closes the two gaps where Cognee/Graphiti/Zep led (entity-relation KG +
  bi-temporal), on mycelium's own substrate — no Neo4j, no new infra; reuses
  RLS, metering, review-gating, audit, identity provenance.
- Consistent with the system's philosophy: demote/invalidate, never destroy;
  provenance + GDPR erase by `source_note_id`; autonomous output is gated.
- Deferred / follow-ups: auto-contradiction detection across notes (the
  primitives `supersede_fact`/`invalidate_fact` exist; auto-deciding which
  facts conflict is a separate LLM-reasoning step); wiring extraction into the
  autonomous ingest sweep behind a flag; feeding currently-valid kg_edges into
  graph.py's PageRank/betweenness for KG centrality; the external benchmark
  harness (B3) that measures multi-hop/temporal/governance.
