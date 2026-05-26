"""Semantic dense stage: pgvector HNSW (``vector_ip_ops`` index after
migration 0007) ordered by ``max_inner_product`` against the query
embedding. Early-exits when the query couldn't be embedded (no
optional dep, dim mismatch upstream) so the pipeline degrades to
lexical-only without raising."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text

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
        if ctx.query_embedding is None:
            return candidates
        # pgvector iterative_scan: filter (org/project/tag) runs BEFORE
        # kNN so a selective tag still surfaces matches. SET LOCAL keeps
        # the GUC bound to the current transaction only.
        await ctx.session.execute(
            text("SET LOCAL hnsw.iterative_scan = strict_order")
        )
        stmt = (
            select(MemoryBlob.id)
            .where(
                MemoryBlob.org_id == ctx.org_id,
                ctx.project_pred,
                MemoryBlob.embedding.is_not(None),
                *ctx.tag_clauses,
            )
            .order_by(MemoryBlob.embedding.max_inner_product(ctx.query_embedding.vector))
            .limit(self.oversample)
        )
        rows = (await ctx.session.execute(stmt)).scalars().all()
        new = [
            Candidate(
                blob_id=bid,
                scores_by_stage={self.name: float(i + 1)},
            )
            for i, bid in enumerate(rows)
        ]
        return merge_candidates(candidates, new)
