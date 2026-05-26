"""Unit tests for the DedupeBySourceStage. The DB-bound hydration is
covered through the integration suite; this file checks the pure
collapse logic + no-op behavior on legacy data.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from flow_core.services.retrieval import Candidate
from flow_core.services.retrieval.stages import DedupeBySourceStage
from flow_core.services.retrieval.types import RetrievalContext


def _ctx_stub(*, empty_select: bool = True) -> RetrievalContext:
    """``empty_select=True`` returns a session whose execute().all()
    yields an empty list (used for tests that hand candidates with
    source_id already populated, so the hydrate SELECT is a no-op).
    Tests that need a specific row set should patch session.execute
    after construction."""
    from sqlalchemy import true as sql_true

    mock_result = MagicMock()
    mock_result.all.return_value = []
    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)
    return RetrievalContext(
        session=session,
        org_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        project_id=None,
        operation_id="t",
        embedder=None,  # type: ignore[arg-type]
        project_pred=sql_true(),
        tag_clauses=(),
        query_embedding=None,
    )


def _cand_with_source(source_id: str, chunk_index: int, score: float) -> Candidate:
    return Candidate(
        blob_id=uuid.uuid4(),
        score=score,
        source_kind="note",
        source_id=source_id,
        chunk_index=chunk_index,
    )


async def test_dedupe_keeps_first_occurrence_per_source() -> None:
    note_id = "note-abc"
    c1 = _cand_with_source(note_id, chunk_index=2, score=0.9)  # winning chunk
    c2 = _cand_with_source(note_id, chunk_index=0, score=0.5)  # lower-score sibling
    c3 = _cand_with_source(note_id, chunk_index=1, score=0.3)
    out = await DedupeBySourceStage().run("q", _ctx_stub(), [c1, c2, c3])
    assert len(out) == 1
    assert out[0] is c1
    # Winning chunk_index is preserved.
    assert out[0].chunk_index == 2


async def test_dedupe_keeps_distinct_sources() -> None:
    a = _cand_with_source("a", chunk_index=0, score=0.9)
    b = _cand_with_source("b", chunk_index=0, score=0.8)
    c = _cand_with_source("c", chunk_index=0, score=0.7)
    out = await DedupeBySourceStage().run("q", _ctx_stub(), [a, b, c])
    assert out == [a, b, c]


async def test_dedupe_passes_through_candidates_without_provenance() -> None:
    # Candidates with no source_id should not collapse against each
    # other (they're treated as distinct).
    a = Candidate(blob_id=uuid.uuid4(), score=0.9)
    b = Candidate(blob_id=uuid.uuid4(), score=0.8)
    out = await DedupeBySourceStage().run("q", _ctx_stub(), [a, b])
    assert out == [a, b]


async def test_dedupe_empty_input() -> None:
    out = await DedupeBySourceStage().run("q", _ctx_stub(), [])
    assert out == []


async def test_dedupe_mixed_sources_and_sourceless() -> None:
    sourced_winner = _cand_with_source("doc-1", chunk_index=1, score=0.9)
    sourced_loser = _cand_with_source("doc-1", chunk_index=0, score=0.5)
    sourceless = Candidate(blob_id=uuid.uuid4(), score=0.6)
    out = await DedupeBySourceStage().run(
        "q", _ctx_stub(), [sourced_winner, sourceless, sourced_loser]
    )
    # Winner of doc-1 + sourceless candidate. Order preserved.
    assert out == [sourced_winner, sourceless]
