"""Cross-encoder reranker stage (task `27579d6a`).

Drops in after RRF fusion to re-order the leading candidates with a
cross-encoder model (sees query+doc joined, materially more accurate
than the bi-encoder embedding the dense branch uses).

How much of the list it re-orders is decided by a TIME BUDGET, not by a
count of pairs. It scores the candidates in RRF order, batch after batch,
for as long as the provider's own estimate of the next batch fits in what
is left of the budget; what it reached is re-ordered by the cross-encoder
and the rest keeps its RRF order below it. A count used to decide this
(``reranker_top_k=50``), and a count is only right on the machine, the model
and the document lengths it was chosen on: on the production node the
default of 50 pairs took 121 s (task 03cdf674). A budget is stated in the
unit that does not move when the corpus grows or the model changes, which is
how long the caller can wait.

Every request reports what happened in ``ctx.extras["rerank"]`` (a
``RerankOutcome``), including when nothing happened. Until 2026-10-22 a
request made with the feature off passed the RRF order through unchanged and
said nothing, so a caller asking for a rerank could not tell it had not
got one.

Drives the score override: a reranked candidate's ``score`` becomes the
cross-encoder's [0,1] relevance and the previous RRF score moves to
``scores_by_stage["rrf"]``. The downstream ``OrderingStage`` re-sorts on
the new score. A reranked candidate the cross-encoder judges irrelevant can
therefore fall below the RRF tail (RRF sums sit around 0.016), which is the
intended reading of "the model says no", not a mix-up of scales.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select

from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.reranker import Reranker, RerankerUnavailable, get_reranker
from mycelium_core.services.retrieval.types import Candidate, RetrievalContext, Stage

# Re-ordering a single candidate changes nothing, so a budget that cannot
# cover at least this many is a budget that cannot buy a rerank at all.
_MIN_USEFUL_PAIRS = 2


class RerankOutcome(enum.StrEnum):
    """What a requested rerank did. Closed: a value outside it is a bug, and
    the caller is meant to branch on it."""

    RAN = "ran"
    # The query or the candidate set was too small for a rerank to matter.
    SKIPPED_GATE = "skipped_gate"
    # The budget could not cover the smallest useful batch.
    SKIPPED_BUDGET = "skipped_budget"
    # The provider cannot score here: feature off, or model not loaded.
    UNAVAILABLE = "unavailable"
    # The provider should have scored and raised instead.
    FAILED = "failed"


# Most degraded first: when one search runs several retrievals, the merged
# report names the worst thing that happened to any of them, so "ran" is
# only claimed when every branch that asked for a rerank got one.
_SEVERITY: tuple[RerankOutcome, ...] = (
    RerankOutcome.FAILED,
    RerankOutcome.UNAVAILABLE,
    RerankOutcome.SKIPPED_BUDGET,
    RerankOutcome.SKIPPED_GATE,
    RerankOutcome.RAN,
)


def combine_outcomes(outcomes: Iterable[RerankOutcome | None]) -> RerankOutcome | None:
    """Merge the outcomes of several retrievals into one report. ``None``
    (no rerank requested) is ignored; all ``None`` stays ``None``."""
    present = {o for o in outcomes if o is not None}
    for outcome in _SEVERITY:
        if outcome in present:
            return outcome
    return None


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
    # Seconds the stage may spend scoring, queue wait included.
    budget_s: float = 1.0
    # Depth cap for REPRODUCIBLE MEASUREMENT only (the bench pins it so that a
    # run scores the same pairs on any machine). None in production: there the
    # budget decides, and a fixed depth is exactly what this stage replaced.
    max_pairs: int | None = None
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
            ctx.extras["rerank"] = RerankOutcome.SKIPPED_GATE
            return candidates
        provider = self.provider or get_reranker()
        target = candidates if self.max_pairs is None else candidates[: self.max_pairs]
        # Readiness first, on an empty batch: an unavailable provider must not
        # cost a SELECT of every candidate's text.
        if provider.estimate_seconds(query, []) is None:
            ctx.extras["rerank"] = RerankOutcome.UNAVAILABLE
            return candidates
        texts = await self._load_texts(ctx, target)
        # The budget is the cross-encoder's, so the clock starts after the
        # texts are in hand: a slow SELECT is the database's latency, and
        # charging it here makes even a zero-cost provider report "over
        # budget" whenever the database is busy. Pinned by
        # test_a_slow_text_load_is_not_charged_to_the_budget.
        deadline = time.monotonic() + self.budget_s

        done = 0
        while done < len(target):
            remaining = deadline - time.monotonic()
            size = self._batch_that_fits(provider, query, texts[done:], remaining)
            if size is None:
                # Lost mid-way: what was scored stays applied, and a reranked
                # prefix is reported as a rerank exactly like one the budget
                # cut short, because that is what the caller received.
                ctx.extras["rerank"] = RerankOutcome.UNAVAILABLE if done == 0 else RerankOutcome.RAN
                return candidates
            if done == 0 and size < _MIN_USEFUL_PAIRS:
                ctx.extras["rerank"] = RerankOutcome.SKIPPED_BUDGET
                return candidates
            if size == 0:
                break
            batch = target[done : done + size]
            try:
                result = await provider.rerank(query, texts[done : done + size])
            except RerankerUnavailable:
                ctx.extras["rerank"] = RerankOutcome.UNAVAILABLE if done == 0 else RerankOutcome.RAN
                return candidates
            except Exception:
                # The cross-encoder depends on an optional extra / model that
                # can fail at predict time (mirrors memory._safe_embed):
                # degrade to the RRF order rather than erroring the whole
                # search, and say so. A batch already applied stays applied.
                ctx.extras["rerank"] = RerankOutcome.FAILED
                return candidates
            for cand, score in zip(batch, result.scores, strict=False):
                cand.scores_by_stage["rrf"] = cand.score  # preserve for diagnostics
                cand.scores_by_stage[self.name] = score
                cand.score = score
            done += size
        ctx.extras["rerank"] = RerankOutcome.RAN
        return candidates

    @staticmethod
    def _batch_that_fits(
        provider: Reranker, query: str, texts: list[str], remaining: float
    ) -> int | None:
        """The longest leading slice of ``texts`` whose estimated cost fits in
        ``remaining`` seconds, or None if the provider stopped being able to
        estimate. Linear in the candidates, which is nothing next to a forward
        pass."""
        size = 0
        for n in range(1, len(texts) + 1):
            estimate = provider.estimate_seconds(query, texts[:n])
            if estimate is None:
                return None
            if estimate > remaining:
                break
            size = n
        return size

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
