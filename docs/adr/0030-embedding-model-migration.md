# ADR-0030 — Embedding model migration (dual-column)

Status: Accepted (foundation landed, target model = bge-m3 1024d)
Date: 2026-05-26
Tracks: task `1d081395`

## Context

Flow uses pgvector dense embeddings as the semantic branch of its
hybrid retrieval (alongside FTS, fused via RRF). The legacy default
is `intfloat/multilingual-e5-small` (384d, ~118 MB, multilingual,
CPU-cheap). Two pressures push for migration:

1. Quality: SOTA multilingual embedders in 2025-2026 (bge-m3
   1024d, gte-multilingual-base 768d, OpenAI text-embedding-3 with
   matryoshka) materially outperform e5-small on MIRACL / BEIR
   benchmarks, +5-15% MRR on Italian-heavy workloads.
2. Long context: bge-m3 carries an 8k token context vs e5-small's
   512, which lets a long task / consolidated note fit in a single
   embedding without truncation.

The default switch is breaking on two axes:
- Dimension: pgvector forbids `ALTER TYPE` on `vector(N)`; the
  column dim is immutable. A new dim requires a new column.
- Vector space: vectors from different models are not
  interchangeable. Searching with model B against a corpus indexed
  with model A produces noise. Until every blob is re-embedded with
  B, retrieval quality collapses.

A big-bang re-embed of a large workspace (~M blobs) at ~50 ms each
takes ~14 hours of CPU per million. We need a strategy that
preserves online retrieval throughout.

## Decision

**Dual-column transparent migration.** Add a parallel set of
embedding columns (`embedding_v2`, `model_id_v2`, `dim_v2`) on
`memory_blobs`; populate them gradually via a worker; retrieve from
both columns and fuse via the existing RRF; cutover (drop v1 + rename
v2) only when v2 coverage reaches 100%. The system stays online
throughout, retrieval quality degrades gracefully during the window.

Five phases:

1. **Schema** (`migration 0009_embedding_v2_dual_column`): ADD
   COLUMN embedding_v2 vector(D) NULL + model_id_v2 + dim_v2 + HNSW
   index on the new column with `vector_ip_ops` (matches the v1 op
   class post-migration 0007). D is parameterised via
   `FLOW_EMBED_DIM_V2` (default 1024 for bge-m3).
2. **Provider abstraction** (`core/embedder.py`): factory
   `get_embedder_v2()` returns the v2 instance when
   `FLOW_EMBED_MODEL_V2` is set, else None. Independent override seam
   for tests (`set_embedder_v2_override`). The v1 factory
   (`get_embedder()`) reads the model name from `FLOW_EMBED_MODEL`
   (default keeps `multilingual-e5-small`) — no fork required.
3. **Dual write** (`memory.write_blob`): when v2 is configured, every
   new write populates both columns. Cost is 2x embed per write,
   accepted for the migration window. Each meter event is recorded
   separately (`op="embed"` vs `op="embed_v2"`) so the bill tracks
   the migration cost explicitly.
4. **Dual read** (`retrieval/stages/semantic.py`): SemanticDenseStage
   runs up to two branches per query — v2 (against `embedding_v2`)
   and v1 (against `embedding`). Each contributes its own per-stage
   rank; the existing RRF fusion picks winners across both. Legacy
   rows (v2 NULL) show up only in the v1 branch; migrated rows show
   in both and benefit from the higher-quality v2 score.
5. **Backfill worker**
   (`worker/embedding_migration.py` + `services/embedding_migration`):
   per-workspace sweep populating `embedding_v2` for blobs where it
   is NULL, batched (default 50/tick), race-protected via the WHERE
   `embedding_v2 IS NULL` clause. Per-org exception-isolated. No-op
   when v2 is not configured.

Admin surface: `GET /api/memory/migration-status` returns
`{total, migrated, pending}` per workspace; `POST /api/memory/migrate-embeddings`
(admin-gated) triggers the backfill synchronously with a larger
batch for impatient operators.

**Cutover** (separate migration `0NNN_embedding_cutover`, not part of
this ADR's foundation): when `pending=0` for every workspace, a
single migration drops `embedding` + `model_id` + `dim`, renames the
v2 columns to the canonical names, and rebuilds the HNSW index. This
is an operational decision (when to commit), tracked by separate
follow-up tasks.

## Alternatives considered

- **In-place ALTER TYPE**: rejected, pgvector doesn't support it. A
  manual column rewrite + index rebuild would block writes for hours
  on production-sized tables.
- **Big-bang re-embed + cutover in one migration**: rejected,
  unacceptable downtime + irrecoverable failure mode (a worker crash
  mid-migration leaves the corpus inconsistent).
- **Shadow table**: write to a parallel `memory_blobs_v2` for the
  duration of the migration. Rejected: doubles the partition + RLS +
  index maintenance; the column-based approach reuses every existing
  facet (tags, soft-delete, channels) for free.
- **Cloud-only premium**: skip the migration, route premium tenants
  to OpenAI text-embedding-3-large via API. Rejected: doesn't help
  self-hosted OSS users; the abstraction we land now sblocca cloud
  providers as a separate config without forking the path.

## Consequences

- Storage: one extra `vector(1024)` per blob + one extra HNSW index.
  ~+4 KB per blob (4 bytes × 1024) + index memory. For a workspace
  with 10k blobs: ~+40 MB + index. Acceptable for the duration of
  the migration; reclaimed by the cutover.
- Write cost: 2x embed per write during the migration. The
  embedder caches the model load (singleton), and the embed itself
  is parallel-batched in the worker; CPU overhead is real but
  proportional to the rollout rate the operator sets.
- Retrieve cost: two HNSW lookups instead of one when both vectors
  populated. Sub-millisecond per lookup, no user-visible latency.
- Rollback: drop the v2 column and the related index; reverting to
  v1-only is one downgrade migration. The dual-read code path
  silently degrades to v1 when v2 isn't populated, so an aborted
  rollout doesn't break retrieval.

## Status of related tasks

- ✓ Foundation (this ADR): migration 0009, provider abstraction,
  dual-write, dual-read, backfill worker, admin endpoints, status
  endpoint. Tests + ADR.
- Cutover migration: follow-up task to be opened when the operator
  decides v2 coverage is sufficient.
- Sparse + multi-vector (bge-m3's other modes): out of scope, ADR
  separato. pgvector 0.7's `sparsevec` makes sparse plausible; ColBERT
  / multi-vector requires extension custom or a backend swap.
