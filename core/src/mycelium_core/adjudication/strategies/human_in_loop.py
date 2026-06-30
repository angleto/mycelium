"""HumanInLoop: write an ``escalation`` step and return an outcome
with ``escalated=True``.

The strategy does NOT block the calling coroutine waiting for the
human. M1 design choice: the human resolves the adjudication
asynchronously via ``services.adjudication.resolve_escalation`` (P4),
the same way ``HandoffStatus`` notifications are resolved today. A
caller that wants synchronous blocking can wrap this in a ``Race``
with a polling read.

This strategy is the canonical escalation tail of a ``FallbackChain``
and the canonical implementation of an approval gate; it must not
invent a parallel approval primitive (ADR-0027 §D6).
"""

from __future__ import annotations

from mycelium_core.adjudication.base import (
    AdjudicationContext,
    AdjudicationOutcome,
    CostModel,
    DissentNote,
    StepStore,
    StrategyRequirements,
)
from mycelium_core.models.adjudication import AdjudicationStepKind


class HumanInLoopStrategy:
    id = "human_in_loop"
    requires = StrategyRequirements(needs_human=True)
    composable_with: frozenset[str] = frozenset()
    mutually_exclusive_with: frozenset[str] = frozenset()
    cost_model = CostModel()

    def applicable(self, ctx: AdjudicationContext) -> float:
        # Applicable everywhere as a fallback; the router prefers
        # cheaper strategies via the cost-tie-breaker.
        return 0.2

    async def run(self, ctx: AdjudicationContext, store: StepStore) -> AdjudicationOutcome:
        reason = ctx.config.get("reason", "explicit_escalation")
        prompt = ctx.config.get(
            "human_prompt",
            "Adjudication escalated to a human reviewer. Resolve via the adjudication service.",
        )
        await store.append(
            kind=AdjudicationStepKind.escalation,
            payload={"reason": reason, "prompt": prompt},
            agent_id="human_in_loop",
        )
        # Surface a residual dissent so the outcome shape (ADR-0027
        # §D7) carries the escalation as data, not only as the flag.
        dissent = (
            DissentNote(
                agent_id="human_in_loop",
                position="awaiting_human",
                rationale=reason,
            ),
        )
        return AdjudicationOutcome(
            decision={},
            confidence=0.0,
            residual_dissent=dissent,
            escalated=True,
            meta={"escalation_reason": reason},
        )
