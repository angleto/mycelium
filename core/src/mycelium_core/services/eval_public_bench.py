"""Public-benchmark adapters: LongMemEval / LOCOMO -> the offline eval gate
(task cc4653bd, Track B3 / WS-E1 follow-up).

The goal is numbers COMPARABLE to the public memory-system claims
(Mem0/Zep/Cognee report LOCOMO / LongMemEval): parse the published dataset
shapes into (a) ingestion units written through the real ``memory.write_blob``
(provenance ``(source_kind, source_id)`` per session/turn, session dates in a
header line so temporal questions are answerable from the stored text) and
(b) gold cases scored by the SAME ``eval_offline.run_eval`` used by the CI
gate -- one measurement path, no bespoke scorer.

Dataset shapes (verified 2026-07-03 against the source repos):

* LongMemEval (github.com/xiaowu0162/LongMemEval; data on huggingface
  ``xiaowu0162/longmemeval-cleaned``): a JSON list of instances, each with
  ``question_id`` (suffix ``_abs`` = abstention), ``question_type``
  (single-session-user / single-session-assistant / single-session-preference
  / temporal-reasoning / knowledge-update / multi-session), ``question``,
  ``answer``, ``question_date``, ``haystack_session_ids``, ``haystack_dates``,
  ``haystack_sessions`` (list of sessions; a session is a list of turns
  ``{"role": ..., "content": ..., "has_answer"?: true}``) and
  ``answer_session_ids``. Each instance carries its OWN haystack, so the
  runner ingests each instance into a throwaway org.
* LOCOMO (github.com/snap-research/locomo, ``data/locomo10.json``): a JSON
  list of samples, each with ``sample_id``, ``conversation`` (``speaker_a``,
  ``speaker_b``, ``session_<n>`` = list of turns ``{"speaker", "dia_id",
  "text", "img_url"?, "blip_caption"?}``, ``session_<n>_date_time``) and
  ``qa`` (``{"question", "answer" | "adversarial_answer", "evidence":
  [dia_id...], "category": 1..5}``). Categories: 1 multi-hop, 2 temporal,
  3 open-domain, 4 single-hop, 5 adversarial (unanswerable -> abstention).

What this measures -- and does not: RETRIEVAL recall@k / MRR over stored
units plus an abstention rate, per question category. It does NOT run an
answer-generation judge, so the numbers are comparable to reported
"retrieval/search accuracy" columns, not to end-to-end QA scores. Abstention
is scored as correct when the pipeline returns no hits or honestly abstains
(``meta.abstained``) -- with no grader floor configured the expected baseline
is ~0, which is exactly the f0d24fdb lever this bench should expose.

Efficiency axis: ``tokens_per_query`` approximates the tokens an agent would
ingest from the served hits, using the deterministic chars/4 heuristic (no
tokenizer dependency; stated wherever reported).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.memory_blob import BlobSource, MemoryBlob
from mycelium_core.models.tag import TagKind
from mycelium_core.services import eval_offline, memory, taxonomy
from mycelium_core.services.eval_offline import GoldCase

SOURCE_KIND_LONGMEMEVAL = "longmemeval"
SOURCE_KIND_LOCOMO = "locomo"

LOCOMO_CATEGORY_LABELS: Mapping[int, str] = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}

# ``operation_id`` / source_id length guards (blob_sources.source_id is
# VARCHAR(255); dataset ids are short but this is third-party data).
_MAX_SOURCE_ID = 255


@dataclass(frozen=True)
class IngestUnit:
    """One retrievable unit to store: a LongMemEval session or a LOCOMO turn."""

    source_kind: str
    source_id: str
    text: str
    date: str | None  # session date verbatim from the dataset (also in text)


@dataclass(frozen=True)
class BenchQuestion:
    qid: str
    query: str
    answer: str
    category: str  # dataset-native label (question_type / category name)
    evidence_source_ids: frozenset[str]
    abstention: bool  # expected behaviour is "nothing to retrieve"


@dataclass(frozen=True)
class BenchInstance:
    """A self-contained corpus + its questions. LongMemEval: one instance per
    dataset entry (its own haystack, ONE question). LOCOMO: one instance per
    sample (one conversation, many questions)."""

    instance_id: str
    units: tuple[IngestUnit, ...]
    questions: tuple[BenchQuestion, ...]


def _require(obj: Mapping[str, Any], key: str, instance: str) -> Any:
    if key not in obj:
        raise ValueError(f"{instance}: missing required field {key!r}")
    return obj[key]


def parse_longmemeval_instance(obj: Mapping[str, Any]) -> BenchInstance:
    """One LongMemEval entry -> one single-question :class:`BenchInstance`.
    A unit is a whole session (the dataset's evidence granularity --
    ``answer_session_ids`` names sessions, not turns), rendered as a
    date-headed transcript so temporal-reasoning questions can be answered
    from the stored text alone."""
    qid = str(_require(obj, "question_id", "longmemeval"))
    where = f"longmemeval:{qid}"
    session_ids = list(_require(obj, "haystack_session_ids", where))
    dates = list(_require(obj, "haystack_dates", where))
    sessions = list(_require(obj, "haystack_sessions", where))
    if not (len(session_ids) == len(dates) == len(sessions)):
        raise ValueError(
            f"{where}: haystack_session_ids/haystack_dates/haystack_sessions "
            f"lengths differ ({len(session_ids)}/{len(dates)}/{len(sessions)})"
        )
    units: list[IngestUnit] = []
    seen: set[str] = set()
    for sid_raw, date, turns in zip(session_ids, dates, sessions, strict=True):
        sid = str(sid_raw)[:_MAX_SOURCE_ID]
        if sid in seen:  # third-party data: never let a dup silently merge
            raise ValueError(f"{where}: duplicate haystack session id {sid!r}")
        seen.add(sid)
        lines = [f"[session date: {date}]"]
        for turn in turns:
            role = str(turn.get("role", "user"))
            content = str(turn.get("content", "")).strip()
            if content:
                lines.append(f"{role}: {content}")
        units.append(
            IngestUnit(
                source_kind=SOURCE_KIND_LONGMEMEVAL,
                source_id=sid,
                text="\n".join(lines),
                date=str(date),
            )
        )
    abstention = qid.endswith("_abs")
    evidence = frozenset(str(s)[:_MAX_SOURCE_ID] for s in (obj.get("answer_session_ids") or []))
    question = BenchQuestion(
        qid=qid,
        query=str(_require(obj, "question", where)),
        answer=str(obj.get("answer", "")),
        category=str(_require(obj, "question_type", where)),
        evidence_source_ids=frozenset() if abstention else evidence,
        abstention=abstention,
    )
    if not abstention and not question.evidence_source_ids:
        raise ValueError(f"{where}: non-abstention question without answer_session_ids")
    return BenchInstance(instance_id=qid, units=tuple(units), questions=(question,))


def parse_locomo_sample(obj: Mapping[str, Any]) -> BenchInstance:
    """One LOCOMO sample -> one multi-question :class:`BenchInstance`. A unit
    is a single TURN (``dia_id`` -- the dataset's own evidence granularity),
    date-headed and speaker-attributed; image turns carry the BLIP caption so
    a caption-dependent question is not unanswerable by construction."""
    sample_id = str(_require(obj, "sample_id", "locomo"))
    where = f"locomo:{sample_id}"
    conv = _require(obj, "conversation", where)
    units: list[IngestUnit] = []
    seen: set[str] = set()
    n = 1
    while f"session_{n}" in conv:
        date = conv.get(f"session_{n}_date_time")
        for turn in conv[f"session_{n}"] or []:
            dia_id = str(_require(turn, "dia_id", where))[:_MAX_SOURCE_ID]
            if dia_id in seen:
                raise ValueError(f"{where}: duplicate dia_id {dia_id!r}")
            seen.add(dia_id)
            speaker = str(turn.get("speaker", "?"))
            text = str(turn.get("text", "")).strip()
            caption = str(turn.get("blip_caption") or "").strip()
            if caption:
                text = f"{text} (shares a photo: {caption})".strip()
            units.append(
                IngestUnit(
                    source_kind=SOURCE_KIND_LOCOMO,
                    source_id=dia_id,
                    text=f"[{date}] {speaker}: {text}",
                    date=str(date) if date is not None else None,
                )
            )
        n += 1
    if not units:
        raise ValueError(f"{where}: conversation has no session_<n> turns")
    questions: list[BenchQuestion] = []
    for i, qa in enumerate(obj.get("qa") or []):
        category_code = int(_require(qa, "category", where))
        category = LOCOMO_CATEGORY_LABELS.get(category_code, f"category-{category_code}")
        abstention = category_code == 5
        evidence = frozenset(
            str(e)[:_MAX_SOURCE_ID] for e in (qa.get("evidence") or []) if str(e) in seen
        )
        answer = qa.get("answer", qa.get("adversarial_answer", ""))
        questions.append(
            BenchQuestion(
                qid=f"{sample_id}:{i}",
                query=str(_require(qa, "question", where)),
                answer=str(answer),
                category=category,
                evidence_source_ids=frozenset() if abstention else evidence,
                abstention=abstention,
            )
        )
    return BenchInstance(instance_id=sample_id, units=tuple(units), questions=tuple(questions))


async def ingest_instance(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    instance: BenchInstance,
    project_id: uuid.UUID | None = None,
) -> None:
    """Write every unit through the real ``memory.write_blob`` (provenance =
    ``(source_kind, source_id)``, one generic ``date:<session date>`` tag per
    distinct session date for as-of style filtering). Long sessions chunk into
    several blobs; :func:`resolve_evidence` picks up every chunk via
    ``blob_sources``."""
    date_tags: dict[str, uuid.UUID] = {}
    for unit in instance.units:
        tag_ids: list[uuid.UUID] = []
        if unit.date:
            if unit.date not in date_tags:
                tag = await taxonomy.create_tag(
                    session,
                    org_id=org_id,
                    actor_id=actor_id,
                    kind=TagKind.generic,
                    name=f"date:{unit.date}"[:120],
                )
                date_tags[unit.date] = tag.id
            tag_ids.append(date_tags[unit.date])
        await memory.write_blob(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=project_id,
            text_body=unit.text,
            operation_id=f"bench-{uuid.uuid4().hex}",
            namespace="note",
            sources=[(unit.source_kind, unit.source_id)],
            tag_ids=tag_ids,
        )


async def resolve_evidence(
    session: AsyncSession, *, org_id: uuid.UUID, instance: BenchInstance
) -> dict[str, frozenset[uuid.UUID]]:
    """``source_id -> {blob ids}`` for the instance's units (all chunks)."""
    kind = instance.units[0].source_kind if instance.units else SOURCE_KIND_LONGMEMEVAL
    rows = (
        await session.execute(
            select(BlobSource.source_id, BlobSource.blob_id).where(
                BlobSource.org_id == org_id, BlobSource.source_kind == kind
            )
        )
    ).all()
    out: dict[str, set[uuid.UUID]] = {}
    for source_id, blob_id in rows:
        out.setdefault(source_id, set()).add(blob_id)
    return {sid: frozenset(ids) for sid, ids in out.items()}


@dataclass(frozen=True)
class QuestionResult:
    qid: str
    category: str
    abstention: bool
    rank: int | None  # scored questions: 1-based rank of first evidence hit
    abstain_correct: bool | None  # abstention questions: no hits or honest abstain
    served_tokens: int  # chars/4 over the texts of the served hits


@dataclass(frozen=True)
class InstanceScore:
    instance_id: str
    results: tuple[QuestionResult, ...]
    skipped_no_evidence: tuple[str, ...]  # qids whose evidence resolved to no blob


async def _served_tokens(session: AsyncSession, hit_ids: Sequence[uuid.UUID]) -> int:
    if not hit_ids:
        return 0
    total_chars = (
        await session.execute(
            select(
                func.coalesce(func.sum(func.length(func.coalesce(MemoryBlob.text, ""))), 0)
            ).where(MemoryBlob.id.in_(list(hit_ids)))
        )
    ).scalar_one()
    return int(total_chars) // 4


async def score_instance(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    instance: BenchInstance,
    k: int = 10,
    project_id: uuid.UUID | None = None,
    limit_questions: int | None = None,
    grader_min_rrf: float | None = None,
) -> InstanceScore:
    """Score one already-ingested instance: scored questions go through
    ``eval_offline.run_eval`` (the CI gate's path); abstention questions call
    ``memory.retrieve_with_meta`` directly and are correct when nothing is
    served or the pipeline honestly abstains."""
    resolved = await resolve_evidence(session, org_id=org_id, instance=instance)
    questions = list(instance.questions)
    if limit_questions is not None:
        questions = questions[:limit_questions]

    results: list[QuestionResult] = []
    skipped: list[str] = []
    scored: list[tuple[BenchQuestion, GoldCase]] = []
    for q in questions:
        if q.abstention:
            hits, meta = await memory.retrieve_with_meta(
                session,
                org_id=org_id,
                actor_id=actor_id,
                project_id=project_id,
                query=q.query,
                operation_id=f"bench-{uuid.uuid4().hex}",
                limit=k,
                grader_min_rrf=grader_min_rrf,
                # Bench traffic is measurement: like run_eval (which covers
                # the scored questions), it must not leave retrieval traces
                # or the bench would forge search demand (Fase 0, 561c6aca).
                probe=True,
            )
            results.append(
                QuestionResult(
                    qid=q.qid,
                    category=q.category,
                    abstention=True,
                    rank=None,
                    abstain_correct=(not hits) or meta.abstained,
                    served_tokens=await _served_tokens(session, [h.blob.id for h in hits]),
                )
            )
            continue
        expected = frozenset().union(
            *(resolved.get(sid, frozenset()) for sid in q.evidence_source_ids)
        )
        if not expected:
            # Evidence ids that resolve to no stored blob (dataset refers to a
            # unit we never stored) would score an artificial miss -- report,
            # never silently count.
            skipped.append(q.qid)
            continue
        scored.append((q, GoldCase(query=q.query, expected=expected)))

    if scored:
        report = await eval_offline.run_eval(
            session,
            org_id=org_id,
            actor_id=actor_id,
            cases=[case for _q, case in scored],
            k=k,
            project_id=project_id,
            grader_min_rrf=grader_min_rrf,
        )
        for (q, _case), case_result in zip(scored, report.cases, strict=True):
            results.append(
                QuestionResult(
                    qid=q.qid,
                    category=q.category,
                    abstention=False,
                    rank=case_result.rank,
                    abstain_correct=None,
                    served_tokens=await _served_tokens(session, case_result.hit_ids),
                )
            )
    return InstanceScore(
        instance_id=instance.instance_id,
        results=tuple(results),
        skipped_no_evidence=tuple(skipped),
    )


@dataclass(frozen=True)
class CategoryScore:
    category: str
    n: int
    recall_at_k: float
    mrr: float


@dataclass(frozen=True)
class BenchReport:
    """Aggregate over every scored instance. ``embedder_models`` is the honesty
    label: the distinct ``model_id`` values the stored corpus carries --
    ``{'none'}`` means keyword-only retrieval (no dense tier), and any number
    published from this report must say so."""

    dataset: str
    k: int
    n_instances: int
    n_questions: int
    n_scored: int
    n_abstention: int
    n_skipped_no_evidence: int
    recall_at_k: float
    mrr: float
    abstention_correct_rate: float
    tokens_per_query: float  # mean chars/4 of served hits, all questions
    per_category: tuple[CategoryScore, ...]
    embedder_models: tuple[str, ...]
    grader_min_rrf: float | None = None

    def render(self) -> str:
        lines = [
            f"{self.dataset}  k={self.k}  instances={self.n_instances}  "
            f"questions={self.n_questions} (scored={self.n_scored}, "
            f"abstention={self.n_abstention}, skipped_no_evidence="
            f"{self.n_skipped_no_evidence})",
            f"embedder_models={list(self.embedder_models)}  "
            f"tokens/query (chars/4)={self.tokens_per_query:.0f}  "
            f"grader_min_rrf={self.grader_min_rrf}",
            f"overall  recall@{self.k}={self.recall_at_k:.3f}  MRR={self.mrr:.3f}  "
            f"abstention_correct={self.abstention_correct_rate:.3f}",
            f"{'category':<28}{'n':>5}  {'recall':>7} {'mrr':>7}",
        ]
        for c in self.per_category:
            lines.append(f"{c.category:<28}{c.n:>5}  {c.recall_at_k:>7.3f} {c.mrr:>7.3f}")
        return "\n".join(lines)


async def corpus_embedder_models(session: AsyncSession, *, org_id: uuid.UUID) -> tuple[str, ...]:
    rows = (
        await session.execute(
            select(MemoryBlob.model_id).where(MemoryBlob.org_id == org_id).distinct()
        )
    ).scalars()
    return tuple(sorted({m if m is not None else "null" for m in rows}))


def aggregate(
    dataset: str,
    k: int,
    scores: Sequence[InstanceScore],
    embedder_models: Sequence[str],
    grader_min_rrf: float | None = None,
) -> BenchReport:
    all_results = [r for s in scores for r in s.results]
    scored = [r for r in all_results if not r.abstention]
    abst = [r for r in all_results if r.abstention]
    hits = [r for r in scored if r.rank is not None]
    per_cat: dict[str, list[QuestionResult]] = {}
    for r in scored:
        per_cat.setdefault(r.category, []).append(r)
    for r in abst:
        per_cat.setdefault(r.category, []).append(r)
    cats: list[CategoryScore] = []
    for cat in sorted(per_cat):
        rs = per_cat[cat]
        srs = [r for r in rs if not r.abstention]
        if srs:
            cats.append(
                CategoryScore(
                    category=cat,
                    n=len(srs),
                    recall_at_k=sum(1 for r in srs if r.rank is not None) / len(srs),
                    mrr=sum(1.0 / r.rank for r in srs if r.rank is not None) / len(srs),
                )
            )
        else:  # abstention-only category: recall column reports correctness
            ars = [r for r in rs if r.abstention]
            correct = sum(1 for r in ars if r.abstain_correct)
            cats.append(
                CategoryScore(
                    category=f"{cat} (abstain)",
                    n=len(ars),
                    recall_at_k=correct / len(ars) if ars else 0.0,
                    mrr=0.0,
                )
            )
    return BenchReport(
        dataset=dataset,
        k=k,
        n_instances=len(scores),
        n_questions=len(all_results) + sum(len(s.skipped_no_evidence) for s in scores),
        n_scored=len(scored),
        n_abstention=len(abst),
        n_skipped_no_evidence=sum(len(s.skipped_no_evidence) for s in scores),
        recall_at_k=(len(hits) / len(scored)) if scored else 0.0,
        mrr=(sum(1.0 / r.rank for r in hits if r.rank) / len(scored)) if scored else 0.0,
        abstention_correct_rate=(
            sum(1 for r in abst if r.abstain_correct) / len(abst) if abst else 0.0
        ),
        tokens_per_query=(
            sum(r.served_tokens for r in all_results) / len(all_results) if all_results else 0.0
        ),
        per_category=tuple(cats),
        embedder_models=tuple(embedder_models),
        grader_min_rrf=grader_min_rrf,
    )
