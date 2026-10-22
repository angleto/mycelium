"""Unit tests for the cross-encoder reranker stage + provider gate.

The DB-bound text hydration is exercised through the wider
integration suite; here we keep to the gate logic, score override,
and the Noop provider behavior.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Sequence
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from mycelium_core.reranker import (
    LocalReranker,
    NoopReranker,
    RerankerUnavailable,
    RerankResult,
    cost_units,
    set_reranker_override,
)
from mycelium_core.services.retrieval import Candidate
from mycelium_core.services.retrieval.stages import (
    CrossEncoderRerankerStage,
    RerankGate,
    RerankOutcome,
    combine_outcomes,
)
from mycelium_core.services.retrieval.types import RetrievalContext


class _StaticReranker:
    """Deterministic test stand-in: returns a descending sequence so
    we can verify the score override + ordering side-effect."""

    model_id = "test-rerank"

    def estimate_seconds(self, query: str, pairs: Sequence[str]) -> float | None:
        return 0.0

    async def rerank(self, query: str, pairs: Sequence[str]) -> RerankResult:
        scores = [float(len(pairs) - i) for i in range(len(pairs))]
        return RerankResult(scores=scores, model_id=self.model_id)


class _RaisingReranker:
    """A reranker whose model load/predict fails (the missing optional extra
    in prod). The stage must degrade to the RRF order, not crash the search."""

    model_id = "boom"

    def estimate_seconds(self, query: str, pairs: Sequence[str]) -> float | None:
        return 0.0

    async def rerank(self, query: str, pairs: Sequence[str]) -> RerankResult:
        raise RuntimeError("reranker model unavailable")


@pytest.fixture(autouse=True)
def _reset_reranker_override() -> Iterator[None]:
    yield
    set_reranker_override(None)


def _ctx_stub() -> RetrievalContext:
    from sqlalchemy import true as sql_true

    return RetrievalContext(
        session=None,  # type: ignore[arg-type]
        org_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        project_id=None,
        operation_id="t",
        embedder=None,  # type: ignore[arg-type]
        project_pred=sql_true(),
        tag_clauses=(),
        query_embedding=None,
    )


def _cand(text: str, score: float = 0.5) -> Candidate:
    return Candidate(blob_id=uuid.uuid4(), score=score, text=text)


def test_gate_skips_short_query() -> None:
    gate = RerankGate(min_query_tokens=3, min_candidates=2)
    assert gate.should_rerank("hi", [_cand("a"), _cand("b"), _cand("c")]) is False


def test_gate_skips_few_candidates() -> None:
    gate = RerankGate(min_query_tokens=2, min_candidates=5)
    assert gate.should_rerank("one two three", [_cand("a"), _cand("b")]) is False


def test_gate_accepts_when_thresholds_met() -> None:
    gate = RerankGate(min_query_tokens=2, min_candidates=2)
    assert gate.should_rerank("one two three", [_cand("a"), _cand("b"), _cand("c")]) is True


async def test_rerank_overrides_score_and_preserves_rrf_in_diagnostics() -> None:
    stage = CrossEncoderRerankerStage(
        provider=_StaticReranker(),
        gate=RerankGate(min_query_tokens=1, min_candidates=1),
    )
    a = _cand("alpha", score=0.05)
    b = _cand("bravo", score=0.04)
    c = _cand("charlie", score=0.03)
    a.scores_by_stage = {"rrf": 0.05}
    b.scores_by_stage = {"rrf": 0.04}
    c.scores_by_stage = {"rrf": 0.03}

    out = await stage.run("one two three", _ctx_stub(), [a, b, c])
    # _StaticReranker returns [3, 2, 1] -> alpha gets 3, charlie gets 1.
    assert a.score == 3.0
    assert b.score == 2.0
    assert c.score == 1.0
    # RRF score preserved for diagnostics.
    assert a.scores_by_stage["rrf"] == 0.05
    assert a.scores_by_stage["rerank"] == 3.0
    # Stage doesn't sort -- OrderingStage downstream does the re-sort.
    # Order preserved in the input candidate list.
    assert out == [a, b, c]


async def test_rerank_skips_under_gate() -> None:
    stage = CrossEncoderRerankerStage(
        provider=_StaticReranker(),
        gate=RerankGate(min_query_tokens=3, min_candidates=5),
    )
    cands = [_cand(f"c{i}", score=0.1 * i) for i in range(3)]
    ctx = _ctx_stub()
    out = await stage.run("short", ctx, cands)
    # Scores untouched, no rerank entry in scores_by_stage.
    for c in out:
        assert "rerank" not in c.scores_by_stage
    assert ctx.extras["rerank"] is RerankOutcome.SKIPPED_GATE


async def test_rerank_with_no_candidates_is_noop() -> None:
    stage = CrossEncoderRerankerStage(
        provider=_StaticReranker(),
        gate=RerankGate(min_query_tokens=1, min_candidates=0),
    )
    out = await stage.run("one two three", _ctx_stub(), [])
    assert out == []


async def test_rerank_loads_missing_text_via_db() -> None:
    """When candidates carry no text, the stage queries the DB once
    for the missing ones. Mock the session execute to verify the SELECT
    fires exactly once even with multiple missing candidates."""
    session = AsyncMock()
    # Result.all() is sync and returns the list of (id, text) tuples.
    # MagicMock (not AsyncMock) for .all() so it doesn't return a coroutine.
    mock_result = MagicMock()
    bid_a, bid_b = uuid.uuid4(), uuid.uuid4()
    mock_result.all.return_value = [(bid_a, "alpha text"), (bid_b, "bravo text")]
    # session.execute is awaited; returns the (sync) result object.
    session.execute = AsyncMock(return_value=mock_result)
    ctx = _ctx_stub()
    object.__setattr__(ctx, "session", session)

    stage = CrossEncoderRerankerStage(
        provider=_StaticReranker(),
        gate=RerankGate(min_query_tokens=1, min_candidates=1),
    )
    cand_a = Candidate(blob_id=bid_a, score=0.5)  # no text
    cand_b = Candidate(blob_id=bid_b, score=0.4)  # no text
    out = await stage.run("one two", ctx, [cand_a, cand_b])

    assert session.execute.await_count == 1  # single SELECT for both missing
    assert cand_a.text == "alpha text"
    assert cand_b.text == "bravo text"
    assert out == [cand_a, cand_b]


async def test_noop_reranker_cannot_score() -> None:
    provider = NoopReranker()
    assert provider.estimate_seconds("q", ["doc1"]) is None
    with pytest.raises(RerankerUnavailable):
        await provider.rerank("q", ["doc1", "doc2"])


def test_provider_override_seam() -> None:
    """The set_reranker_override hook bypasses singletons (the same
    test seam pattern used by embedder)."""
    sentinel = NoopReranker()
    set_reranker_override(lambda: sentinel)
    from mycelium_core.reranker import get_reranker

    assert get_reranker() is sentinel
    _ = cast(NoopReranker, get_reranker())  # type narrows


async def test_rerank_degrades_gracefully_on_provider_failure() -> None:
    # A missing/broken cross-encoder model must NOT crash the search: the
    # stage degrades to the upstream RRF order (mirrors memory._safe_embed).
    # This is the prerequisite that makes enabling the reranker by default safe.
    stage = CrossEncoderRerankerStage(
        provider=_RaisingReranker(),
        gate=RerankGate(min_query_tokens=1, min_candidates=1),
    )
    a = _cand("alpha", score=0.05)
    b = _cand("bravo", score=0.04)
    a.scores_by_stage = {"rrf": 0.05}
    b.scores_by_stage = {"rrf": 0.04}
    ctx = _ctx_stub()
    out = await stage.run("one two three", ctx, [a, b])
    assert out == [a, b]
    assert a.score == 0.05 and b.score == 0.04
    assert "rerank" not in a.scores_by_stage
    assert ctx.extras["rerank"] is RerankOutcome.FAILED


async def test_rerank_with_feature_off_says_unavailable_and_keeps_rrf_order() -> None:
    # The feature gated off but rerank requested per-call: the RRF order passes
    # through UNCHANGED (a flat score would let OrderingStage re-sort on the
    # created_at tiebreak) AND the request is reported as unavailable.
    #
    # The second half is the defect this guards. Observed failing against the
    # version that carried it (HEAD 59bc556b, 2026-10-22): the same request
    # with the production configuration (reranker_enabled=False, provider
    # NoopReranker) left the order untouched and ``ctx.extras`` EMPTY, so the
    # caller of a reranked search could not tell it had not been reranked.
    stage = CrossEncoderRerankerStage(
        provider=NoopReranker(),
        gate=RerankGate(min_query_tokens=1, min_candidates=1),
    )
    a = _cand("alpha", score=0.05)
    b = _cand("bravo", score=0.04)
    a.scores_by_stage = {"rrf": 0.05}
    b.scores_by_stage = {"rrf": 0.04}
    ctx = _ctx_stub()
    out = await stage.run("one two three", ctx, [a, b])
    assert out == [a, b]
    assert a.score == 0.05 and b.score == 0.04
    assert "rerank" not in a.scores_by_stage
    assert ctx.extras["rerank"] is RerankOutcome.UNAVAILABLE


class _MeteredReranker:
    """A provider whose estimate is a known cost per pair, recording every
    batch it is asked to score. Implements the whole contract: an estimate
    that grows with the batch, ``None`` once it is told it cannot score, and
    [0,1] scores that rank the batch in the order it arrived reversed, so a
    reranked prefix is visibly re-ordered."""

    model_id = "metered"

    def __init__(self, seconds_per_pair: float, *, unavailable_after: int | None = None) -> None:
        self.seconds_per_pair = seconds_per_pair
        self.batches: list[list[str]] = []
        self._unavailable_after = unavailable_after

    def _gone(self) -> bool:
        return self._unavailable_after is not None and len(self.batches) >= self._unavailable_after

    def estimate_seconds(self, query: str, pairs: Sequence[str]) -> float | None:
        if self._gone():
            return None
        return self.seconds_per_pair * len(pairs)

    async def rerank(self, query: str, pairs: Sequence[str]) -> RerankResult:
        if self._gone():
            raise RerankerUnavailable("gone")
        self.batches.append(list(pairs))
        # Spends the time it declared, like a real forward pass: the stage
        # judges the budget on the clock, not on the estimate.
        await asyncio.sleep(self.seconds_per_pair * len(pairs))
        n = len(pairs)
        return RerankResult(scores=[(i + 1) / (n + 1) for i in range(n)], model_id=self.model_id)


def _rrf_list(n: int) -> list[Candidate]:
    out = []
    for i in range(n):
        c = _cand(f"doc{i}", score=0.016 - i * 0.001)
        c.scores_by_stage = {"rrf": c.score}
        out.append(c)
    return out


async def test_budget_decides_depth_and_tail_keeps_rrf() -> None:
    # 0.1 s of budget at 0.03 s a pair buys 3 pairs: the first three
    # candidates in RRF order are reranked, the other five keep their RRF score.
    provider = _MeteredReranker(0.03)
    stage = CrossEncoderRerankerStage(
        provider=provider, budget_s=0.1, gate=RerankGate(min_query_tokens=1, min_candidates=1)
    )
    cands = _rrf_list(8)
    ctx = _ctx_stub()
    await stage.run("one two three", ctx, cands)
    assert ctx.extras["rerank"] is RerankOutcome.RAN
    assert sum(len(b) for b in provider.batches) == 3
    assert all("rerank" in c.scores_by_stage for c in cands[:3])
    assert all("rerank" not in c.scores_by_stage for c in cands[3:])
    assert [c.score for c in cands[3:]] == [0.016 - i * 0.001 for i in range(3, 8)]


async def test_budget_that_cannot_buy_two_pairs_skips_and_says_so() -> None:
    # The production node: ~1.67 s a pair against a 1 s budget. Not even two
    # pairs fit, so nothing is scored and the caller is told why.
    provider = _MeteredReranker(1.67)
    stage = CrossEncoderRerankerStage(
        provider=provider, budget_s=1.0, gate=RerankGate(min_query_tokens=1, min_candidates=1)
    )
    cands = _rrf_list(8)
    ctx = _ctx_stub()
    await stage.run("one two three", ctx, cands)
    assert ctx.extras["rerank"] is RerankOutcome.SKIPPED_BUDGET
    assert provider.batches == []
    assert all("rerank" not in c.scores_by_stage for c in cands)


async def test_a_larger_budget_reaches_deeper_on_the_same_provider() -> None:
    # The production node's ratio scaled down 100x (1.67 s a pair, a 20 s
    # agent budget): depth follows the budget, here floor(0.2 / 0.0167) = 11.
    provider = _MeteredReranker(0.0167)
    stage = CrossEncoderRerankerStage(
        provider=provider, budget_s=0.2, gate=RerankGate(min_query_tokens=1, min_candidates=1)
    )
    ctx = _ctx_stub()
    await stage.run("one two three", ctx, _rrf_list(30))
    assert ctx.extras["rerank"] is RerankOutcome.RAN
    assert sum(len(b) for b in provider.batches) == 11


async def test_max_pairs_pins_depth_for_measurement() -> None:
    provider = _MeteredReranker(0.0)
    stage = CrossEncoderRerankerStage(
        provider=provider,
        budget_s=1000.0,
        max_pairs=4,
        gate=RerankGate(min_query_tokens=1, min_candidates=1),
    )
    ctx = _ctx_stub()
    await stage.run("one two three", ctx, _rrf_list(10))
    assert sum(len(b) for b in provider.batches) == 4


async def test_provider_lost_after_a_batch_keeps_what_ran() -> None:
    # 0.03 s a pair in 0.1 s: the first batch takes 3 of 6, and the provider
    # is gone before the second. What was scored stays applied, nothing more
    # is attempted, and the result is reported as the reranked prefix it is.
    provider = _MeteredReranker(0.03, unavailable_after=1)
    stage = CrossEncoderRerankerStage(
        provider=provider, budget_s=0.1, gate=RerankGate(min_query_tokens=1, min_candidates=1)
    )
    cands = _rrf_list(6)
    ctx = _ctx_stub()
    await stage.run("one two three", ctx, cands)
    assert [len(b) for b in provider.batches] == [3]
    assert all("rerank" in c.scores_by_stage for c in cands[:3])
    assert all("rerank" not in c.scores_by_stage for c in cands[3:])
    assert ctx.extras["rerank"] is RerankOutcome.RAN


def test_combine_outcomes_reports_the_worst_and_ignores_unrequested() -> None:
    assert combine_outcomes([None, None]) is None
    assert combine_outcomes([RerankOutcome.RAN, None]) is RerankOutcome.RAN
    assert combine_outcomes([RerankOutcome.RAN, RerankOutcome.SKIPPED_BUDGET]) is (
        RerankOutcome.SKIPPED_BUDGET
    )
    assert combine_outcomes([RerankOutcome.UNAVAILABLE, RerankOutcome.FAILED]) is (
        RerankOutcome.FAILED
    )


def test_cost_units_pads_to_the_longest_pair() -> None:
    assert cost_units("qq", []) == 0
    assert cost_units("qq", ["a", "bbbb"]) == 2 * (2 + 4)


async def test_local_reranker_never_loads_inside_a_request() -> None:
    # Constructed, never prewarmed: it cannot estimate and refuses to score,
    # instead of loading the model inline (114.6 s and ~2.7 GiB on the
    # production node, task 03cdf674).
    provider = LocalReranker("not-a-real-model")
    assert provider.estimate_seconds("q", ["d"]) is None
    with pytest.raises(RerankerUnavailable):
        await provider.rerank("q", ["d"])
    assert provider._model is None


def test_local_reranker_estimate_includes_queued_work_and_follows_measurements() -> None:
    provider = LocalReranker("not-a-real-model")
    provider._model = object()  # stands for a loaded model; nothing is run
    provider._observe(100, 1.0)  # first measurement seeds: 0.01 s/unit
    assert provider.estimate_seconds("q", ["x" * 9]) == pytest.approx(0.01 * 10)
    provider._pending_units = 90  # another request is being scored
    assert provider.estimate_seconds("q", ["x" * 9]) == pytest.approx(0.01 * 100)
    provider._pending_units = 0
    provider._observe(100, 2.0)  # slower batch: moves the estimate a third of the way
    assert provider.estimate_seconds("q", ["x" * 9]) == pytest.approx((0.01 + 0.3 * 0.01) * 10)


async def test_a_slow_text_load_is_not_charged_to_the_budget() -> None:
    # The budget is the cross-encoder's. A database that takes longer than the
    # whole budget to hand over the texts must not turn a zero-cost provider
    # into "over budget". Observed failing against the first cut of the
    # budgeted stage (clock started before the SELECT): SKIPPED_BUDGET.
    session = AsyncMock()
    result = MagicMock()
    ids = [uuid.uuid4() for _ in range(3)]
    result.all.return_value = [(i, f"text {n}") for n, i in enumerate(ids)]

    async def _slow_execute(*_a: object, **_k: object) -> MagicMock:
        await asyncio.sleep(0.15)
        return result

    session.execute = _slow_execute
    ctx = _ctx_stub()
    object.__setattr__(ctx, "session", session)
    stage = CrossEncoderRerankerStage(
        provider=_MeteredReranker(0.0),
        budget_s=0.1,
        gate=RerankGate(min_query_tokens=1, min_candidates=1),
    )
    cands = [Candidate(blob_id=i, score=0.01) for i in ids]
    await stage.run("one two three", ctx, cands)
    assert ctx.extras["rerank"] is RerankOutcome.RAN
