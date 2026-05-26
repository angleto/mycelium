"""Unit tests for the retrieval pipeline executor + canonical stages
that don't need a DB session (fusion, ordering, limit, grader-min).
The DB-bound stages (lexical, semantic, access-counter) are exercised
by the existing ``test_memory_redesign`` + ``test_search_unified``
integration suites.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import pytest

from flow_core.services.retrieval import (
    Candidate,
    RetrievalContext,
    RetrievalPipeline,
    Stage,
    merge_candidates,
)
from flow_core.services.retrieval.stages import (
    GraderMinStage,
    LimitStage,
    OrderingStage,
    RRFFusionStage,
)


@dataclass
class _FakeAddStage(Stage):
    """Drops a fixed candidate list into the pipeline, simulating a
    branch (lexical/semantic) without touching the DB."""

    name: str
    candidates: list[Candidate]

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        return merge_candidates(candidates, self.candidates)


def _cand(*, blob_id: uuid.UUID | None = None, stage: str, rank: int) -> Candidate:
    return Candidate(
        blob_id=blob_id or uuid.uuid4(),
        scores_by_stage={stage: float(rank)},
    )


def _ctx_stub() -> RetrievalContext:
    # The non-DB stages don't touch session/predicates; pass throwaway
    # values typed correctly.
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


async def test_pipeline_runs_stages_in_order() -> None:
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    pipeline = RetrievalPipeline(
        stages=[
            _FakeAddStage(
                name="lex",
                candidates=[
                    Candidate(blob_id=id_a, scores_by_stage={"lex": 1.0}),
                    Candidate(blob_id=id_b, scores_by_stage={"lex": 2.0}),
                ],
            ),
            _FakeAddStage(
                name="sem",
                candidates=[
                    Candidate(blob_id=id_b, scores_by_stage={"sem": 1.0}),
                    Candidate(blob_id=id_a, scores_by_stage={"sem": 2.0}),
                ],
            ),
            RRFFusionStage(k=60),
        ]
    )
    out = await pipeline.run("q", _ctx_stub())
    by_id = {c.blob_id: c for c in out}
    # Both candidates carry both per-branch ranks after merge.
    assert by_id[id_a].scores_by_stage == {"lex": 1.0, "sem": 2.0}
    assert by_id[id_b].scores_by_stage == {"lex": 2.0, "sem": 1.0}
    # RRF score = 1/(60+lex_rank) + 1/(60+sem_rank). Both pairs sum to the same value
    # (1/61 + 1/62) so order at this point is undefined; the OrderingStage
    # would later tie-break on created_at/id (see other test).
    expected = 1.0 / 61 + 1.0 / 62
    assert by_id[id_a].score == pytest.approx(expected)
    assert by_id[id_b].score == pytest.approx(expected)


async def test_ordering_tie_breaks_on_created_at_then_id() -> None:
    older = uuid.UUID("00000000-0000-0000-0000-000000000001")
    newer = uuid.UUID("00000000-0000-0000-0000-000000000002")
    c1 = Candidate(
        blob_id=newer,
        score=0.5,
        created_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
    )
    c2 = Candidate(
        blob_id=older,
        score=0.5,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    pipeline = RetrievalPipeline(stages=[OrderingStage()])
    out = await pipeline.run("q", _ctx_stub())  # empty start
    # Re-inject via fake stage path
    out = [c1, c2]
    out = await OrderingStage().run("q", _ctx_stub(), out)
    assert [c.blob_id for c in out] == [older, newer]


async def test_grader_min_drops_weak_top() -> None:
    weak = Candidate(blob_id=uuid.uuid4(), score=0.01)
    pipeline = RetrievalPipeline(stages=[GraderMinStage(min_score=0.05)])
    out = await pipeline.run("q", _ctx_stub())
    out = [weak]
    out = await GraderMinStage(min_score=0.05).run("q", _ctx_stub(), out)
    assert out == []


async def test_grader_min_keeps_strong_top() -> None:
    strong = Candidate(blob_id=uuid.uuid4(), score=0.5)
    other = Candidate(blob_id=uuid.uuid4(), score=0.1)
    out = await GraderMinStage(min_score=0.05).run("q", _ctx_stub(), [strong, other])
    assert len(out) == 2


async def test_limit_truncates() -> None:
    cands = [Candidate(blob_id=uuid.uuid4(), score=1.0 - i * 0.1) for i in range(10)]
    out = await LimitStage(k=3).run("q", _ctx_stub(), cands)
    assert len(out) == 3
    assert out == cands[:3]


def test_merge_candidates_accumulates_per_stage_scores() -> None:
    bid = uuid.uuid4()
    existing = [Candidate(blob_id=bid, scores_by_stage={"lex": 1.0})]
    incoming = [Candidate(blob_id=bid, scores_by_stage={"sem": 2.0})]
    merged = merge_candidates(existing, incoming)
    assert len(merged) == 1
    assert merged[0].scores_by_stage == {"lex": 1.0, "sem": 2.0}


def test_merge_candidates_appends_new_ids() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    existing = [Candidate(blob_id=a, scores_by_stage={"lex": 1.0})]
    incoming = [Candidate(blob_id=b, scores_by_stage={"sem": 1.0})]
    merged = merge_candidates(existing, incoming)
    assert [c.blob_id for c in merged] == [a, b]
