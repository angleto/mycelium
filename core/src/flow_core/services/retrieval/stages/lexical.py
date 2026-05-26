"""Lexical FTS stage: dual-dictionary tsvector OR (``simple`` +
``italian``) on ``memory_blobs.fts`` / ``fts_lang`` (migration 0007).
Adds candidates with a per-stage rank = position in the ts_rank-sorted
oversample list (1-based, like the original RRF input)."""

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
class LexicalFTSStage(Stage):
    name: str = "lexical"
    oversample: int = 50

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        stmt = (
            select(MemoryBlob.id)
            .where(
                MemoryBlob.org_id == ctx.org_id,
                ctx.project_pred,
                text(
                    "(fts @@ plainto_tsquery('simple', :q)"
                    " OR fts_lang @@ plainto_tsquery('italian', :q))"
                ),
                *ctx.tag_clauses,
            )
            .order_by(
                text(
                    "GREATEST("
                    "ts_rank(fts, plainto_tsquery('simple', :q)),"
                    "ts_rank(fts_lang, plainto_tsquery('italian', :q))"
                    ") DESC"
                )
            )
            .limit(self.oversample)
        ).params(q=query)
        rows = (await ctx.session.execute(stmt)).scalars().all()
        new = [
            Candidate(
                blob_id=bid,
                scores_by_stage={self.name: float(i + 1)},
            )
            for i, bid in enumerate(rows)
        ]
        return merge_candidates(candidates, new)
