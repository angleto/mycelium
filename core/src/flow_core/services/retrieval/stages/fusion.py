"""RRF fusion stage: sums 1/(k+rank) across every ``scores_by_stage``
entry so far. Reciprocal-rank-fusion is robust to score-scale
differences across stages (semantic distances vs lexical ts_rank vs
reranker logits) -- no per-stage normalization needed. The aggregate
lands in ``Candidate.score`` for the next stage to sort/filter on."""

from __future__ import annotations

from dataclasses import dataclass

from flow_core.services.retrieval.types import Candidate, RetrievalContext, Stage


@dataclass
class RRFFusionStage(Stage):
    name: str = "rrf"
    k: int = 60

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        for c in candidates:
            fused = 0.0
            for rank in c.scores_by_stage.values():
                fused += 1.0 / (self.k + rank)
            c.score = fused
        return candidates
