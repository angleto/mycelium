"""Executor of a list of ``Stage``. Trivial today (linear loop), kept
isolated so future concurrency primitives (``ParallelStages``) and
diagnostics (per-stage timing, candidate count delta) can slot in
without touching callers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from flow_core.services.retrieval.types import Candidate, RetrievalContext, Stage


@dataclass
class RetrievalPipeline:
    stages: Sequence[Stage]

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for stage in self.stages:
            candidates = await stage.run(query, ctx, candidates)
        return candidates
