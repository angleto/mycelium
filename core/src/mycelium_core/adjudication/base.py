"""Protocol, DTOs and capability metadata for the adjudication framework.

See docs/adr/0027 §D1, §D2, §D7. Strategies (single-shot, debate,
quorum, contract-net, human-in-loop, judge, composition primitives)
implement ``AdjudicationStrategy`` and are discovered via the registry.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from mycelium_core.models.adjudication import AdjudicationStepKind


@dataclass(frozen=True)
class StrategyRequirements:
    """Capabilities the strategy needs to run.

    The router uses this both to filter (a strategy that needs an
    embedder is excluded when none is configured) and to surface a
    reason in telemetry.
    """

    # 1 = single agent (e.g. single-shot), >=2 = deliberation. The
    # router rejects strategies whose minimum exceeds the available
    # agent pool.
    min_agents: int = 1
    max_agents: int | None = None
    needs_embedder: bool = False
    # ``human_in_loop`` strategies set this to True so the router
    # blocks them when no human gateway is wired (CI, autonomous
    # mode).
    needs_human: bool = False
    # The strategy honours a budget cap. ``Cap`` wraps it; strategies
    # that ignore the cap silently set this to False so the cost
    # check happens outside.
    budget_aware: bool = False


@dataclass(frozen=True)
class CostModel:
    """Conservative ex-ante token estimate.

    The router uses ``estimate`` to compare strategies and to reject
    invocations that would exceed the per-adjudication cap before
    running anything.
    """

    fixed_tokens: int = 0
    tokens_per_agent: int = 0
    tokens_per_round: int = 0

    def estimate(self, *, n_agents: int = 1, n_rounds: int = 1) -> int:
        return (
            self.fixed_tokens
            + self.tokens_per_agent * max(0, n_agents)
            + self.tokens_per_round * max(0, n_rounds)
        )


@dataclass(frozen=True)
class DissentNote:
    """One residual point of disagreement after the strategy resolves.

    ADR-0027 §D7: confidence + residual_dissent are not optional. A
    synthesis that suppresses disagreement is treated as a bug.
    """

    agent_id: str
    position: str
    rationale: str


@dataclass(frozen=True)
class AdjudicationOutcome:
    """The result a strategy returns from ``run``.

    A composition (``FallbackChain``, ``Cap``) inspects ``escalated`` /
    ``aborted_reason`` to decide whether to advance or stop.
    """

    decision: dict[str, Any]
    confidence: float
    residual_dissent: tuple[DissentNote, ...] = ()
    escalated: bool = False
    aborted_reason: str | None = None
    # Free-form metadata: strategy id chain used (composition), round
    # counts, convergence signals, etc. Not user-facing.
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdjudicationContext:
    """Read-only context passed to a strategy.

    The strategy must not mutate ``context`` or ``config``; both are
    snapshot inputs. New data goes into the ``StepStore``.
    """

    org_id: uuid.UUID
    actor_id: uuid.UUID
    adjudication_id: uuid.UUID
    question_text: str
    context: dict[str, Any]
    config: dict[str, Any]
    task_id: uuid.UUID | None = None
    # The number of agents available to the strategy at run time, as
    # known by the caller. ``None`` means "the strategy decides"; some
    # strategies (single-shot) ignore the field entirely.
    n_agents_available: int | None = None


@dataclass(frozen=True)
class StepRecord:
    """A snapshot row from the step log; what the StepStore returns.

    Mirrors ``AdjudicationStep`` model fields but does not depend on
    SQLAlchemy session lifetime: strategies and tests can pass it
    around freely.
    """

    step_no: int
    kind: AdjudicationStepKind
    payload: dict[str, Any]
    agent_id: str | None
    embedding: tuple[float, ...] | None


@runtime_checkable
class StepStore(Protocol):
    """Append + read interface for one adjudication's step log.

    Strategies write events here as a side effect of ``run``; the
    service layer instantiates the concrete store (DB-backed in
    production, in-memory in unit tests and inside composition
    primitives that virtualise nested step counters).
    """

    async def append(
        self,
        *,
        kind: AdjudicationStepKind,
        payload: dict[str, Any],
        agent_id: str | None = None,
        embedding: Sequence[float] | None = None,
    ) -> int: ...

    async def list_steps(self) -> list[StepRecord]: ...


@runtime_checkable
class AdjudicationStrategy(Protocol):
    """The single-method seam strategies implement.

    ADR-0027 §D2: this Protocol is deliberately minimal (two methods +
    metadata). Capability needs go into ``requires``; cost projections
    into ``cost_model``; composability into ``composable_with`` /
    ``mutually_exclusive_with``. Anything else belongs to the strategy
    body, not to the seam.

    The metadata fields are declared as instance attributes (not
    ``ClassVar``) so a fake/test double can build an instance with a
    dynamic ``id`` and still satisfy the Protocol. In-tree strategies
    typically declare them ``ClassVar``: ``ClassVar[T]`` is a strict
    subtype of ``T`` from the Protocol matcher's perspective.
    """

    id: str
    requires: StrategyRequirements
    composable_with: frozenset[str]
    mutually_exclusive_with: frozenset[str]
    cost_model: CostModel

    def applicable(self, ctx: AdjudicationContext) -> float:
        """Soft score in [0, 1]. 0 means not runnable (missing
        capability). The router ranks strategies by this score before
        ties broken by ``cost_model.estimate``."""
        ...

    async def run(self, ctx: AdjudicationContext, store: StepStore) -> AdjudicationOutcome: ...
