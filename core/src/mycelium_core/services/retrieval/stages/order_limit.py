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

from dataclasses import dataclass

from sqlalchemy import select

from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.services.retrieval.types import Candidate, RetrievalContext, Stage


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
    name: str = "grader_min"
    min_score: float | None = None

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        if self.min_score is None or not candidates:
            return candidates
        # Floor on the FUSED RRF score, not the current ``score``: the
        # optional reranker overwrites ``score`` with its own (differently
        # scaled) value but preserves the fused score under "rrf", so the
        # abstain threshold stays calibrated whether or not rerank ran.
        top = candidates[0]
        fused = top.scores_by_stage.get("rrf", top.score)
        if fused < self.min_score:
            # Record the abstain so RetrievalMeta can tell a deliberate
            # "no answer above the floor" from a genuinely empty index
            # (the empty result is otherwise byte-identical). WS-B1.
            ctx.extras["grader_abstained"] = True
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
