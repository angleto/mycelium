"""Retrieval-trace stage (Fase 0 of the search-informed graph, task
561c6aca). Side-effect only -- candidates pass through unchanged, like
``AccessCounterStage`` -- mounted LAST so it sees exactly the hits the
caller gets. One append-only INSERT per search (ids + ranks, no reads,
no content): the raw signal the offline Phase-2 aggregation
(``refresh_edge_usage``) turns into ``note_edge_usage`` pair counters.

Not mounted at all for probe traffic (the eval harness) so measurement
runs never forge search demand, and gated by
``settings.retrieval_trace_enabled`` -- see the pipeline assembly in
``memory.retrieve_with_meta``.
"""

from __future__ import annotations

from dataclasses import dataclass

from mycelium_core.models.retrieval_trace import RetrievalTrace
from mycelium_core.services.retrieval.types import Candidate, RetrievalContext, Stage


@dataclass
class RetrievalTraceStage(Stage):
    name: str = "retrieval_trace"
    # Safety cap on the traced prefix; the pipeline's LimitStage already
    # bounds candidates to the caller's k, this only guards a future
    # re-ordering of the stage list.
    top_m: int = 16

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        if not candidates:
            return candidates
        ctx.session.add(
            RetrievalTrace(
                org_id=ctx.org_id,
                items=[
                    {"blob_id": str(c.blob_id), "rank": i + 1}
                    for i, c in enumerate(candidates[: self.top_m])
                ],
            )
        )
        await ctx.session.flush()
        return candidates
