"""Semantic dense stage: pgvector HNSW (``vector_ip_ops`` index after
migration 0007) ordered by ``max_inner_product`` against the query
embedding. Early-exits when the query couldn't be embedded (no
optional dep, dim mismatch upstream) so the pipeline degrades to
lexical-only without raising.

Embedding migration v1/v2 (task `1d081395`): during the migration
window both columns may exist. The stage picks the column to query
based on the QUERY embedding's dim: if the query was embedded with
the v2 model (configured ``embed_model_v2``), we search on
``embedding_v2``; otherwise we search on the legacy ``embedding``.
A separate ``query_embedding_v2`` slot on RetrievalContext carries
the v2 vector for the dual-read case; when both are present the
stage emits TWO branches and merges them so RRF can fuse v1-only
rows (legacy, not yet backfilled) with v2-only / v2+v1 rows.
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
        v1: EmbedResult | None = ctx.query_embedding
        v2: EmbedResult | None = ctx.extras.get("query_embedding_v2")
        if v1 is None and v2 is None:
            return candidates
        # pgvector iterative_scan: filter (org/project/tag) runs BEFORE
        # kNN so a selective tag still surfaces matches. SET LOCAL
        # keeps the GUC bound to the current transaction only. Set
        # once even if we run both branches.
        await ctx.session.execute(
            text("SET LOCAL hnsw.iterative_scan = strict_order")
        )
        new: list[Candidate] = []
        # v2 branch: rows that have been migrated. Run first so its
        # rank position is lower (= more important to RRF) on the
        # rows where both vectors exist.
        if v2 is not None:
            stmt_v2 = (
                select(MemoryBlob.id)
                .where(
                    MemoryBlob.org_id == ctx.org_id,
                    ctx.project_pred,
                    MemoryBlob.embedding_v2.is_not(None),
                    *ctx.tag_clauses,
                )
                .order_by(MemoryBlob.embedding_v2.max_inner_product(v2.vector))
                .limit(self.oversample)
            )
            rows_v2 = (await ctx.session.execute(stmt_v2)).scalars().all()
            new.extend(
                Candidate(
                    blob_id=bid,
                    scores_by_stage={f"{self.name}_v2": float(i + 1)},
                )
                for i, bid in enumerate(rows_v2)
            )
        if v1 is not None:
            stmt_v1 = (
                select(MemoryBlob.id)
                .where(
                    MemoryBlob.org_id == ctx.org_id,
                    ctx.project_pred,
                    MemoryBlob.embedding.is_not(None),
                    *ctx.tag_clauses,
                )
                .order_by(MemoryBlob.embedding.max_inner_product(v1.vector))
                .limit(self.oversample)
            )
            rows_v1 = (await ctx.session.execute(stmt_v1)).scalars().all()
            new.extend(
                Candidate(
                    blob_id=bid,
                    scores_by_stage={self.name: float(i + 1)},
                )
                for i, bid in enumerate(rows_v1)
            )
        return merge_candidates(candidates, new)
