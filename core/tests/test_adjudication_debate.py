"""DebateStrategy (docs/adr/0027, P3) tests.

A scripted LLM drives the multi-round loop end-to-end; a FakeEmbedder
gives deterministic cosine similarity over token overlap, so coherence
energy is a function of the LLM script. Covers:

- pure helpers (``_coherence_energy``, ``_parse_turn_text``,
  ``_confidence_for``);
- convergence path (round-0 stop when debaters agree);
- stability path (no changed_mind for ``stability_window`` rounds);
- exhaustion path (``max_rounds`` reached);
- judge synthesis with explicit residual dissent (ADR-0027 §D7);
- ``use_judge=False`` majority-vote synthesis;
- devil's-advocate enables an extra debater;
- service end-to-end against the real DB: status resolves, kind log
  contains turns + scores + a synthesis step.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

import _fake_ai
import pytest
from _fake_embedder import FakeEmbedder

from flow_core.adjudication.base import AdjudicationContext
from flow_core.adjudication.store import InMemoryStepStore
from flow_core.adjudication.strategies.debate import (
    DebateStrategy,
    _coherence_energy,
    _confidence_for,
    _parse_turn_text,
)
from flow_core.ai_providers import LLMResult, set_llm_override
from flow_core.db import admin_session, tenant_session
from flow_core.embedder import set_embedder_override
from flow_core.models.adjudication import AdjudicationStatus, AdjudicationStepKind
from flow_core.services import adjudication as adj_svc
from flow_core.services.auth import signup

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


_Step = str | Any  # str or Callable[[Sequence[tuple[str, str]]], str]


class _ScriptLLM:
    """Same shape as the one in test_assistant.py: a fixed list of
    replies cycled per call. A reply can be a literal string or a
    callable for context-sensitive answers."""

    model_id = "fake-llm"

    def __init__(self, steps: list[_Step]) -> None:
        self._steps = steps
        self._i = 0

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        step = self._steps[min(self._i, len(self._steps) - 1)]
        self._i += 1
        text = step(messages) if callable(step) else step
        return LLMResult(text=text, tokens_in=1, tokens_out=1, model_id=self.model_id)


def _turn(position: str, rationale: str, changed_mind: bool) -> str:
    return json.dumps(
        {
            "position": position,
            "rationale": rationale,
            "changed_mind": changed_mind,
        }
    )


def _judge(answer: str, dissent: list[dict[str, str]] | None = None) -> str:
    return json.dumps(
        {
            "answer": answer,
            "rationale": "stub",
            "dissent": dissent or [],
        }
    )


def _ctx(question: str = "should we ship?", **overrides: Any) -> AdjudicationContext:
    defaults: dict[str, Any] = {
        "org_id": uuid.uuid4(),
        "actor_id": uuid.uuid4(),
        "adjudication_id": uuid.uuid4(),
        "question_text": question,
        "context": {},
        "config": {},
    }
    defaults.update(overrides)
    return AdjudicationContext(**defaults)


@pytest.fixture(autouse=True)
def _reset_overrides() -> Any:
    yield
    set_llm_override(None)
    set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_coherence_energy_zero_when_identical() -> None:
    v = [1.0, 0.0, 0.0]
    assert _coherence_energy([v, v, v]) == 0.0


def test_coherence_energy_high_when_orthogonal() -> None:
    e = _coherence_energy([[1.0, 0.0], [0.0, 1.0]])
    # Two unit vectors at 90°: centroid = (0.5, 0.5), normalised at
    # 45°; each is at 45° from the centroid -> cosine ~ 0.707,
    # distance ~ 0.293.
    assert 0.28 < e < 0.31


def test_coherence_energy_returns_zero_for_singleton() -> None:
    assert _coherence_energy([[1.0, 0.0, 0.0]]) == 0.0


def test_parse_turn_text_valid_json() -> None:
    position, rationale, changed = _parse_turn_text(
        _turn("ship it", "tests are green", changed_mind=True),
        default_changed_mind=False,
    )
    assert position == "ship it"
    assert rationale == "tests are green"
    assert changed is True


def test_parse_turn_text_fallback_to_raw() -> None:
    position, rationale, changed = _parse_turn_text("not a json reply", default_changed_mind=True)
    assert position == "not a json reply"
    assert rationale == ""
    assert changed is True


def test_parse_turn_text_json_embedded_in_prose() -> None:
    payload = _turn("delay", "risk", changed_mind=False)
    position, _, changed = _parse_turn_text(
        f"After consideration: {payload}\nThanks.", default_changed_mind=True
    )
    assert position == "delay"
    assert changed is False


def test_confidence_ordering() -> None:
    assert _confidence_for("converged", 0.05) > _confidence_for("stable", None)
    assert _confidence_for("stable", None) > _confidence_for("exhausted", None)
    # Converged with very high coherence -> low confidence, but still
    # at least 0.7 (the floor).
    assert _confidence_for("converged", 0.9) >= 0.7


# ---------------------------------------------------------------------------
# Debate loop paths (in-memory)
# ---------------------------------------------------------------------------


async def test_debate_converges_when_all_agree_in_round_zero() -> None:
    # Every debater + judge answers with identical content -> embeddings
    # collapse to one direction -> coherence energy 0 -> converged
    # after round 0.
    set_embedder_override(FakeEmbedder)
    set_llm_override(
        lambda: _ScriptLLM(
            [
                _turn("ship", "ok", changed_mind=True),
                _turn("ship", "ok", changed_mind=True),
                _turn("ship", "ok", changed_mind=True),
                _judge("ship"),
            ]
        )
    )
    s = DebateStrategy()
    store = InMemoryStepStore()
    outcome = await s.run(_ctx(config={"max_rounds": 3}), store)
    assert outcome.meta["stop_reason"] == "converged"
    assert outcome.meta["rounds_run"] == 1
    assert outcome.confidence >= 0.9
    # 3 turns + 1 score + 1 synthesis.
    steps = await store.list_steps()
    kinds = [st.kind for st in steps]
    assert kinds.count(AdjudicationStepKind.turn) == 3
    assert kinds.count(AdjudicationStepKind.score) == 1
    assert kinds.count(AdjudicationStepKind.synthesis) == 1


async def test_debate_stops_on_stability_after_window_rounds() -> None:
    # Three debaters with divergent positions but ``changed_mind=false``
    # from round 0 on. Stability window = 2 -> after round 1 (second
    # round) the streak reaches 2 and the loop stops with ``stable``.
    set_embedder_override(FakeEmbedder)
    set_llm_override(
        lambda: _ScriptLLM(
            [
                # Round 0: positions diverge, but already not changing.
                _turn("approve", "yes", changed_mind=False),
                _turn("delay", "wait", changed_mind=False),
                _turn("reject", "no", changed_mind=False),
                # Round 1: same.
                _turn("approve", "yes", changed_mind=False),
                _turn("delay", "wait", changed_mind=False),
                _turn("reject", "no", changed_mind=False),
                # Judge.
                _judge("delay", dissent=[{"agent_id": "debater_0", "position": "approve"}]),
            ]
        )
    )
    s = DebateStrategy()
    store = InMemoryStepStore()
    outcome = await s.run(
        _ctx(
            config={
                "max_rounds": 5,
                "stability_window": 2,
                "coherence_threshold": 0.0001,  # force coherence to not trigger
            }
        ),
        store,
    )
    assert outcome.meta["stop_reason"] == "stable"
    assert outcome.meta["rounds_run"] == 2
    assert outcome.confidence == 0.65


async def test_debate_exhausts_when_no_convergence_and_keeps_changing() -> None:
    # Every round positions diverge AND ``changed_mind=true`` always ->
    # neither convergence nor stability ever triggers -> exhausted.
    set_embedder_override(FakeEmbedder)
    set_llm_override(
        lambda: _ScriptLLM(
            [
                _turn(f"alpha-{i}", "x", changed_mind=True)
                if i % 3 == 0
                else _turn(f"beta-{i}", "y", changed_mind=True)
                if i % 3 == 1
                else _turn(f"gamma-{i}", "z", changed_mind=True)
                for i in range(9)
            ]
            + [_judge("undecided")]
        )
    )
    s = DebateStrategy()
    store = InMemoryStepStore()
    outcome = await s.run(_ctx(config={"max_rounds": 2}), store)
    assert outcome.meta["stop_reason"] == "exhausted"
    assert outcome.meta["rounds_run"] == 2
    assert outcome.confidence == 0.40


async def test_debate_judge_surfaces_residual_dissent() -> None:
    set_embedder_override(FakeEmbedder)
    set_llm_override(
        lambda: _ScriptLLM(
            [
                _turn("ship", "tests pass", changed_mind=True),
                _turn("ship", "tests pass", changed_mind=True),
                _turn("ship", "tests pass", changed_mind=True),
                _judge(
                    "ship",
                    dissent=[
                        {
                            "agent_id": "debater_1",
                            "position": "delay",
                            "rationale": "no rollback plan",
                        }
                    ],
                ),
            ]
        )
    )
    s = DebateStrategy()
    store = InMemoryStepStore()
    outcome = await s.run(_ctx(), store)
    assert outcome.decision["answer"] == "ship"
    assert len(outcome.residual_dissent) == 1
    assert outcome.residual_dissent[0].agent_id == "debater_1"
    assert outcome.residual_dissent[0].position == "delay"


async def test_debate_without_judge_majority_vote_synthesis() -> None:
    # 3 debaters, 2 say "ship" and 1 says "delay" -> majority wins, the
    # third becomes residual dissent. No judge call.
    set_embedder_override(FakeEmbedder)
    set_llm_override(
        lambda: _ScriptLLM(
            [
                _turn("ship", "ok", changed_mind=False),
                _turn("ship", "ok", changed_mind=False),
                _turn("delay", "wait", changed_mind=False),
            ]
        )
    )
    s = DebateStrategy()
    store = InMemoryStepStore()
    outcome = await s.run(
        _ctx(
            config={
                "max_rounds": 1,
                "use_judge": False,
                "coherence_threshold": 0.0001,
            }
        ),
        store,
    )
    assert outcome.decision == {"answer": "ship"}
    assert len(outcome.residual_dissent) == 1
    assert outcome.residual_dissent[0].position == "delay"
    assert outcome.meta["judge_used"] is False


async def test_debate_devils_advocate_adds_extra_debater() -> None:
    set_embedder_override(FakeEmbedder)
    # 4 debaters now (3 defaults + devil); 1 round so 5 LLM calls (+
    # judge).
    set_llm_override(
        lambda: _ScriptLLM(
            [
                _turn("ship", "ok", changed_mind=True),
                _turn("ship", "ok", changed_mind=True),
                _turn("ship", "ok", changed_mind=True),
                _turn("ship", "ok", changed_mind=True),
                _judge("ship"),
            ]
        )
    )
    s = DebateStrategy()
    store = InMemoryStepStore()
    outcome = await s.run(_ctx(config={"max_rounds": 1, "devils_advocate": True}), store)
    assert outcome.meta["n_debaters"] == 4
    steps = await store.list_steps()
    devils_turn = [
        st for st in steps if st.kind == AdjudicationStepKind.turn and st.payload["devils_advocate"]
    ]
    assert len(devils_turn) == 1
    assert devils_turn[0].agent_id == "debater_3"


# ---------------------------------------------------------------------------
# Service end-to-end
# ---------------------------------------------------------------------------


async def test_service_starts_debate_and_persists_full_timeline() -> None:
    set_embedder_override(FakeEmbedder)
    set_llm_override(lambda: _fake_ai.FakeLLM())  # echo: ... -> trivial converge

    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ADJ-DBT")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        row = await adj_svc.start_adjudication(
            s,
            org_id=org,
            actor_id=user,
            question_text="ship the patch?",
            strategy_id="debate",
            config={"max_rounds": 2, "use_judge": True},
        )
        assert row.status == AdjudicationStatus.resolved
        # echo-based answers are not valid JSON so each turn falls back
        # to raw text as position; embeddings still align (same prefix)
        # so the loop converges in round 0.
        assert row.outcome_json is not None
        steps = await adj_svc.list_adjudication_steps(s, org_id=org, adjudication_id=row.id)
        kinds = [st.kind for st in steps]
        assert kinds.count(AdjudicationStepKind.turn) == 3
        assert kinds.count(AdjudicationStepKind.score) == 1
        assert kinds.count(AdjudicationStepKind.synthesis) == 1
        # All turn embeddings persisted.
        turn_steps = [st for st in steps if st.kind == AdjudicationStepKind.turn]
        assert all(st.embedding is not None for st in turn_steps)
