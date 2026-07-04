"""Unit tests for the reranker-logit abstain floor in ``GraderMinStage``
(task f0d24fdb / N3).

Pure stage-level tests: build Candidates with controlled ``scores_by_stage``
and run the stage against a stub context, so the precedence rule and the
sigmoid threshold are verified without a reranker model or a DB. The
end-to-end wiring (retrieve -> reranker -> grader) lives in test_f6_memory.
"""

from __future__ import annotations

import math
import uuid

from mycelium_core.services.retrieval import Candidate
from mycelium_core.services.retrieval.stages import GraderMinStage
from mycelium_core.services.retrieval.stages.order_limit import _sigmoid
from mycelium_core.services.retrieval.types import RetrievalContext


def _ctx() -> RetrievalContext:
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


def _cand(
    *, rrf: float | None = None, rerank: float | None = None, score: float = 0.5
) -> Candidate:
    sbs: dict[str, float] = {}
    if rrf is not None:
        sbs["rrf"] = rrf
    if rerank is not None:
        sbs["rerank"] = rerank
        score = rerank  # after rerank the aggregate score IS the logit
    return Candidate(blob_id=uuid.uuid4(), score=score, scores_by_stage=sbs)


def test_sigmoid_stable_and_monotone() -> None:
    assert _sigmoid(0.0) == 0.5
    assert math.isclose(_sigmoid(2.0), 1.0 / (1.0 + math.exp(-2.0)))
    assert math.isclose(_sigmoid(-2.0), math.exp(-2.0) / (1.0 + math.exp(-2.0)))
    # Overflow-safe at the extremes (raw logits never reach here, but the
    # helper must not raise): huge negative would overflow a naive exp(-x).
    assert _sigmoid(-1000.0) >= 0.0
    assert _sigmoid(1000.0) <= 1.0
    assert _sigmoid(-1.0) < _sigmoid(1.0)


async def test_rerank_floor_abstains_when_top_prob_below_floor() -> None:
    # logit 0.0 -> sigmoid 0.5; floor 0.9 (probability) -> abstain.
    ctx = _ctx()
    out = await GraderMinStage(min_rerank_prob=0.9).run("q", ctx, [_cand(rerank=0.0)])
    assert out == []
    assert ctx.extras["grader_abstained"] is True
    assert ctx.extras["grader_abstain_reason"] == "grader_min_rerank_logit"


async def test_rerank_floor_passes_when_top_prob_at_or_above_floor() -> None:
    ctx = _ctx()
    cands = [_cand(rerank=0.0)]
    out = await GraderMinStage(min_rerank_prob=0.1).run("q", ctx, cands)
    assert out == cands
    assert "grader_abstained" not in ctx.extras


async def test_rerank_floor_is_noop_when_reranker_did_not_run() -> None:
    """The logit floor can only grade a signal it has: with no ``rerank``
    score present (reranker gated off / too few candidates) the floor is a
    no-op and the result passes through -- honestly documented, not a silent
    abstain on a missing signal."""
    ctx = _ctx()
    cands = [_cand(rrf=0.01)]  # only a fused score, never reranked
    out = await GraderMinStage(min_rerank_prob=0.99).run("q", ctx, cands)
    assert out == cands
    assert "grader_abstained" not in ctx.extras


async def test_rerank_floor_takes_precedence_over_rrf_floor_when_reranked() -> None:
    """After reranking the top item can carry a LOW fused RRF score (it
    reranked up from a low RRF rank); the RRF floor must NOT fire on it --
    the rerank floor is the sole authority when the reranker ran."""
    ctx = _ctx()
    # Fused score 0.001 is below a 0.02 RRF floor, but the rerank prob (0.5)
    # clears the 0.1 rerank floor: the hit is kept, RRF floor is bypassed.
    cands = [_cand(rrf=0.001, rerank=0.0)]
    out = await GraderMinStage(min_score=0.02, min_rerank_prob=0.1).run("q", ctx, cands)
    assert out == cands
    assert "grader_abstained" not in ctx.extras


async def test_none_rerank_floor_is_byte_identical_to_rrf_only() -> None:
    """The None pin: with ``min_rerank_prob=None`` the stage behaves exactly
    like the historical RRF-only grader, whether or not the item was
    reranked."""
    # Reranked item, RRF floor fires on the preserved fused score.
    ctx = _ctx()
    out = await GraderMinStage(min_score=0.02, min_rerank_prob=None).run(
        "q", ctx, [_cand(rrf=0.001, rerank=5.0)]
    )
    assert out == []
    assert ctx.extras["grader_abstain_reason"] == "grader_min_rrf"

    # RRF floor passes -> hit kept, no abstain recorded.
    ctx2 = _ctx()
    cands = [_cand(rrf=0.04, rerank=5.0)]
    out2 = await GraderMinStage(min_score=0.02, min_rerank_prob=None).run("q", ctx2, cands)
    assert out2 == cands
    assert "grader_abstained" not in ctx2.extras


async def test_both_floors_none_passes_through() -> None:
    ctx = _ctx()
    cands = [_cand(rrf=0.001, rerank=-5.0)]
    out = await GraderMinStage().run("q", ctx, cands)
    assert out == cands
    assert "grader_abstained" not in ctx.extras
