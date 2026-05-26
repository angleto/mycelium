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

from flow_core.models.memory_blob import MemoryBlob
from flow_core.services.retrieval.types import Candidate, RetrievalContext, Stage


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
                    select(MemoryBlob.id, MemoryBlob.created_at).where(
                        MemoryBlob.id.in_(missing)
                    )
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
        if candidates[0].score < self.min_score:
            return []
        return candidates


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
