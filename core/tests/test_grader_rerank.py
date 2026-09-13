"""Unit tests for the reranker abstain floor in ``GraderMinStage``
(task f0d24fdb / N3).

Pure stage-level tests: build Candidates with controlled ``scores_by_stage``
and run the stage against a stub context, so the precedence rule and the
threshold are verified without a DB. The values used are the ones a REAL
provider produces (``test_the_floor_reads_the_number_a_real_provider_sends``
pins that), because the version of these tests that invented its own scale
is what let a double sigmoid live in the stage: every case here passed with
logits because the fixture supplied logits.

The end-to-end wiring (retrieve -> reranker -> grader) lives in test_f6_memory.
"""

from __future__ import annotations

import os
import uuid

import pytest

from mycelium_core.services.retrieval import Candidate
from mycelium_core.services.retrieval.stages import GraderMinStage
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
        score = rerank  # after rerank the aggregate score IS the rerank score
    return Candidate(blob_id=uuid.uuid4(), score=score, scores_by_stage=sbs)


async def test_rerank_floor_abstains_when_top_score_below_floor() -> None:
    # The provider's own number, compared as it arrives: 0.5 against a 0.9
    # floor abstains. No squash on the way in -- see the class docstring.
    ctx = _ctx()
    out = await GraderMinStage(min_rerank_score=0.9).run("q", ctx, [_cand(rerank=0.5)])
    assert out == []
    assert ctx.extras["grader_abstained"] is True
    assert ctx.extras["grader_abstain_reason"] == "grader_min_rerank_score"


async def test_rerank_floor_passes_when_top_score_at_or_above_floor() -> None:
    ctx = _ctx()
    cands = [_cand(rerank=0.5)]
    out = await GraderMinStage(min_rerank_score=0.1).run("q", ctx, cands)
    assert out == cands
    assert "grader_abstained" not in ctx.extras


async def test_rerank_floor_is_noop_when_reranker_did_not_run() -> None:
    """The floor can only grade a signal it has: with no ``rerank``
    score present (reranker gated off / too few candidates) the floor is a
    no-op and the result passes through -- honestly documented, not a silent
    abstain on a missing signal."""
    ctx = _ctx()
    cands = [_cand(rrf=0.01)]  # only a fused score, never reranked
    out = await GraderMinStage(min_rerank_score=0.99).run("q", ctx, cands)
    assert out == cands
    assert "grader_abstained" not in ctx.extras


async def test_rerank_floor_takes_precedence_over_rrf_floor_when_reranked() -> None:
    """After reranking the top item can carry a LOW fused RRF score (it
    reranked up from a low RRF rank); the RRF floor must NOT fire on it --
    the rerank floor is the sole authority when the reranker ran."""
    ctx = _ctx()
    # Fused score 0.001 is below a 0.02 RRF floor, but the rerank score (0.5)
    # clears the 0.1 rerank floor: the hit is kept, RRF floor is bypassed.
    cands = [_cand(rrf=0.001, rerank=0.5)]
    out = await GraderMinStage(min_score=0.02, min_rerank_score=0.1).run("q", ctx, cands)
    assert out == cands
    assert "grader_abstained" not in ctx.extras


async def test_none_rerank_floor_is_byte_identical_to_rrf_only() -> None:
    """The None pin: with ``min_rerank_score=None`` the stage behaves exactly
    like the historical RRF-only grader, whether or not the item was
    reranked."""
    # Reranked item, RRF floor fires on the preserved fused score.
    ctx = _ctx()
    out = await GraderMinStage(min_score=0.02, min_rerank_score=None).run(
        "q", ctx, [_cand(rrf=0.001, rerank=0.99)]
    )
    assert out == []
    assert ctx.extras["grader_abstain_reason"] == "grader_min_rrf"

    # RRF floor passes -> hit kept, no abstain recorded.
    ctx2 = _ctx()
    cands = [_cand(rrf=0.04, rerank=0.99)]
    out2 = await GraderMinStage(min_score=0.02, min_rerank_score=None).run("q", ctx2, cands)
    assert out2 == cands
    assert "grader_abstained" not in ctx2.extras


async def test_both_floors_none_passes_through() -> None:
    ctx = _ctx()
    cands = [_cand(rrf=0.001, rerank=-5.0)]
    out = await GraderMinStage().run("q", ctx, cands)
    assert out == cands
    assert "grader_abstained" not in ctx.extras


async def test_the_floor_compares_the_number_it_was_given() -> None:
    """The boundary is EXACTLY the floor, which is what pins "no transform".

    This is the cheap half of the guard that was missing. Any monotone
    function slipped between the provider and the comparison keeps every
    other test in this file green (they assert order, and order survives a
    sigmoid) and moves this boundary: under the double squash that shipped,
    a candidate at 0.30 cleared a 0.30 floor, because what was compared was
    sigmoid(0.30) = 0.574.
    """
    at = await GraderMinStage(min_rerank_score=0.30).run("q", _ctx(), [_cand(rerank=0.30)])
    assert at != [], "a score exactly at the floor is not below it"

    just_under = await GraderMinStage(min_rerank_score=0.30).run(
        "q", _ctx(), [_cand(rerank=0.2999)]
    )
    assert just_under == []


# --- Off-CI: the contract against a REAL provider. The blocking gate cannot
# download a cross-encoder (third-party fetch + torch), which is the whole
# reason the range went undeclared and unchecked for a release. Set
# MYCELIUM_RERANKER_CONTRACT_MODEL to a cross-encoder id to run it; the
# cheapest one that exercises the same contract is
# cross-encoder/ms-marco-MiniLM-L6-v2 (22M). ---

_CONTRACT_MODEL = os.environ.get("MYCELIUM_RERANKER_CONTRACT_MODEL", "")


def test_remote_code_cannot_be_executed_from_a_moving_reference() -> None:
    """``trust_remote_code`` without a pinned commit is refused.

    The unpinned form is exactly as easy to write and silently unbounded:
    what runs is whatever that repository serves at load time. Pinning is
    therefore a requirement of the constructor and not a convention, and this
    is the assertion that keeps it one.
    """
    import pytest as _pytest

    from mycelium_core.reranker import LocalReranker

    with _pytest.raises(ValueError, match="code_revision"):
        LocalReranker("Alibaba-NLP/gte-multilingual-reranker-base", trust_remote_code=True)

    # Pinned: accepted (no load happens in the constructor).
    LocalReranker(
        "Alibaba-NLP/gte-multilingual-reranker-base",
        trust_remote_code=True,
        code_revision="40ced75c3017eb27626c9d4ea981bde21a2662f4",
    )
    # And the default path, which executes nothing, needs no pin.
    LocalReranker("BAAI/bge-reranker-v2-m3")


@pytest.mark.skipif(not _CONTRACT_MODEL, reason="MYCELIUM_RERANKER_CONTRACT_MODEL not set")
async def test_the_floor_reads_the_number_a_real_provider_sends() -> None:
    """The scores a real provider hands over are in [0,1] and ordered.

    Asserting the RANGE and not just the order is the point: a provider that
    returned raw logits would satisfy every ordering test in this file and
    every retrieval metric in the bench, and would only show up as an abstain
    floor that does nothing until it does everything.
    """
    from mycelium_core.reranker import LocalReranker

    provider = LocalReranker(_CONTRACT_MODEL, trust_remote_code=False)
    result = await provider.rerank(
        "where is the abstain threshold read from?",
        [
            "the abstain threshold is read from the reranker score",
            "a recipe for carbonara, which is not about thresholds at all",
        ],
    )
    assert len(result.scores) == 2
    assert all(0.0 <= x <= 1.0 for x in result.scores), result.scores
    assert result.scores[0] > result.scores[1]
