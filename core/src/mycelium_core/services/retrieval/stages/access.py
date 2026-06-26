"""Access-counter stage: bump ``access_count`` / ``access_score`` /
``last_accessed_at`` on the rows the pipeline is about to return.
Side-effect only -- candidates pass through unchanged. Used by the
tier-recompute decay (memory.recompute_tier) to keep hot/warm/cold
honest.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import update

from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.services.retrieval.types import Candidate, RetrievalContext, Stage


@dataclass
class AccessCounterStage(Stage):
    name: str = "access_counter"

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        if not candidates:
            return candidates
        now = dt.datetime.now(tz=dt.UTC)
        await ctx.session.execute(
            update(MemoryBlob)
            .where(MemoryBlob.id.in_([c.blob_id for c in candidates]))
            .values(
                access_count=MemoryBlob.access_count + 1,
                last_accessed_at=now,
                access_score=MemoryBlob.access_score + 1,
            )
        )
        await ctx.session.flush()
        return candidates
