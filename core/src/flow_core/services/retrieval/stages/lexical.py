"""Lexical FTS stage: dual-dictionary tsvector on ``memory_blobs.fts``
(``simple``, exact terms) and ``fts_lang`` (``italian``, stemmed),
migration 0007.

Emitted as TWO separate signals so fusion can weight them apart:

- ``lexical_exact``: blobs that match the query terms VERBATIM (the
  ``simple`` dictionary does no stemming). The strongest signal.
- ``lexical_stem``: blobs that match only after Italian stemming. Useful
  for morphology ('gatti' -> 'gatto') but it also conflates a short
  proper noun with a common word -- e.g. 'marzia' and 'marzo' share the
  stem, so an essay dated 'marzo 2025' spuriously matches a search for
  the name Marzia. Weighted DOWN in fusion (near the semantic tier) so a
  stem-only hit never masquerades as an exact hit; the relative floor
  then drops it when a genuine exact match set a high top score.

Each signal's per-blob value is its 1-based rank in that dictionary's
ts_rank-sorted oversample list."""

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

    async def _ranked(
        self, ctx: RetrievalContext, query: str, *, match: str, rank: str, key: str
    ) -> list[Candidate]:
        stmt = (
            select(MemoryBlob.id)
            .where(
                MemoryBlob.org_id == ctx.org_id,
                ctx.project_pred,
                text(match),
                *ctx.tag_clauses,
            )
            .order_by(text(f"{rank} DESC"))
            .limit(self.oversample)
        ).params(q=query)
        rows = (await ctx.session.execute(stmt)).scalars().all()
        return [
            Candidate(blob_id=bid, scores_by_stage={key: float(i + 1)})
            for i, bid in enumerate(rows)
        ]

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        exact = await self._ranked(
            ctx,
            query,
            match="fts @@ plainto_tsquery('simple', :q)",
            rank="ts_rank(fts, plainto_tsquery('simple', :q))",
            key="lexical_exact",
        )
        stem = await self._ranked(
            ctx,
            query,
            match="fts_lang @@ plainto_tsquery('italian', :q)",
            rank="ts_rank(fts_lang, plainto_tsquery('italian', :q))",
            key="lexical_stem",
        )
        return merge_candidates(candidates, exact + stem)
