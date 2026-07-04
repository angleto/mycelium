"""Order + limit + grader-min stages.

``OrderingStage`` sorts by score DESC with a tie-break on
``created_at`` ASC and then ``str(blob_id)``: deterministic so a
re-execution under identical conditions returns identical order. The
tie-break needs ``created_at`` populated; the stage loads the missing
fields in a single SELECT (so callers don't see N+1).

``GraderMinStage`` early-exits to an empty list when the top score
falls below a configured floor (used by graders that want "no answer"
over "weak answer").

``LimitStage`` truncates to top-K.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import select

from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.services.retrieval.types import Candidate, RetrievalContext, Stage


def _sigmoid(x: float) -> float:
    """Numerically stable logistic sigmoid (overflow-safe both directions)."""
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass
class OrderingStage(Stage):
    name: str = "order"

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        if not candidates:
            return candidates
        # Ensure created_at is loaded for the tie-break.
        missing = [c.blob_id for c in candidates if c.created_at is None]
        if missing:
            rows = (
                await ctx.session.execute(
                    select(MemoryBlob.id, MemoryBlob.created_at).where(MemoryBlob.id.in_(missing))
                )
            ).all()
            by_id = {bid: ts for bid, ts in rows}
            for c in candidates:
                if c.created_at is None:
                    c.created_at = by_id.get(c.blob_id)
        candidates.sort(
            key=lambda c: (
                -c.score,
                c.created_at or _MIN_TS,
                str(c.blob_id),
            )
        )
        return candidates


@dataclass
class GraderMinStage(Stage):
    """Honest abstain: drop the whole result when the top hit is too weak,
    so the caller gets "not in memory" over a confidently-wrong first hit.

    Two floors, checked with a precedence rule (task f0d24fdb / N3):

    * ``min_rerank_prob`` -- the QUALITY floor on the cross-encoder logit
      (squashed to a [0,1] probability). This is the honest-abstain signal:
      the logit scores query-doc relevance directly. It is only meaningful
      when the reranker actually ran (it writes ``scores_by_stage["rerank"]``).
    * ``min_score`` -- the coarse RRF floor on the FUSED score (WS-B1),
      rank-based and measured near-useless for abstention (note 3276b266 §5),
      kept for continuity.

    PRECEDENCE: when the reranker ran AND a rerank floor is set, the rerank
    floor is the SOLE authority and the RRF floor is skipped -- after
    reranking ``candidates[0]`` is the top by logit, not by RRF, so a
    high-quality hit that reranked up from a low RRF rank would be spuriously
    cut by the RRF floor. With no rerank signal (or no rerank floor) the RRF
    floor applies exactly as before, so a caller that leaves ``min_rerank_prob``
    None is byte-identical to the historical behaviour."""

    name: str = "grader_min"
    min_score: float | None = None
    min_rerank_prob: float | None = None

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        if not candidates:
            return candidates
        top = candidates[0]
        reranked = "rerank" in top.scores_by_stage
        # Quality gate takes precedence when the reranker ran (see class doc):
        # grade on the logit of the item the cross-encoder ranked #1.
        if self.min_rerank_prob is not None and reranked:
            if _sigmoid(top.scores_by_stage["rerank"]) < self.min_rerank_prob:
                ctx.extras["grader_abstained"] = True
                ctx.extras["grader_abstain_reason"] = "grader_min_rerank_logit"
                return []
            return candidates
        if self.min_score is None:
            return candidates
        # Coarse RRF floor on the FUSED score, not the current ``score``: the
        # optional reranker overwrites ``score`` but preserves the fused score
        # under "rrf", so the threshold stays calibrated. Reached only when
        # there is no rerank quality signal to defer to.
        fused = top.scores_by_stage.get("rrf", top.score)
        if fused < self.min_score:
            # Record the abstain so RetrievalMeta can tell a deliberate
            # "no answer above the floor" from a genuinely empty index
            # (the empty result is otherwise byte-identical). WS-B1.
            ctx.extras["grader_abstained"] = True
            ctx.extras["grader_abstain_reason"] = "grader_min_rrf"
            return []
        return candidates


@dataclass
class RelativeFloorStage(Stage):
    """Drop candidates whose fused score falls far below the top hit.

    A keyword/name query produces a wide score gap: the lexical hits sit
    near the top while pure-semantic noise (weighted down in fusion)
    trails far behind -- those are cut. A conceptual query produces a
    FLAT score profile (all semantic, similar ranks), so nothing is more
    than ``ratio`` below the top and the cut is a no-op: recall for
    genuinely-semantic queries is preserved. ``ratio`` 0 disables it.

    Runs after OrderingStage (candidates already score-DESC) so the top
    is ``candidates[0]``."""

    name: str = "relative_floor"
    ratio: float = 0.0

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        if self.ratio <= 0.0 or not candidates:
            return candidates
        top = max(c.score for c in candidates)
        if top <= 0.0:
            return candidates
        floor = self.ratio * top
        return [c for c in candidates if c.score >= floor]


@dataclass
class LimitStage(Stage):
    name: str = "limit"
    k: int = 10

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        return candidates[: self.k]


# Module-level sentinel for sort key when created_at is missing
# (shouldn't happen post-OrderingStage but defensive).
import datetime as _dt  # noqa: E402

_MIN_TS = _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)
