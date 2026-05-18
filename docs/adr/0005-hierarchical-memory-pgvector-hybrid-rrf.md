# ADR-0005 Hierarchical memory on pgvector, hybrid RRF retrieval

Status: accepted.

## Context

We need a multi-level memory that summarizes the old, sends it to
semantic retrieval and finds it again, on the DB (not an app-side numpy
store). The user explicitly requested the lexical branch in hybrid
search (it had been proposed as optional; it is now baseline).

## Decision

Hot/warm/cold tiers. Cold = embedding in `pgvector` with an HNSW
index. Baseline hybrid retrieval: a semantic branch (HNSW) + a lexical
branch (`tsvector`/`ts_rank`, `pg_trgm` with a dedicated trigram
index), fused with **RRF** (rank-based, k around 60, no normalization
of incommensurable scores). K oversampled per branch (around 100)
before fusion; deterministic tiebreak; fusion within (org, project).
For very selective filters (message-id, invoice number) an exact path,
not HNSW. `hnsw.iterative_scan = relaxed_order` tuned. Pluggable
embedding (ADR-0012) with `model_id`+`dim` per blob and a re-embedding
job (new column/table, dual-write, `CREATE INDEX CONCURRENTLY`, atomic
cutover): honest guarantee = no write downtime, possible read-latency
degradation on a single node during the backfill. Explicit N:1
provenance (`blob_sources`) for GDPR deletion; consolidation never
cross-subject. Isolation: see ADR-0007.

## Consequences

- Robust retrieval on exact and semantic matches; no score
  hyperparameter to tune (only RRF's `k`).
- "No numpy" means: no app-side numpy similarity store; the local
  `Embedder` may depend on numpy transitively.

## Alternatives rejected

- Semantic only: loses exact matches on rare tokens in free text (the
  user explicitly requested lexical).
- Fusion with weights on raw scores: `ts_rank` and cosine scores are
  incommensurable; rank-based RRF avoids the problem.
- Claiming "no read downtime" for re-embedding: not sustainable on a
  single ARM node; replaced with an honest guarantee.
