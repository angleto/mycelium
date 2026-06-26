"""Adjudication framework (docs/adr/0027) tests.

Covers M1 deliverables for ADR-0027 (ticket figlio 2):

- Registry: register, get, list, rank, validate_composition, clear.
- Composition primitives: FallbackChain (advance on escalation, stop
  on resolved), Cap (timeout -> aborted), Filter (when-gating),
  Race (first non-escalated wins).
- Policy router: explicit override > declarative rules (priority)
  > applicability auto-rank.
- Built-in strategies: SingleShot writes a turn step; HumanInLoop
  writes an escalation step and returns escalated=True.
- Service end-to-end against the real DB: start_adjudication
  persists the row + steps, status transitions to resolved /
  escalated / aborted correctly, and resolve_escalation appends an
  intervention step + flips status.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import _fake_ai
import pytest

from mycelium_core.adjudication import (
    AdjudicationContext,
    AdjudicationOutcome,
    Cap,
    CostModel,
    DissentNote,
    FallbackChain,
    Filter,
    InMemoryStepStore,
    Race,
    StepStore,
    StrategyRequirements,
    get_registry,
)
from mycelium_core.adjudication.policy import PolicyRouter, PolicyRule
from mycelium_core.adjudication.strategies import (
    HumanInLoopStrategy,
    SingleShotStrategy,
    register_builtins,
)
from mycelium_core.ai_providers import set_llm_override
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.adjudication import (
    AdjudicationStatus,
    AdjudicationStepKind,
)
from mycelium_core.services import adjudication as adj_svc
from mycelium_core.services.auth import signup

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(**overrides: Any) -> AdjudicationContext:
    """Build a minimal context for unit tests. Override at will."""
    defaults: dict[str, Any] = {
        "org_id": uuid.uuid4(),
        "actor_id": uuid.uuid4(),
        "adjudication_id": uuid.uuid4(),
        "question_text": "what should we do?",
        "context": {},
        "config": {},
    }
    defaults.update(overrides)
    return AdjudicationContext(**defaults)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


class _FixedOutcomeStrategy:
    """Test double that returns a pre-baked outcome and records one
    step. ``id`` is set per instance so multiple fixtures coexist."""

    requires = StrategyRequirements()
    composable_with: frozenset[str] = frozenset()
    mutually_exclusive_with: frozenset[str] = frozenset()
    cost_model = CostModel(fixed_tokens=10)

    def __init__(
        self,
        sid: str,
        outcome: AdjudicationOutcome,
        *,
        applicability: float = 0.5,
    ) -> None:
        self.id = sid
        self._outcome = outcome
        self._applicability = applicability

    def applicable(self, ctx: AdjudicationContext) -> float:
        return self._applicability

    async def run(self, ctx: AdjudicationContext, store: StepStore) -> AdjudicationOutcome:
        await store.append(
            kind=AdjudicationStepKind.synthesis,
            payload={"by": self.id, "preview": "stub"},
            agent_id=self.id,
        )
        return self._outcome


def _resolved(decision: str = "ok") -> AdjudicationOutcome:
    return AdjudicationOutcome(decision={"answer": decision}, confidence=0.9)


def _escalated() -> AdjudicationOutcome:
    return AdjudicationOutcome(
        decision={},
        confidence=0.0,
        escalated=True,
        residual_dissent=(DissentNote(agent_id="stub", position="unsure", rationale="x"),),
    )


@pytest.fixture(autouse=True)
def _reset_llm_override() -> Any:
    """Safety net for any test in this module that sets an LLM
    override: must never leak into the next test."""
    yield
    set_llm_override(None)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_register_and_get_roundtrip() -> None:
    reg = get_registry()
    reg.clear()
    s = _FixedOutcomeStrategy("alpha", _resolved())
    reg.register(s)
    assert reg.get("alpha") is s
    assert "alpha" in {x.id for x in reg.list_strategies()}


def test_registry_rank_filters_zero_and_orders_by_applicable_then_cost() -> None:
    reg = get_registry()
    reg.clear()
    reg.register(_FixedOutcomeStrategy("zero", _resolved(), applicability=0.0))
    reg.register(_FixedOutcomeStrategy("low", _resolved(), applicability=0.2))
    reg.register(_FixedOutcomeStrategy("high", _resolved(), applicability=0.8))

    class _Cheaper(_FixedOutcomeStrategy):
        cost_model = CostModel(fixed_tokens=1)

    reg.register(_Cheaper("high_cheap", _resolved(), applicability=0.8))

    scored = reg.rank(_ctx())
    ids = [s.id for _, s in scored]
    assert "zero" not in ids
    assert ids[0] == "high_cheap"  # cheaper wins ties
    assert ids[1] == "high"
    assert ids[2] == "low"


def test_registry_get_unknown_raises() -> None:
    reg = get_registry()
    reg.clear()
    with pytest.raises(KeyError):
        reg.get("nope")


def test_registry_validate_composition_mutually_exclusive() -> None:
    reg = get_registry()
    reg.clear()
    a = _FixedOutcomeStrategy("a", _resolved())
    b = _FixedOutcomeStrategy("b", _resolved())
    b.mutually_exclusive_with = frozenset({"a"})
    outer = _FixedOutcomeStrategy("outer", _resolved())
    with pytest.raises(ValueError, match="mutually exclusive"):
        reg.validate_composition(outer=outer, children=[a, b])


def test_registry_validate_composition_outer_whitelist() -> None:
    reg = get_registry()
    reg.clear()
    a = _FixedOutcomeStrategy("a", _resolved())
    outer = _FixedOutcomeStrategy("outer", _resolved())
    outer.composable_with = frozenset({"b"})
    with pytest.raises(ValueError, match="not composable"):
        reg.validate_composition(outer=outer, children=[a])


# ---------------------------------------------------------------------------
# Composition primitives
# ---------------------------------------------------------------------------


async def test_fallback_chain_advances_on_escalated_and_stops_on_resolved() -> None:
    get_registry().clear()
    chain = FallbackChain(
        [
            _FixedOutcomeStrategy("e1", _escalated()),
            _FixedOutcomeStrategy("e2", _escalated()),
            _FixedOutcomeStrategy("good", _resolved("final")),
        ]
    )
    store = InMemoryStepStore()
    outcome = await chain.run(_ctx(), store)
    assert outcome.escalated is False
    assert outcome.decision == {"answer": "final"}
    assert outcome.meta["fallback_chain"] == ["e1", "e2", "good"]
    assert len(await store.list_steps()) == 3


async def test_fallback_chain_stops_at_first_resolved() -> None:
    get_registry().clear()
    chain = FallbackChain(
        [
            _FixedOutcomeStrategy("good", _resolved("first")),
            _FixedOutcomeStrategy("never", _resolved("nope")),
        ]
    )
    store = InMemoryStepStore()
    outcome = await chain.run(_ctx(), store)
    assert outcome.decision == {"answer": "first"}
    assert outcome.meta["fallback_chain"] == ["good"]
    # The second strategy never ran -> only one step recorded.
    assert len(await store.list_steps()) == 1


def test_fallback_chain_requires_at_least_one_child() -> None:
    with pytest.raises(ValueError, match="at least one"):
        FallbackChain([])


async def test_cap_timeout_yields_aborted_outcome() -> None:
    get_registry().clear()

    class _Slow:
        id = "slow"
        requires = StrategyRequirements()
        composable_with: frozenset[str] = frozenset()
        mutually_exclusive_with: frozenset[str] = frozenset()
        cost_model = CostModel()

        def applicable(self, ctx: AdjudicationContext) -> float:
            return 1.0

        async def run(self, ctx: AdjudicationContext, store: StepStore) -> AdjudicationOutcome:
            await asyncio.sleep(10)
            return _resolved("never")

    capped = Cap(_Slow(), wall_s_max=0.05)
    store = InMemoryStepStore()
    outcome = await capped.run(_ctx(), store)
    assert outcome.aborted_reason == "cap_exceeded"
    assert outcome.decision == {}
    steps = await store.list_steps()
    # Cap writes an escalation step recording the cap event.
    assert any(s.kind == AdjudicationStepKind.escalation for s in steps)


async def test_cap_passthrough_when_under_limit() -> None:
    get_registry().clear()
    capped = Cap(_FixedOutcomeStrategy("fast", _resolved("done")), wall_s_max=5.0)
    store = InMemoryStepStore()
    outcome = await capped.run(_ctx(), store)
    assert outcome.decision == {"answer": "done"}
    assert outcome.aborted_reason is None


async def test_filter_engages_only_when_predicate_true() -> None:
    get_registry().clear()
    inner = _FixedOutcomeStrategy("inner", _resolved("via_filter"))
    f = Filter(inner, when=lambda c: c.context.get("stakes") == "high")
    store = InMemoryStepStore()

    out_engaged = await f.run(_ctx(context={"stakes": "high"}), store)
    assert out_engaged.decision == {"answer": "via_filter"}

    out_skipped = await f.run(_ctx(context={"stakes": "low"}), store)
    assert out_skipped.decision == {}
    assert out_skipped.meta["filter_engaged"] is False


async def test_filter_falls_back_when_predicate_false() -> None:
    get_registry().clear()
    inner = _FixedOutcomeStrategy("inner", _resolved("via_filter"))
    fb = _FixedOutcomeStrategy("fb", _resolved("fallback"))
    f = Filter(inner, when=lambda c: False, fallback=fb)
    store = InMemoryStepStore()
    outcome = await f.run(_ctx(), store)
    assert outcome.decision == {"answer": "fallback"}


async def test_race_returns_first_resolved_and_cancels_others() -> None:
    get_registry().clear()

    class _SlowResolved:
        id = "slow_ok"
        requires = StrategyRequirements()
        composable_with: frozenset[str] = frozenset()
        mutually_exclusive_with: frozenset[str] = frozenset()
        cost_model = CostModel()

        def applicable(self, ctx: AdjudicationContext) -> float:
            return 1.0

        async def run(self, ctx: AdjudicationContext, store: StepStore) -> AdjudicationOutcome:
            await asyncio.sleep(0.5)
            return _resolved("slow")

    fast = _FixedOutcomeStrategy("fast", _resolved("fast"))
    race = Race([fast, _SlowResolved()])
    store = InMemoryStepStore()
    outcome = await race.run(_ctx(), store)
    assert outcome.decision == {"answer": "fast"}


# ---------------------------------------------------------------------------
# Policy router
# ---------------------------------------------------------------------------


def test_policy_router_explicit_override_wins() -> None:
    reg = get_registry()
    reg.clear()
    a = _FixedOutcomeStrategy("a", _resolved(), applicability=0.1)
    b = _FixedOutcomeStrategy("b", _resolved(), applicability=0.9)
    reg.register(a)
    reg.register(b)
    router = PolicyRouter()
    sel, cfg = router.select(_ctx(config={"k": "v"}), override="a")
    assert sel.id == "a"
    assert cfg == {"k": "v"}


def test_policy_router_picks_first_matching_rule_by_priority() -> None:
    reg = get_registry()
    reg.clear()
    reg.register(_FixedOutcomeStrategy("debate_stub", _resolved()))
    reg.register(_FixedOutcomeStrategy("human_stub", _resolved()))
    router = PolicyRouter()
    router.add_rule(
        PolicyRule(
            name="fallback_human",
            when=lambda c: True,
            strategy_id="human_stub",
            priority=200,
        )
    )
    router.add_rule(
        PolicyRule(
            name="high_stakes_debate",
            when=lambda c: c.context.get("stakes") == "high",
            strategy_id="debate_stub",
            config={"n_agents": 3},
            priority=10,
        )
    )
    sel, cfg = router.select(_ctx(context={"stakes": "high"}))
    assert sel.id == "debate_stub"
    assert cfg == {"n_agents": 3}

    sel2, _ = router.select(_ctx(context={"stakes": "low"}))
    assert sel2.id == "human_stub"


def test_policy_router_predicate_exception_raises_runtime_error() -> None:
    reg = get_registry()
    reg.clear()
    reg.register(_FixedOutcomeStrategy("any", _resolved()))
    router = PolicyRouter()

    def _boom(_: AdjudicationContext) -> bool:
        raise RuntimeError("kaboom")

    router.add_rule(PolicyRule(name="bad", when=_boom, strategy_id="any"))
    with pytest.raises(RuntimeError, match="bad"):
        router.select(_ctx())


def test_policy_router_auto_rank_when_no_rule_matches() -> None:
    reg = get_registry()
    reg.clear()
    reg.register(_FixedOutcomeStrategy("low", _resolved(), applicability=0.2))
    reg.register(_FixedOutcomeStrategy("high", _resolved(), applicability=0.9))
    router = PolicyRouter()
    sel, _ = router.select(_ctx())
    assert sel.id == "high"


def test_policy_router_empty_registry_raises() -> None:
    reg = get_registry()
    reg.clear()
    router = PolicyRouter()
    with pytest.raises(LookupError):
        router.select(_ctx())


# ---------------------------------------------------------------------------
# Built-in strategies (unit)
# ---------------------------------------------------------------------------


async def test_single_shot_uses_llm_and_writes_turn_step() -> None:
    set_llm_override(lambda: _fake_ai.FakeLLM())
    s = SingleShotStrategy()
    store = InMemoryStepStore()
    ctx = _ctx(question_text="who should approve this?")
    outcome = await s.run(ctx, store)
    assert outcome.decision == {"answer": "echo: who should approve this?"}
    assert outcome.confidence == 0.5
    steps = await store.list_steps()
    assert len(steps) == 1
    assert steps[0].kind == AdjudicationStepKind.turn
    assert steps[0].agent_id == "single_shot"


async def test_human_in_loop_writes_escalation_and_returns_escalated() -> None:
    s = HumanInLoopStrategy()
    store = InMemoryStepStore()
    outcome = await s.run(
        _ctx(config={"reason": "external_review"}),
        store,
    )
    assert outcome.escalated is True
    assert outcome.confidence == 0.0
    assert outcome.residual_dissent
    steps = await store.list_steps()
    assert [(st.kind, st.agent_id) for st in steps] == [
        (AdjudicationStepKind.escalation, "human_in_loop")
    ]
    assert steps[0].payload["reason"] == "external_review"


# ---------------------------------------------------------------------------
# Service: end-to-end against the real DB.
# ---------------------------------------------------------------------------


async def test_service_start_with_single_shot_resolves_and_persists() -> None:
    register_builtins()  # restore in-tree strategies if a prior test cleared
    set_llm_override(lambda: _fake_ai.FakeLLM())

    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ADJ-SS")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        row = await adj_svc.start_adjudication(
            s,
            org_id=org,
            actor_id=user,
            question_text="should we approve?",
            strategy_id="single_shot",
        )
        assert row.status == AdjudicationStatus.resolved
        assert row.outcome_json is not None
        assert row.outcome_json["decision"] == {"answer": "echo: should we approve?"}
        assert row.confidence is not None
        steps = await adj_svc.list_adjudication_steps(s, org_id=org, adjudication_id=row.id)
        assert [st.kind for st in steps] == [AdjudicationStepKind.turn]


async def test_service_start_with_human_in_loop_escalates_and_resolves() -> None:
    register_builtins()

    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ADJ-HIL")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        row = await adj_svc.start_adjudication(
            s,
            org_id=org,
            actor_id=user,
            question_text="block this transfer?",
            strategy_id="human_in_loop",
            config={"reason": "amount_threshold"},
        )
        assert row.status == AdjudicationStatus.escalated
        steps = await adj_svc.list_adjudication_steps(s, org_id=org, adjudication_id=row.id)
        assert [st.kind for st in steps] == [AdjudicationStepKind.escalation]

        resolved = await adj_svc.resolve_escalation(
            s,
            org_id=org,
            actor_id=user,
            adjudication_id=row.id,
            decision={"answer": "approved"},
            rationale="reviewed offline",
        )
        assert resolved.status == AdjudicationStatus.resolved
        assert resolved.outcome_json is not None
        assert resolved.outcome_json["decision"] == {"answer": "approved"}
        steps2 = await adj_svc.list_adjudication_steps(s, org_id=org, adjudication_id=row.id)
        kinds = [st.kind for st in steps2]
        assert kinds == [
            AdjudicationStepKind.escalation,
            AdjudicationStepKind.intervention,
        ]


async def test_service_resolve_rejects_non_escalated_status() -> None:
    register_builtins()
    set_llm_override(lambda: _fake_ai.FakeLLM())

    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ADJ-RJ")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        row = await adj_svc.start_adjudication(
            s,
            org_id=org,
            actor_id=user,
            question_text="hi",
            strategy_id="single_shot",
        )
        assert row.status == AdjudicationStatus.resolved
        with pytest.raises(ValueError, match="not in 'escalated'"):
            await adj_svc.resolve_escalation(
                s,
                org_id=org,
                actor_id=user,
                adjudication_id=row.id,
                decision={"x": 1},
            )


async def test_service_strategy_exception_marks_aborted_and_reraises() -> None:
    register_builtins()
    get_registry().register(_FixedOutcomeStrategy("explode_strategy", _resolved()))

    class _Boom(_FixedOutcomeStrategy):
        async def run(self, ctx: AdjudicationContext, store: StepStore) -> AdjudicationOutcome:
            raise RuntimeError("strategy failure")

    get_registry().register(_Boom("kaboom", _resolved()))

    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ADJ-EX")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(RuntimeError, match="strategy failure"):
            await adj_svc.start_adjudication(
                s,
                org_id=org,
                actor_id=user,
                question_text="will fail",
                strategy_id="kaboom",
            )
        # The row exists with status aborted and the error captured.
        # We must read in a fresh session since the failing flush
        # poisoned the previous transaction context.

    async with tenant_session(str(org), str(user)) as s2:
        from sqlalchemy import select

        from mycelium_core.models.adjudication import Adjudication

        rows = (
            (await s2.execute(select(Adjudication).where(Adjudication.org_id == org)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == AdjudicationStatus.aborted
        assert rows[0].outcome_json is not None
        assert "strategy failure" in rows[0].outcome_json["error"]
