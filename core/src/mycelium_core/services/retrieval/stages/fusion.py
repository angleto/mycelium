"""RRF fusion stage: sums ``weight/(k+rank)`` across every
``scores_by_stage`` entry so far. Reciprocal-rank-fusion is robust to
score-scale differences across stages (semantic distances vs lexical
ts_rank vs reranker logits) -- no per-stage normalization needed.

``weights`` lets a branch outrank another regardless of rank: the
lexical branch is weighted ABOVE the semantic one so an exact term match
always beats a semantic-only neighbour. This matters because the local
embedder (bge-m3) packs even unrelated same-language text into a high,
compressed cosine band, so a pure-semantic 'match' for a keyword/name
query is often noise that, under equal-weight rank-only RRF, ties with
(or outranks) the real lexical hit. Default: every stage weight 1.0 (the
original behaviour). The aggregate lands in ``Candidate.score``."""

from __future__ import annotations

from dataclasses import dataclass, field

from mycelium_core.services.retrieval.types import Candidate, RetrievalContext, Stage


@dataclass
class RRFFusionStage(Stage):
    name: str = "rrf"
    k: int = 60
    # Per-stage multipliers keyed by the ``scores_by_stage`` name
    # (e.g. 'lexical', 'semantic', 'semantic_hosted', 'humus'). Missing
    # -> 1.0. The humus source (ADR-0034) is weighted on the low
    # precision tier (a small boost, not the exact tier) so it nudges
    # archived atoms up without overriding an exact lexical match.
    weights: dict[str, float] = field(default_factory=dict)

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        for c in candidates:
            fused = 0.0
            for stage_name, rank in c.scores_by_stage.items():
                weight = self.weights.get(stage_name, 1.0)
                fused += weight / (self.k + rank)
            c.score = fused
        return candidates
