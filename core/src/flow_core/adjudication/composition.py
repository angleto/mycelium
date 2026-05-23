"""Composition primitives as meta-strategies (ADR-0027 §D4).

``FallbackChain``, ``Cap``, ``Filter`` and ``Race`` themselves implement
``AdjudicationStrategy``, so the registry/router/store are unaware of
nesting. Composability checks (``composable_with`` /
``mutually_exclusive_with``) are enforced when a composition is
instantiated, not at runtime: a violation is a programming error.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from flow_core.adjudication.base import (
    AdjudicationContext,
    AdjudicationOutcome,
    AdjudicationStrategy,
    CostModel,
    StepStore,
    StrategyRequirements,
)
from flow_core.adjudication.registry import get_registry
from flow_core.models.adjudication import AdjudicationStepKind


class FallbackChain:
    """Try children in order; advance on ``escalated`` or
    ``aborted_reason``; stop on the first non-escalated, non-aborted
    outcome.

    Use case: ``[CoherenceVote(...), Debate(...), HumanInLoop()]`` ==
    cheap try, deliberate try, escalate to human as last resort.
    """

    id: str = "fallback_chain"
    requires: StrategyRequirements = StrategyRequirements()
    composable_with: frozenset[str] = frozenset()
    mutually_exclusive_with: frozenset[str] = frozenset()
    cost_model: CostModel = CostModel()

    def __init__(self, children: list[AdjudicationStrategy]) -> None:
        if not children:
            raise ValueError("FallbackChain requires at least one child strategy")
        get_registry().validate_composition(outer=self, children=children)
        self._children = list(children)

    def applicable(self, ctx: AdjudicationContext) -> float:
        scores = [c.applicable(ctx) for c in self._children]
        return max(scores) if scores else 0.0

    async def run(self, ctx: AdjudicationContext, store: StepStore) -> AdjudicationOutcome:
        # ``__init__`` enforces at least one child, so the first
        # iteration always runs.
        last = await self._children[0].run(ctx, store)
        chain = [self._children[0].id]
        for child in self._children[1:]:
            if not last.escalated and last.aborted_reason is None:
                break
            chain.append(child.id)
            last = await child.run(ctx, store)
        return AdjudicationOutcome(
            decision=last.decision,
            confidence=last.confidence,
            residual_dissent=last.residual_dissent,
            escalated=last.escalated,
            aborted_reason=last.aborted_reason,
            meta={**last.meta, "fallback_chain": chain},
        )


class Cap:
    """Hard ceiling on wall time and tokens. On exceed: returns an
    ``aborted`` outcome (``aborted_reason='cap_exceeded'``) without
    further side effects. The inner strategy may still have written
    steps before the cap fired; that history is preserved by design.

    Token accounting is best-effort: this layer only measures wall
    time. Token caps must be enforced by the inner strategy through
    the ``budget_aware`` requirement (the strategy reads its own
    ``billing.meter_if_billable`` ledger and stops). The wall-time
    cap is the unconditional kill switch.
    """

    id: str = "cap"
    requires: StrategyRequirements = StrategyRequirements()
    composable_with: frozenset[str] = frozenset()
    mutually_exclusive_with: frozenset[str] = frozenset()
    cost_model: CostModel = CostModel()

    def __init__(
        self,
        inner: AdjudicationStrategy,
        *,
        wall_s_max: float,
        tokens_max: int | None = None,
    ) -> None:
        if wall_s_max <= 0:
            raise ValueError("Cap requires wall_s_max > 0")
        self._inner = inner
        self._wall_s_max = wall_s_max
        self._tokens_max = tokens_max

    def applicable(self, ctx: AdjudicationContext) -> float:
        return self._inner.applicable(ctx)

    async def run(self, ctx: AdjudicationContext, store: StepStore) -> AdjudicationOutcome:
        start = time.monotonic()
        try:
            outcome = await asyncio.wait_for(self._inner.run(ctx, store), timeout=self._wall_s_max)
        except TimeoutError:
            await store.append(
                kind=AdjudicationStepKind.escalation,
                payload={
                    "reason": "cap_exceeded",
                    "limit_wall_s": self._wall_s_max,
                    "inner_strategy": self._inner.id,
                },
            )
            return AdjudicationOutcome(
                decision={},
                confidence=0.0,
                aborted_reason="cap_exceeded",
                meta={
                    "cap_wall_s": self._wall_s_max,
                    "cap_tokens": self._tokens_max,
                    "elapsed_s": time.monotonic() - start,
                    "inner_strategy": self._inner.id,
                },
            )
        return AdjudicationOutcome(
            decision=outcome.decision,
            confidence=outcome.confidence,
            residual_dissent=outcome.residual_dissent,
            escalated=outcome.escalated,
            aborted_reason=outcome.aborted_reason,
            meta={
                **outcome.meta,
                "cap_wall_s": self._wall_s_max,
                "cap_tokens": self._tokens_max,
                "elapsed_s": time.monotonic() - start,
            },
        )


class Filter:
    """Apply ``inner`` only if ``when(ctx)`` returns True; otherwise
    return a no-op outcome (or call ``fallback`` if provided).

    Use case: only debate when stakes are high enough to pay for it.
    """

    id: str = "filter"
    requires: StrategyRequirements = StrategyRequirements()
    composable_with: frozenset[str] = frozenset()
    mutually_exclusive_with: frozenset[str] = frozenset()
    cost_model: CostModel = CostModel()

    def __init__(
        self,
        inner: AdjudicationStrategy,
        *,
        when: Callable[[AdjudicationContext], bool],
        fallback: AdjudicationStrategy | None = None,
    ) -> None:
        self._inner = inner
        self._when = when
        self._fallback = fallback

    def applicable(self, ctx: AdjudicationContext) -> float:
        if not self._when(ctx):
            return self._fallback.applicable(ctx) if self._fallback else 0.0
        return self._inner.applicable(ctx)

    async def run(self, ctx: AdjudicationContext, store: StepStore) -> AdjudicationOutcome:
        if self._when(ctx):
            return await self._inner.run(ctx, store)
        if self._fallback is not None:
            return await self._fallback.run(ctx, store)
        # No-op outcome: confidence 0, decision empty. Callers treat
        # this as "filter did not engage, no decision produced".
        return AdjudicationOutcome(
            decision={},
            confidence=0.0,
            meta={"filter_engaged": False, "inner_strategy": self._inner.id},
        )


class Race:
    """Run children concurrently; resolve on the first non-escalated
    outcome.

    Use case: ``[Debate(...), HumanInLoop()]`` with ``stop_on_first=True``
    means "if the human resolves before the debate converges, take
    the human's call; otherwise the debate's call".

    All losing tasks are cancelled. Steps already written by losing
    tasks are NOT rolled back: the timeline is honest about what ran.
    """

    id: str = "race"
    requires: StrategyRequirements = StrategyRequirements()
    composable_with: frozenset[str] = frozenset()
    mutually_exclusive_with: frozenset[str] = frozenset()
    cost_model: CostModel = CostModel()

    def __init__(
        self,
        children: list[AdjudicationStrategy],
        *,
        stop_on_first: bool = True,
    ) -> None:
        if len(children) < 2:
            raise ValueError("Race requires at least two child strategies")
        get_registry().validate_composition(outer=self, children=children)
        self._children = list(children)
        self._stop_on_first = stop_on_first

    def applicable(self, ctx: AdjudicationContext) -> float:
        scores = [c.applicable(ctx) for c in self._children]
        return max(scores) if scores else 0.0

    async def run(self, ctx: AdjudicationContext, store: StepStore) -> AdjudicationOutcome:
        tasks = [asyncio.create_task(c.run(ctx, store)) for c in self._children]
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            winner = next(iter(done))
            outcome = winner.result()
            if self._stop_on_first or (not outcome.escalated and outcome.aborted_reason is None):
                for p in pending:
                    p.cancel()
                # Drain cancellations so they do not leak as warnings.
                # Swallowing is intentional: the winner already produced
                # the outcome; losers' exceptions are noise.
                for p in pending:
                    try:
                        await p
                    except (asyncio.CancelledError, Exception):  # noqa: S110
                        pass
                return AdjudicationOutcome(
                    decision=outcome.decision,
                    confidence=outcome.confidence,
                    residual_dissent=outcome.residual_dissent,
                    escalated=outcome.escalated,
                    aborted_reason=outcome.aborted_reason,
                    meta={**outcome.meta, "race_winner": True},
                )
            # First done was escalated and stop_on_first is False:
            # wait for the rest, return the first non-escalated.
            for fut in asyncio.as_completed(list(pending)):
                outcome = await fut
                if not outcome.escalated and outcome.aborted_reason is None:
                    return AdjudicationOutcome(
                        decision=outcome.decision,
                        confidence=outcome.confidence,
                        residual_dissent=outcome.residual_dissent,
                        escalated=outcome.escalated,
                        aborted_reason=outcome.aborted_reason,
                        meta={**outcome.meta, "race_winner": True},
                    )
            return outcome
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
