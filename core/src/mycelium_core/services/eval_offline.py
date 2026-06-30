"""Offline retrieval eval — deterministic gold-set regression gate
(ADR-0035, task ca351859 / Mycelio WS-E1).

The problem this closes: ``recall_at_k`` (garden_health) is a
self-referential proxy (clicks at rank 1) and ``is_probe`` *excludes*
probes instead of seeding a held-out set, so there is no deterministic
way to tell whether retrieval is actually any good or to catch a
regression. This module is the instrument: given a gold set of
``{query -> expected blob}`` cases over a seeded corpus, it runs the
REAL ``memory.retrieve`` pipeline and reports recall@k + MRR, plus a
dense-tier health check (blobs must carry real embeddings, not the
``model_id='none'`` keyword-only state -- the WS-A failure mode).

It is pure measurement (no I/O beyond the session it is handed), so it
serves three callers identically: the CI pytest gate (a fixed synthetic
gold set, see ``tests/test_eval_offline.py``), a future CLI/worker run
against a real org, and ad-hoc debugging.

Faithfulness of distillation is a SEPARATE axis (grounding/verify, task
a44e72a4) and is intentionally out of scope here -- it needs an
LLM-as-judge, which is neither deterministic nor available in CI.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.services import memory

# Sidecar marker a blob carries when it was written with no embedder
# available (keyword-only). Mirrors ``memory._NO_EMBED_MODEL``; duplicated
# here to avoid importing a private name.
_NO_EMBED_MODEL = "none"


@dataclass(frozen=True)
class GoldCase:
    """One held-out query and the blob id(s) that correctly answer it."""

    query: str
    expected: frozenset[uuid.UUID]


@dataclass(frozen=True)
class CaseResult:
    query: str
    # 1-based rank of the first expected blob within the top-k, or None
    # when no expected blob made the cut.
    rank: int | None
    hit_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class EvalReport:
    k: int
    n_cases: int
    recall_at_k: float  # fraction of cases with an expected blob in top-k
    mrr: float  # mean reciprocal rank of the first expected blob
    dense_blobs: int  # org blobs carrying a real embedding
    total_blobs: int  # org blobs total
    cases: tuple[CaseResult, ...]
    # Cases where the per-org grader/abstain floor returned []: a non-zero count
    # explains a recall collapse that would otherwise look like a corpus problem
    # (adversarial audit A-7 -- a mis-set retrieval_grader_min_rrf).
    abstained_cases: int = 0

    @property
    def dense_tier_nonempty(self) -> bool:
        """At least one blob carries a real dense vector. False is the
        WS-A regression (the whole corpus fell back to keyword-only)."""
        return self.dense_blobs > 0


async def dense_tier_health(session: AsyncSession, *, org_id: uuid.UUID) -> tuple[int, int]:
    """``(#blobs with a real embedding, #total blobs)`` for the org. A blob
    with ``model_id='none'`` / NULL embedding is keyword-only; if that is
    the whole corpus the dense tier is dead (WS-A)."""
    total = (
        await session.execute(
            select(func.count()).select_from(MemoryBlob).where(MemoryBlob.org_id == org_id)
        )
    ).scalar_one()
    dense = (
        await session.execute(
            select(func.count())
            .select_from(MemoryBlob)
            .where(
                MemoryBlob.org_id == org_id,
                MemoryBlob.embedding.is_not(None),
                MemoryBlob.model_id != _NO_EMBED_MODEL,
            )
        )
    ).scalar_one()
    return int(dense), int(total)


async def run_eval(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    cases: Sequence[GoldCase],
    k: int = 10,
) -> EvalReport:
    """Run every gold case through the real ``memory.retrieve`` and
    aggregate recall@k + MRR, plus the dense-tier health of the org's
    corpus. Deterministic given a deterministic embedder + seeded data."""
    results: list[CaseResult] = []
    found = 0
    rr_sum = 0.0
    abstained_cases = 0
    for case in cases:
        # retrieve_with_meta (not bare retrieve) so a recall collapse caused by
        # a mis-set grader/abstain floor surfaces as meta.abstained instead of
        # masquerading as "the corpus doesn't contain the answer" (A-7).
        hits, meta = await memory.retrieve_with_meta(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=None,
            query=case.query,
            operation_id=f"eval-{uuid.uuid4().hex}",
            limit=k,
        )
        if meta.abstained:
            abstained_cases += 1
        hit_ids = tuple(h.blob.id for h in hits)
        rank = next((i + 1 for i, bid in enumerate(hit_ids) if bid in case.expected), None)
        if rank is not None:
            found += 1
            rr_sum += 1.0 / rank
        results.append(CaseResult(query=case.query, rank=rank, hit_ids=hit_ids))
    n = len(cases)
    dense, total = await dense_tier_health(session, org_id=org_id)
    return EvalReport(
        k=k,
        n_cases=n,
        recall_at_k=(found / n if n else 0.0),
        mrr=(rr_sum / n if n else 0.0),
        dense_blobs=dense,
        total_blobs=total,
        cases=tuple(results),
        abstained_cases=abstained_cases,
    )


def load_cases(path: str | Path) -> list[GoldCase]:
    """Load gold cases from a JSONL file: one object per line with ``query``
    (str) and ``expected_blob_ids`` (a non-empty list of blob-id strings --
    the stored blobs that correctly answer the query). Blank lines skipped.

    The file-driven counterpart to the in-code synthetic gold set, so an
    EXTERNAL bench (a LongMemEval / LOCOMO subset, ingested into an org and
    resolved to stored blob ids) runs through the SAME ``run_eval`` without
    code changes -- the first step toward a public, comparable measurement of
    "is this the most powerful memory" (WS-E1 follow-up). A multi-target
    ``expected_blob_ids`` scores as a hit when ANY id lands in top-k, matching
    ``run_eval``'s recall semantics."""
    cases: list[GoldCase] = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON ({exc})") from exc
        query = obj.get("query")
        ids = obj.get("expected_blob_ids")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"{path}:{lineno}: missing/blank 'query'")
        if not isinstance(ids, list) or not ids:
            raise ValueError(f"{path}:{lineno}: 'expected_blob_ids' must be a non-empty list")
        try:
            expected = frozenset(uuid.UUID(str(i)) for i in ids)
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: bad blob id ({exc})") from exc
        cases.append(GoldCase(query=query, expected=expected))
    return cases


async def run_eval_from_file(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    path: str | Path,
    k: int = 10,
) -> EvalReport:
    """Convenience: load the gold cases from ``path`` and run them through
    :func:`run_eval` against the corpus ALREADY ingested under ``org_id``
    (the ingestion of an external bench corpus is the caller's job; this only
    measures). Keeps the synthetic CI gate and the external-bench run on one
    measurement path."""
    return await run_eval(session, org_id=org_id, actor_id=actor_id, cases=load_cases(path), k=k)


@dataclass(frozen=True)
class ForgettingReport:
    """Verified forgetting / GDPR right-to-erasure -- a GOVERNANCE axis the
    public memory benchmarks omit. Erasing a subject's provenance must make its
    answers actually unretrievable, not merely hidden."""

    erased: int  # blobs deleted by the provenance erase
    recall_before: float
    recall_after: float

    @property
    def forgotten(self) -> bool:
        """The erase removed >=1 blob AND recall strictly dropped."""
        return self.erased > 0 and self.recall_after < self.recall_before


async def gdpr_forgetting(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_kind: str,
    source_id: str,
    cases: Sequence[GoldCase],
    k: int = 10,
) -> ForgettingReport:
    """Measure verified forgetting: recall over ``cases``, then erase one
    subject's provenance via ``memory.gdpr_erase``, then recall again. A
    compliant memory drops recall for the erased subject (the blobs are GONE,
    not just hidden) -- the metric where self-hostable, provenance-auditable
    memory beats hosted competitors that never score it. Reuses ``run_eval``."""
    before = await run_eval(session, org_id=org_id, actor_id=actor_id, cases=cases, k=k)
    erased = await memory.gdpr_erase(
        session, org_id=org_id, actor_id=actor_id, source_kind=source_kind, source_id=source_id
    )
    after = await run_eval(session, org_id=org_id, actor_id=actor_id, cases=cases, k=k)
    return ForgettingReport(
        erased=erased, recall_before=before.recall_at_k, recall_after=after.recall_at_k
    )
