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
            stmt_hosted = (
                select(MemoryBlob.id)
                .where(
                    MemoryBlob.org_id == ctx.org_id,
                    ctx.project_pred,
                    MemoryBlob.embedding_hosted.is_not(None),
                    *ctx.tag_clauses,
                )
                .order_by(MemoryBlob.embedding_hosted.max_inner_product(hosted.vector))
                .limit(self.oversample)
            )
            rows_hosted = (await ctx.session.execute(stmt_hosted)).scalars().all()
            new.extend(
                Candidate(
                    blob_id=bid,
                    scores_by_stage={f"{self.name}_hosted": float(i + 1)},
                )
                for i, bid in enumerate(rows_hosted)
            )
        if local is not None:
            stmt_local = (
                select(MemoryBlob.id)
                .where(
                    MemoryBlob.org_id == ctx.org_id,
                    ctx.project_pred,
                    MemoryBlob.embedding.is_not(None),
                    *ctx.tag_clauses,
                )
                .order_by(MemoryBlob.embedding.max_inner_product(local.vector))
                .limit(self.oversample)
            )
            rows_local = (await ctx.session.execute(stmt_local)).scalars().all()
            new.extend(
                Candidate(
                    blob_id=bid,
                    scores_by_stage={self.name: float(i + 1)},
                )
                for i, bid in enumerate(rows_local)
            )
        return merge_candidates(candidates, new)
