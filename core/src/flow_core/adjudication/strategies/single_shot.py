"""SingleShot: ask one LLM, record one ``turn``, return its answer.

Baseline strategy. Confidence is fixed at 0.5 because a single-shot
answer carries no convergence signal: any higher value would be a
lie. Useful as a comparison anchor in telemetry and as a fallback
inside a ``FallbackChain``.

The LLM seam is the standard ``flow_core.ai_providers.LLMProvider``,
honoured through ``get_llm()``; tests inject a fake via
``set_llm_override``.
"""

from __future__ import annotations

from flow_core.adjudication.base import (
    AdjudicationContext,
    AdjudicationOutcome,
    CostModel,
    StepStore,
    StrategyRequirements,
)
from flow_core.ai_providers import get_llm
from flow_core.models.adjudication import AdjudicationStepKind

_SYSTEM_PROMPT = (
    "You are answering a single decision question. Provide a concise, "
    "actionable answer. No preamble. No options unless the question "
    "asks for alternatives."
)


class SingleShotStrategy:
    # Plain class attributes (not ``ClassVar``) so the Protocol match
    # succeeds against both fakes (instance var) and in-tree strategies
    # (class var). See ``adjudication.base.AdjudicationStrategy``.
    id = "single_shot"
    requires = StrategyRequirements(min_agents=1, max_agents=1)
    composable_with: frozenset[str] = frozenset()
    mutually_exclusive_with: frozenset[str] = frozenset()
    cost_model = CostModel(
        fixed_tokens=400,
        tokens_per_agent=0,
        tokens_per_round=0,
    )

    def applicable(self, ctx: AdjudicationContext) -> float:
        # Always applicable. Lowest non-zero applicability so the
        # router only picks it when nothing else fits.
        return 0.1

    async def run(self, ctx: AdjudicationContext, store: StepStore) -> AdjudicationOutcome:
        llm = get_llm()
        result = await llm.complete(
            system=_SYSTEM_PROMPT,
            messages=[("user", ctx.question_text)],
        )
        await store.append(
            kind=AdjudicationStepKind.turn,
            payload={
                "text": result.text,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "model_id": result.model_id,
            },
            agent_id="single_shot",
        )
        return AdjudicationOutcome(
            decision={"answer": result.text},
            confidence=0.5,
            meta={
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "model_id": result.model_id,
            },
        )
