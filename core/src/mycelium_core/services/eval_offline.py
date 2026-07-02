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
class ConsolidationCase:
    """A consolidation-recall case for the humus A/B: a query whose intended
    answer is a humus atom (``atom_expected``), plus the raw source blob(s) the
    atom was derived from (``source_expected``). The fairness filter uses the
    latter: if, with humus atoms fully excluded, the query already retrieves a
    raw SOURCE, the atom adds no recall (a raw note answers) and the case is
    dropped as unfair. A genuine cross-note consolidation query retrieves NONE
    of its sources (the generalization is in no single one)."""

    query: str
    atom_expected: frozenset[uuid.UUID]
    source_expected: frozenset[uuid.UUID]


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
    project_id: uuid.UUID | None = None,
    humus: bool | None = None,
    humus_kinds: frozenset[str] | None = None,
    exclude_humus_from_base: bool = False,
) -> EvalReport:
    """Run every gold case through the real ``memory.retrieve`` and
    aggregate recall@k + MRR, plus the dense-tier health of the org's
    corpus. Deterministic given a deterministic embedder + seeded data.

    ``project_id`` picks the retrieval perimeter and follows
    ``memory._project_pred`` semantics: None means blobs with NO project
    (``project_id IS NULL``), NOT "no filter". A real, project-scoped corpus
    MUST pass its project id or every case will silently miss (the bug the
    adversarial review caught on the first version of this harness).

    The humus knobs (``humus`` / ``humus_kinds`` / ``exclude_humus_from_base``)
    are threaded to ``memory.retrieve_with_meta`` so the same harness runs the
    A/B configurations of the humus empirical gate (task 4836a6cc); they
    default to the historical behaviour, so the CI gold gate is unaffected."""
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
            project_id=project_id,
            query=case.query,
            operation_id=f"eval-{uuid.uuid4().hex}",
            limit=k,
            humus=humus,
            humus_kinds=humus_kinds,
            exclude_humus_from_base=exclude_humus_from_base,
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


def load_consolidation_cases(path: str | Path) -> list[ConsolidationCase]:
    """Load consolidation cases from a JSONL file for a real-corpus humus A/B
    (task 4836a6cc). One object per line: ``query`` (str), ``atom_blob_ids``
    (non-empty list -- the humus atom(s) that answer it) and ``source_blob_ids``
    (list, possibly empty -- the raw blob(s) the atom derives from, used by the
    fairness filter). The file-driven counterpart to the in-code synthetic set,
    so a real corpus (once its atoms + sources are resolved to blob ids) runs
    through the SAME ``run_humus_ab``."""
    cases: list[ConsolidationCase] = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON ({exc})") from exc
        query = obj.get("query")
        atom_ids = obj.get("atom_blob_ids")
        source_ids = obj.get("source_blob_ids", [])
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"{path}:{lineno}: missing/blank 'query'")
        if not isinstance(atom_ids, list) or not atom_ids:
            raise ValueError(f"{path}:{lineno}: 'atom_blob_ids' must be a non-empty list")
        if not isinstance(source_ids, list):
            raise ValueError(f"{path}:{lineno}: 'source_blob_ids' must be a list")
        try:
            atom = frozenset(uuid.UUID(str(i)) for i in atom_ids)
            source = frozenset(uuid.UUID(str(i)) for i in source_ids)
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: bad blob id ({exc})") from exc
        cases.append(ConsolidationCase(query=query, atom_expected=atom, source_expected=source))
    return cases


async def run_eval_from_file(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    path: str | Path,
    k: int = 10,
    project_id: uuid.UUID | None = None,
) -> EvalReport:
    """Convenience: load the gold cases from ``path`` and run them through
    :func:`run_eval` against the corpus ALREADY ingested under ``org_id``
    (the ingestion of an external bench corpus is the caller's job; this only
    measures). Keeps the synthetic CI gate and the external-bench run on one
    measurement path. ``project_id`` as in :func:`run_eval` (None = blobs
    without a project, NOT "no filter")."""
    return await run_eval(
        session,
        org_id=org_id,
        actor_id=actor_id,
        cases=load_cases(path),
        k=k,
        project_id=project_id,
    )


# --- Humus empirical gate (task 4836a6cc / note 9a2adb4a §4) -----------------
# The three configurations that isolate humus's marginal value. Humus atoms are
# ordinary note blobs, so they are retrievable via the base lexical/dense
# branches EVEN WITHOUT the humus branch; the branch only adds a boost + a 30%
# cap. So the informative contrasts are:
#   * A vs C = does HAVING the atoms (branch + base) help/hurt vs no atoms?
#   * A vs B = does the boost/cap MACHINERY earn its keep over atoms-as-notes?
_HUMUS_AB_CONFIGS: tuple[tuple[str, bool, bool], ...] = (
    ("A_on", True, False),  # atoms in base + humus branch (boost) + cap = today
    ("B_branch_off", False, False),  # atoms in base only, no boost/cap
    ("C_atoms_excluded", False, True),  # atoms fully absent
)


@dataclass(frozen=True)
class HumusABCell:
    """One (config, k, case-set) measurement."""

    config: str  # A_on | B_branch_off | C_atoms_excluded
    k: int
    caseset: str  # "raw" | "consolidation"
    recall_at_k: float
    mrr: float
    n_cases: int
    abstained_cases: int


@dataclass(frozen=True)
class HumusABReport:
    """Result matrix of the humus A/B. ``cells`` is every (config, k, case-set)
    measurement; ``fair_consolidation`` are the consolidation queries answerable
    ONLY via a humus atom (no raw blob answers them at ``max(ks)`` under
    Config C), and ``dropped_unfair`` those a raw blob already answers (the
    fairness filter -- a dropped consolidation case is itself a finding: its
    atom added no recall a raw note did not)."""

    ks: tuple[int, ...]
    cells: tuple[HumusABCell, ...]
    n_raw: int
    n_consolidation_input: int
    fair_consolidation: tuple[str, ...]
    dropped_unfair: tuple[str, ...]

    def cell(self, config: str, caseset: str, k: int) -> HumusABCell | None:
        return next(
            (c for c in self.cells if c.config == config and c.caseset == caseset and c.k == k),
            None,
        )

    def render(self) -> str:
        lines = [
            f"Humus A/B  ks={self.ks}  raw_cases={self.n_raw}  "
            f"consolidation: fair={len(self.fair_consolidation)}/"
            f"{self.n_consolidation_input} (dropped_unfair={len(self.dropped_unfair)})",
            f"{'config':<18}{'k':>3}  {'raw_recall':>10} {'raw_mrr':>8}  "
            f"{'con_recall':>10} {'con_mrr':>8}",
        ]
        for config, _h, _e in _HUMUS_AB_CONFIGS:
            for k in self.ks:
                raw = self.cell(config, "raw", k)
                con = self.cell(config, "consolidation", k)
                lines.append(
                    f"{config:<18}{k:>3}  "
                    f"{(raw.recall_at_k if raw else 0.0):>10.3f} "
                    f"{(raw.mrr if raw else 0.0):>8.3f}  "
                    f"{(con.recall_at_k if con else 0.0):>10.3f} "
                    f"{(con.mrr if con else 0.0):>8.3f}"
                )
        if self.dropped_unfair:
            lines.append(f"dropped (raw already answers): {list(self.dropped_unfair)}")
        return "\n".join(lines)


async def run_humus_ab(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    raw_cases: Sequence[GoldCase],
    consolidation_cases: Sequence[ConsolidationCase],
    ks: Sequence[int] = (3, 5, 10),
    project_id: uuid.UUID | None = None,
    humus_kinds: frozenset[str] | None = None,
) -> HumusABReport:
    """Run the humus A/B matrix (Configs A/B/C x ks x {raw, consolidation}) over
    an already-seeded corpus and return the measurements.

    ``raw_cases`` measure DISPLACEMENT (does the humus branch push a raw hit out
    of top-k?); ``consolidation_cases`` measure the genuine ADD (a cross-note
    generalization no single raw blob holds). A consolidation case is kept only
    if it PASSES the fairness filter: with humus atoms fully excluded (Config C)
    at ``max(ks)`` the query must retrieve NONE of its raw SOURCE blobs
    (``source_expected``). A dropped case is itself informative: a raw source
    already answers the query, so its atom added no recall.

    HONEST LIMITS of the fairness filter (adversarial review 2026-07-02): it is
    judged by the SAME retrieval pipeline under test, so its verdict depends on
    the org's semantic floor -- a case can flip fair/unfair across floor values.
    On a real corpus, pre-register the floor (from the measured cosine gap, see
    scripts/diag_retrieval.py) BEFORE looking at outcomes, report a floor
    sweep, and prefer probing "does ANY raw blob answer" (not only the
    enumerated sources). Verdicts obtained under the bag-of-words FakeEmbedder
    do NOT transfer to bge-m3 (a paraphrase model can retrieve sources that
    share no token with the query).

    ``project_id`` as in :func:`run_eval` (None = blobs WITHOUT a project, not
    "no filter": a project-scoped corpus must pass its id). ``humus_kinds``
    restricts the humus BRANCH only (base branches unaffected): branch
    attribution, not per-kind atom presence."""
    ks_t = tuple(sorted({int(k) for k in ks}))
    kmax = max(ks_t)
    # Fairness filter (self-validating): probe each query with humus atoms fully
    # absent (Config C), keyed to the raw SOURCE blobs. If a source is retrieved
    # the query is already answered by a raw note -> drop; else the atom is a
    # genuine cross-note add -> keep (measured below, keyed to the atom).
    fair_cases: list[GoldCase] = []
    fair_q: list[str] = []
    dropped: list[str] = []
    for cc in consolidation_cases:
        probe = await run_eval(
            session,
            org_id=org_id,
            actor_id=actor_id,
            cases=[GoldCase(query=cc.query, expected=cc.source_expected)],
            k=kmax,
            project_id=project_id,
            humus=False,
            exclude_humus_from_base=True,
        )
        if probe.cases[0].rank is None:
            fair_cases.append(GoldCase(query=cc.query, expected=cc.atom_expected))
            fair_q.append(cc.query)
        else:
            dropped.append(cc.query)

    cells: list[HumusABCell] = []
    for config, humus_on, exclude_base in _HUMUS_AB_CONFIGS:
        for k in ks_t:
            raw_rep = await run_eval(
                session,
                org_id=org_id,
                actor_id=actor_id,
                cases=raw_cases,
                k=k,
                project_id=project_id,
                humus=humus_on,
                humus_kinds=humus_kinds,
                exclude_humus_from_base=exclude_base,
            )
            cells.append(
                HumusABCell(
                    config=config,
                    k=k,
                    caseset="raw",
                    recall_at_k=raw_rep.recall_at_k,
                    mrr=raw_rep.mrr,
                    n_cases=raw_rep.n_cases,
                    abstained_cases=raw_rep.abstained_cases,
                )
            )
            con_rep = await run_eval(
                session,
                org_id=org_id,
                actor_id=actor_id,
                cases=fair_cases,
                k=k,
                project_id=project_id,
                humus=humus_on,
                humus_kinds=humus_kinds,
                exclude_humus_from_base=exclude_base,
            )
            cells.append(
                HumusABCell(
                    config=config,
                    k=k,
                    caseset="consolidation",
                    recall_at_k=con_rep.recall_at_k,
                    mrr=con_rep.mrr,
                    n_cases=con_rep.n_cases,
                    abstained_cases=con_rep.abstained_cases,
                )
            )
    return HumusABReport(
        ks=ks_t,
        cells=tuple(cells),
        n_raw=len(raw_cases),
        n_consolidation_input=len(consolidation_cases),
        fair_consolidation=tuple(fair_q),
        dropped_unfair=tuple(dropped),
    )


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
    project_id: uuid.UUID | None = None,
) -> ForgettingReport:
    """Measure verified forgetting: recall over ``cases``, then erase one
    subject's provenance via ``memory.gdpr_erase``, then recall again. A
    compliant memory drops recall for the erased subject (the blobs are GONE,
    not just hidden) -- the metric where self-hostable, provenance-auditable
    memory beats hosted competitors that never score it. Reuses ``run_eval``
    (``project_id`` as there: None = blobs without a project)."""
    before = await run_eval(
        session, org_id=org_id, actor_id=actor_id, cases=cases, k=k, project_id=project_id
    )
    erased = await memory.gdpr_erase(
        session, org_id=org_id, actor_id=actor_id, source_kind=source_kind, source_id=source_id
    )
    after = await run_eval(
        session, org_id=org_id, actor_id=actor_id, cases=cases, k=k, project_id=project_id
    )
    return ForgettingReport(
        erased=erased, recall_before=before.recall_at_k, recall_after=after.recall_at_k
    )
