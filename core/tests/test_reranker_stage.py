"""Unit tests for the cross-encoder reranker stage + provider gate.

The DB-bound text hydration is exercised through the wider
integration suite; here we keep to the gate logic, score override,
and the Noop provider behavior.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from mycelium_core.reranker import NoopReranker, RerankResult, set_reranker_override
from mycelium_core.services.retrieval import Candidate
from mycelium_core.services.retrieval.stages import CrossEncoderRerankerStage, RerankGate
from mycelium_core.services.retrieval.types import RetrievalContext


class _StaticReranker:
    """Deterministic test stand-in: returns a descending sequence so
    we can verify the score override + ordering side-effect."""

    model_id = "test-rerank"

    async def rerank(self, query: str, pairs: Sequence[str]) -> RerankResult:
        scores = [float(len(pairs) - i) for i in range(len(pairs))]
        return RerankResult(scores=scores, model_id=self.model_id)


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
    out = await stage.run("short", _ctx_stub(), cands)
    # Scores untouched, no rerank entry in scores_by_stage.
    for c in out:
        assert "rerank" not in c.scores_by_stage


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


async def test_noop_reranker_returns_zeros() -> None:
    provider = NoopReranker()
    result = await provider.rerank("q", ["doc1", "doc2"])
    assert result.scores == [0.0, 0.0]
    assert result.model_id == "noop"


def test_provider_override_seam() -> None:
    """The set_reranker_override hook bypasses singletons (the same
    test seam pattern used by embedder)."""
    sentinel = NoopReranker()
    set_reranker_override(lambda: sentinel)
    from mycelium_core.reranker import get_reranker

    assert get_reranker() is sentinel
    _ = cast(NoopReranker, get_reranker())  # type narrows
