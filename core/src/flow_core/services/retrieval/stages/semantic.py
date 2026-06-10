"""Semantic dense stage: pgvector HNSW (``vector_ip_ops`` index after
migration 0007) ordered by ``max_inner_product`` against the query
embedding. Early-exits when the query couldn't be embedded (no
optional dep, dim mismatch upstream) so the pipeline degrades to
lexical-only without raising.

Two embedding tiers (task 5276207e), fused by RRF: the LOCAL tier
(``embedding`` vector(1024), always present) and the optional HOSTED
tier (``embedding_hosted`` halfvec(4000), per-org Scaleway). The query
is embedded by whichever tiers the org has: ``ctx.query_embedding``
carries the local vector, ``ctx.extras['query_embedding_hosted']`` the
hosted one. When both exist the stage emits TWO branches and merges
them so RRF fuses local-only rows (no hosted tier / not yet backfilled)
with hosted rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text

from flow_core.embedder import EmbedResult
from flow_core.models.memory_blob import MemoryBlob
from flow_core.services.retrieval.types import (
    Candidate,
    RetrievalContext,
    Stage,
    merge_candidates,
)


@dataclass
class SemanticDenseStage(Stage):
    name: str = "semantic"
    oversample: int = 50
    # Minimum cosine similarity a neighbour must clear to enter the
    # candidate set. Embeddings are L2-normalized so inner product =
    # cosine; pgvector ``<#>`` (``max_inner_product``) returns the
    # NEGATED inner product, hence ``cosine = -distance``. 0.0 disables
    # the gate (every kNN row is kept, the historical behaviour). A
    # positive floor drops far neighbours so a keyword/proper-noun query
    # with no genuine semantic match doesn't flood the fusion with noise
    # that ties (rank-only RRF) with the real lexical hits. Per-org,
    # tuned from the admin GUI (Organization.settings).
    min_similarity: float = 0.0

    def _keep(self, distance: float) -> bool:
        # distance = -cosine; keep when cosine >= floor. The floor only
        # bites when > 0 so the default is a true no-op (preserves the
        # historical "keep every kNN row, including slightly-negative
        # cosine" behaviour exactly).
        if self.min_similarity <= 0.0:
            return True
        return -float(distance) >= self.min_similarity

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        # No embedding at all -> degrade to lexical-only.
        local: EmbedResult | None = ctx.query_embedding
        hosted: EmbedResult | None = ctx.extras.get("query_embedding_hosted")
        if local is None and hosted is None:
            return candidates
        # pgvector iterative_scan: filter (org/project/tag) runs BEFORE
        # kNN so a selective tag still surfaces matches. SET LOCAL
        # keeps the GUC bound to the current transaction only. Set
        # once even if we run both branches.
        await ctx.session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        new: list[Candidate] = []
        # Hosted tier first so its rank position is lower (= more important
        # to RRF) on rows where both tiers exist.
        if hosted is not None:
            dist_hosted = MemoryBlob.embedding_hosted.max_inner_product(hosted.vector)
            stmt_hosted = (
                select(MemoryBlob.id, dist_hosted)
                .where(
                    MemoryBlob.org_id == ctx.org_id,
                    ctx.project_pred,
                    MemoryBlob.embedding_hosted.is_not(None),
                    *ctx.tag_clauses,
                )
                .order_by(dist_hosted)
                .limit(self.oversample)
            )
            rows_hosted = (await ctx.session.execute(stmt_hosted)).all()
            new.extend(
                Candidate(
                    blob_id=row[0],
                    scores_by_stage={f"{self.name}_hosted": float(rank)},
                )
                for rank, row in enumerate((r for r in rows_hosted if self._keep(r[1])), start=1)
            )
        if local is not None:
            dist_local = MemoryBlob.embedding.max_inner_product(local.vector)
            stmt_local = (
                select(MemoryBlob.id, dist_local)
                .where(
                    MemoryBlob.org_id == ctx.org_id,
                    ctx.project_pred,
                    MemoryBlob.embedding.is_not(None),
                    *ctx.tag_clauses,
                )
                .order_by(dist_local)
                .limit(self.oversample)
            )
            rows_local = (await ctx.session.execute(stmt_local)).all()
            new.extend(
                Candidate(
                    blob_id=row[0],
                    scores_by_stage={self.name: float(rank)},
                )
                for rank, row in enumerate((r for r in rows_local if self._keep(r[1])), start=1)
            )
        return merge_candidates(candidates, new)
