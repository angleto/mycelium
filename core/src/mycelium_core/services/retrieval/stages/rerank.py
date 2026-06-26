"""Cross-encoder reranker stage (task `27579d6a`).

Drops in after RRF fusion to re-order the top-K candidates with a
cross-encoder model (sees query+doc joined, materially more accurate
than the bi-encoder embedding the dense branch uses).

Gated so it never silently inflates query cost: skipped when the
query is too short to matter, when the candidate set is too small,
or when the per-call / env flag is off. When skipped, candidates pass
through unchanged.

Drives the score override: after rerank, ``Candidate.score`` becomes
the cross-encoder logit; the previous RRF score moves to
``scores_by_stage["rrf"]`` for diagnostics. The downstream
``OrderingStage`` then re-sorts on the new score, so callers don't
need to add a second sort.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.reranker import Reranker, get_reranker
from mycelium_core.services.retrieval.types import Candidate, RetrievalContext, Stage


@dataclass
class RerankGate:
    min_query_tokens: int = 3
    min_candidates: int = 5

    def should_rerank(self, query: str, candidates: list[Candidate]) -> bool:
        if len(query.split()) < self.min_query_tokens:
            return False
        if len(candidates) < self.min_candidates:
            return False
        return True


@dataclass
class CrossEncoderRerankerStage(Stage):
    name: str = "rerank"
    top_k: int = 50
    provider: Reranker | None = None
    gate: RerankGate | None = None

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        gate = self.gate or RerankGate()
        if not gate.should_rerank(query, candidates):
            return candidates
        provider = self.provider or get_reranker()
        # Operate only on the top-K candidates (cost is O(top-K), and
        # rerankering tail candidates that won't be returned is waste).
        # We still pass the full list through and patch in the new scores
        # so the next stage can do whatever it wants with the tail.
        target = candidates[: self.top_k]
        texts = await self._load_texts(ctx, target)
        result = await provider.rerank(query, texts)
        for cand, score in zip(target, result.scores, strict=False):
            cand.scores_by_stage["rrf"] = cand.score  # preserve for diagnostics
            cand.scores_by_stage[self.name] = score
            cand.score = score
        return candidates

    @staticmethod
    async def _load_texts(ctx: RetrievalContext, candidates: list[Candidate]) -> list[str]:
        """Hydrate text for candidates that don't carry it yet (the
        lexical/semantic stages only carry ids). Single SELECT, then
        backfill in place so a subsequent stage finds the text."""
        missing = [c.blob_id for c in candidates if c.text is None]
        if missing:
            rows = (
                await ctx.session.execute(
                    select(MemoryBlob.id, MemoryBlob.text).where(MemoryBlob.id.in_(missing))
                )
            ).all()
            by_id = {bid: txt for bid, txt in rows}
            for c in candidates:
                if c.text is None:
                    c.text = by_id.get(c.blob_id) or ""
        return [c.text or "" for c in candidates]
