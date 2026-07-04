"""Adversarial verification of Track A retrieval observability (audit A-3/A-5).

A-3: a reranker that fails at query time must DEGRADE to RRF order AND surface
``rerank_failed`` in the meta (the degradation is not silent).
A-5: the unified search reports ``abstained`` only when it shaped an EMPTY
result -- a thin-but-nonempty unified result is never mislabelled.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_core.db import admin_session, tenant_session  # noqa: E402
from mycelium_core.models.note import NoteKind  # noqa: E402
from mycelium_core.reranker import RerankResult, set_reranker_override  # noqa: E402
from mycelium_core.services import memory, task_search  # noqa: E402
from mycelium_core.services import notes as nt  # noqa: E402
from mycelium_core.services.auth import signup  # noqa: E402


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="TRACKA")
    return r.org_id, r.user_id


class _FailingReranker:
    """A cross-encoder that errors at predict time (model missing / OOM)."""

    model_id = "boom"

    async def rerank(self, query: str, pairs: Sequence[str]) -> RerankResult:
        raise RuntimeError("reranker model unavailable")


async def test_rerank_failure_degrades_and_surfaces_in_meta() -> None:
    org, user = await _org()
    token = "zebra quumix vortex"  # 3 tokens -> passes the rerank query-len gate
    async with tenant_session(str(org), str(user)) as s:
        for i in range(6):  # >= reranker_min_candidates (5)
            await nt.create_note(
                s,
                org_id=org,
                actor_id=user,
                kind=NoteKind.text,
                title=f"n{i}",
                text=f"{token} note number {i}",
            )
    set_reranker_override(lambda: _FailingReranker())
    try:
        async with tenant_session(str(org), str(user)) as s:
            hits, meta = await memory.retrieve_with_meta(
                s,
                org_id=org,
                actor_id=user,
                project_id=None,
                query=token,
                operation_id=f"rrkfail-{uuid.uuid4().hex}",
                limit=10,
                rerank=True,
            )
    finally:
        set_reranker_override(None)
    assert meta.rerank_failed is True  # the degradation is observable, not silent
    assert len(hits) >= 1  # results still returned (RRF order preserved)


class _ConstLogitReranker:
    """Returns a fixed logit for every doc, so the reranker-logit abstain
    floor is exercised deterministically end-to-end (task f0d24fdb)."""

    model_id = "const-logit"

    def __init__(self, logit: float) -> None:
        self._logit = logit

    async def rerank(self, query: str, pairs: Sequence[str]) -> RerankResult:
        return RerankResult(scores=[self._logit] * len(pairs), model_id=self.model_id)


async def test_rerank_logit_grader_abstains_and_passes_end_to_end() -> None:
    """The honest-abstain quality floor keys off the cross-encoder logit
    (task f0d24fdb / N3), wired retrieve -> reranker -> grader: a fixed logit
    of 0.0 (relevance prob 0.5) abstains under a 0.9 floor with the
    logit-named reason, and passes under a 0.1 floor. Proves the whole
    plumbing, not just the stage."""
    org, user = await _org()
    token = "zebra quumix vortex"  # 3 tokens -> passes the rerank query-len gate
    async with tenant_session(str(org), str(user)) as s:
        for i in range(6):  # >= reranker_min_candidates (5) so the reranker fires
            await nt.create_note(
                s,
                org_id=org,
                actor_id=user,
                kind=NoteKind.text,
                title=f"n{i}",
                text=f"{token} note number {i}",
            )
    set_reranker_override(lambda: _ConstLogitReranker(0.0))  # sigmoid(0.0) == 0.5
    try:
        async with tenant_session(str(org), str(user)) as s:
            hits_hi, meta_hi = await memory.retrieve_with_meta(
                s,
                org_id=org,
                actor_id=user,
                project_id=None,
                query=token,
                operation_id=f"rrklogit-hi-{uuid.uuid4().hex}",
                limit=10,
                rerank=True,
                grader_min_rerank_logit=0.9,  # 0.9 > 0.5 -> abstain
            )
            assert hits_hi == []
            assert meta_hi.abstained is True
            assert meta_hi.abstain_reason == "grader_min_rerank_logit"
        async with tenant_session(str(org), str(user)) as s:
            hits_lo, meta_lo = await memory.retrieve_with_meta(
                s,
                org_id=org,
                actor_id=user,
                project_id=None,
                query=token,
                operation_id=f"rrklogit-lo-{uuid.uuid4().hex}",
                limit=10,
                rerank=True,
                grader_min_rerank_logit=0.1,  # 0.1 < 0.5 -> hits kept
            )
            assert len(hits_lo) >= 1
            assert meta_lo.abstained is False
    finally:
        set_reranker_override(None)


def _meta(*, abstained: bool) -> memory.RetrievalMeta:
    return memory.RetrievalMeta(
        query_embedded=True,
        dense_branch_contributed=True,
        dense_rejected_by_floor=0,
        keyword_only_hits=0,
        abstained=abstained,
        abstain_reason="grader_min_rrf" if abstained else None,
    )


class _FakeUnifiedHit:
    kind = "task"
    model_id = "BAAI/bge-m3"


def test_unified_abstained_only_when_result_is_empty() -> None:
    """A-5: ``abstained`` reflects an EMPTY result. One branch abstaining while
    another returns hits must NOT mark the (non-empty) unified result abstained;
    all branches abstaining with no hits must."""
    metas = [_meta(abstained=True), _meta(abstained=False)]
    # A branch abstained, but the unified result has hits -> NOT abstained.
    with_hits = task_search._aggregate_unified_meta(metas, [_FakeUnifiedHit()])  # type: ignore[list-item]
    assert with_hits.abstained is False
    assert with_hits.abstain_reason is None
    # A branch abstained and the result is empty -> abstained (the honest case).
    empty = task_search._aggregate_unified_meta(metas, [])
    assert empty.abstained is True
    assert empty.abstain_reason == "grader_min_rrf"
    # No branch abstained, empty result -> not abstained (genuinely nothing).
    none_abstained = task_search._aggregate_unified_meta([_meta(abstained=False)], [])
    assert none_abstained.abstained is False
