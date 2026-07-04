"""Offline retrieval eval — the deterministic CI regression gate
(ADR-0035, task ca351859 / Mycelio WS-E1).

A fixed synthetic gold set (``{query -> expected note}`` over a seeded,
lexically-distinct corpus) run through the REAL ``memory.retrieve``. This
is the gate the design asks for:

- recall@k / MRR must not drop below the committed baseline (a retrieval
  or fusion regression -- e.g. the humus stage crowding out live notes --
  moves these down and fails here);
- the dense tier must be non-empty AND every blob must carry a real
  embedding (the WS-A ``model_id='none'`` keyword-only failure mode).

Deterministic via the FakeEmbedder seam; notes index at ``tenant_session``
teardown (same convention as test_note_search).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.models.note import NoteKind
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.services import eval_offline
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup
from mycelium_core.services.eval_offline import GoldCase

# --- The gold set: lexically-distinct topics so the correct answer is
# unambiguous and the baseline is stable. (query, note body). ---
_CORPUS: tuple[tuple[str, str], ...] = (
    (
        "kubernetes autoscaler replicas",
        "Kubernetes horizontal pod autoscaler scales replicas on CPU load",
    ),
    (
        "postgres btree index planner",
        "PostgreSQL btree index speeds up the query planner for range scans",
    ),
    (
        "rust borrow checker lifetime",
        "The Rust borrow checker enforces lifetime and ownership at compile time",
    ),
    (
        "espresso grind tamper crema",
        "Espresso extraction needs a fine grind and a firm tamper for good crema",
    ),
    ("ski touring climbing skins", "Ski touring uses climbing skins on the base for the ascent"),
    (
        "fourier transform frequencies",
        "The Fourier transform decomposes a signal into its constituent frequencies",
    ),
)

# Committed baseline (this fixture, FakeEmbedder). The gate fails if a
# regression pushes the metrics below these. Distinct vocabulary makes the
# correct note the unambiguous top hit, so both land at 1.0; a small
# tolerance guards MRR against an incidental rank-2 from shared stopwords.
_BASELINE_RECALL_AT_K = 1.0
_BASELINE_MRR = 0.95
_K = 5


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _seed_org_with_gold() -> tuple[uuid.UUID, uuid.UUID, list[GoldCase]]:
    """Sign up a fresh org and seed the gold corpus; return the org/user
    plus the gold cases keyed to the real blob ids."""
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="EVAL")
    org, user = r.org_id, r.user_id
    note_ids: list[uuid.UUID] = []
    async with tenant_session(str(org), str(user)) as s:
        for _query, body in _CORPUS:
            note = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, text=body)
            note_ids.append(note.id)
    # Resolve each note's indexed blob (pointer is 1:1 per part) in a fresh
    # session, once the teardown flush of the previous one has landed.
    cases: list[GoldCase] = []
    async with tenant_session(str(org), str(user)) as s:
        for (query, _body), nid in zip(_CORPUS, note_ids, strict=True):
            blob_id = (
                await s.execute(
                    select(NotePartIndexPointer.blob_id).where(NotePartIndexPointer.note_id == nid)
                )
            ).scalar_one()
            cases.append(GoldCase(query=query, expected=frozenset({blob_id})))
    return org, user, cases


async def test_offline_eval_meets_baseline(_embedder: None) -> None:
    org, user, cases = await _seed_org_with_gold()
    async with tenant_session(str(org), str(user)) as s:
        report = await eval_offline.run_eval(s, org_id=org, actor_id=user, cases=cases, k=_K)

    # Retrieval quality must not regress below the committed baseline.
    assert report.n_cases == len(_CORPUS)
    assert report.recall_at_k >= _BASELINE_RECALL_AT_K, (
        f"recall@{_K}={report.recall_at_k} below baseline {_BASELINE_RECALL_AT_K}; "
        f"misses={[c.query for c in report.cases if c.rank is None]}"
    )
    assert report.mrr >= _BASELINE_MRR, f"MRR={report.mrr} below baseline {_BASELINE_MRR}"


async def test_offline_eval_dense_tier_is_healthy(_embedder: None) -> None:
    """The WS-A sentinel: the seeded corpus must carry real embeddings, not
    fall back to the keyword-only ``model_id='none'`` state."""
    org, user, _cases = await _seed_org_with_gold()
    async with tenant_session(str(org), str(user)) as s:
        dense, total = await eval_offline.dense_tier_health(s, org_id=org)
    assert total >= len(_CORPUS)
    assert dense > 0  # dense tier alive
    assert dense == total  # no blob fell back to keyword-only (model_id='none')


async def test_run_eval_threads_rerank_logit_floor(
    _embedder: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reranker-logit abstain floor (task f0d24fdb) threads through
    ``run_eval`` so a bench can sweep the honest-abstain gate: with the
    reranker firing, a floor above the top relevance probability abstains
    every case (recall 0, abstained), below it recall is restored. Proves the
    eval path reaches the grader, not just ``retrieve``."""
    from mycelium_core.config import get_settings
    from mycelium_core.reranker import RerankResult, set_reranker_override

    class _ConstReranker:
        model_id = "const"

        async def rerank(self, query: str, pairs: object) -> RerankResult:
            n = len(pairs)  # type: ignore[arg-type]
            return RerankResult(scores=[0.0] * n, model_id=self.model_id)

    token = "zebra quumix vortex"  # 3 tokens + 6 blobs -> the rerank gate fires
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="EVALRR")
    org, user = r.org_id, r.user_id
    note_ids: list[uuid.UUID] = []
    async with tenant_session(str(org), str(user)) as s:
        for i in range(6):
            note = await nt.create_note(
                s, org_id=org, actor_id=user, kind=NoteKind.text, text=f"{token} note number {i}"
            )
            note_ids.append(note.id)
    async with tenant_session(str(org), str(user)) as s:
        gold = (
            await s.execute(
                select(NotePartIndexPointer.blob_id).where(
                    NotePartIndexPointer.note_id == note_ids[0]
                )
            )
        ).scalar_one()
    cases = [GoldCase(query=token, expected=frozenset({gold}))]

    # run_eval has no per-call rerank flag: the stage is added only when the
    # reranker is enabled workspace-wide, so flip the env for this test.
    monkeypatch.setenv("MYCELIUM_RERANKER_ENABLED", "true")
    get_settings.cache_clear()
    set_reranker_override(lambda: _ConstReranker())
    try:
        async with tenant_session(str(org), str(user)) as s:
            hi = await eval_offline.run_eval(
                s, org_id=org, actor_id=user, cases=cases, k=10, grader_min_rerank_logit=0.9
            )
        assert hi.recall_at_k == 0.0
        assert hi.abstained_cases == 1
        async with tenant_session(str(org), str(user)) as s:
            lo = await eval_offline.run_eval(
                s, org_id=org, actor_id=user, cases=cases, k=10, grader_min_rerank_logit=0.1
            )
        assert lo.recall_at_k == 1.0
        assert lo.abstained_cases == 0
    finally:
        set_reranker_override(None)
        get_settings.cache_clear()
