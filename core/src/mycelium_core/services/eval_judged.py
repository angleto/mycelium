"""Judged end-to-end track for the WS-EVAL protocol (task d7c0693e, nota
WS-EVAL §7): an ANSWERER model answers each query from the served hits
ONLY, a JUDGE model grades the answer against the gold; the article
publishes the (retrieval, e2e) pair per category -- the gap between the
two IS a result, not an embarrassment to hide.

Both models are plain ``LLMProvider``s (ai_providers protocol), so CI
tests inject deterministic fakes and the real path goes through the
metered ``resolve_llm`` seam (never exercised in CI). The prompts are
module constants: they are part of the published protocol and must not
be tuned per-run.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.ai_providers import LLMProvider
from mycelium_core.services.llm_resolver import resolve_llm

Verdict = Literal["correct", "partially", "wrong", "abstained"]

# The exact abstention sentinel the answerer is instructed to emit. The
# check is on the NORMALIZED prefix so a trailing period or lowercase
# variant still counts as an abstention, never as a wrong answer.
ABSTAIN_SENTINEL = "NOT IN MEMORY"

ANSWERER_SYSTEM_PROMPT = (
    "You answer questions using ONLY the memory units provided. Do not use "
    "any outside knowledge. If the memory units do not contain the answer, "
    f'reply exactly "{ABSTAIN_SENTINEL}". Otherwise answer concisely with '
    "the facts from the units."
)

JUDGE_SYSTEM_PROMPT = (
    "You are grading an answer against a gold reference. Reply with exactly "
    "one word:\n"
    "CORRECT - the answer states the gold fact (wording may differ)\n"
    "PARTIAL - the answer states part of the gold fact or hedges it\n"
    "WRONG - the answer contradicts or misses the gold fact\n"
    "Grade ONLY factual agreement with the gold, not style or length."
)


@dataclass(frozen=True)
class JudgedInput:
    """One query of the judged track. ``gold_answer`` is None for the
    impossible queries, where the correct behaviour is abstention."""

    qid: str
    category: str
    query: str
    gold_answer: str | None
    hit_texts: tuple[str, ...]
    retrieval_hit: (
        bool  # gold unit was in the served top-k (or query impossible and nothing served)
    )


@dataclass(frozen=True)
class JudgedCase:
    qid: str
    category: str
    retrieval_hit: bool
    answer_text: str
    verdict: Verdict


@dataclass(frozen=True)
class JudgedCategoryRow:
    category: str
    n: int
    retrieval_rate: float  # fraction with retrieval_hit
    e2e_correct: float  # strict: verdict == correct (abstain counts only on impossibles)
    e2e_partial: float
    gap: float  # retrieval_rate - e2e_correct


@dataclass(frozen=True)
class JudgedReport:
    cases: tuple[JudgedCase, ...]
    per_category: tuple[JudgedCategoryRow, ...]

    def render(self) -> str:
        lines = [f"{'category':<28}{'n':>5}  {'retrieval':>9} {'e2e':>7} {'partial':>8} {'gap':>7}"]
        for r in self.per_category:
            lines.append(
                f"{r.category:<28}{r.n:>5}  {r.retrieval_rate:>9.3f} {r.e2e_correct:>7.3f} "
                f"{r.e2e_partial:>8.3f} {r.gap:>7.3f}"
            )
        return "\n".join(lines)


def _is_abstention(text: str) -> bool:
    return text.strip().strip(".").upper().startswith(ABSTAIN_SENTINEL)


async def answer_from_hits(answerer: LLMProvider, *, query: str, hit_texts: Sequence[str]) -> str:
    """Ask the answerer to reply from the served units only. An empty
    hit list short-circuits to the abstention sentinel (there is nothing
    to answer from; calling the model would only invite hallucination)."""
    if not hit_texts:
        return ABSTAIN_SENTINEL
    units = "\n\n".join(f"[unit {i + 1}]\n{t}" for i, t in enumerate(hit_texts))
    result = await answerer.complete(
        system=ANSWERER_SYSTEM_PROMPT,
        messages=[("user", f"Memory units:\n{units}\n\nQuestion: {query}")],
    )
    return result.text.strip()


async def judge_answer(
    judge: LLMProvider, *, query: str, gold_answer: str, answer_text: str
) -> Verdict:
    """Grade a NON-abstained answer against the gold. The judge's reply
    is mapped on its first word; an unparseable verdict counts as
    ``wrong`` (fail-closed: a broken judge must not inflate scores)."""
    result = await judge.complete(
        system=JUDGE_SYSTEM_PROMPT,
        messages=[
            (
                "user",
                f"Question: {query}\nGold: {gold_answer}\nAnswer: {answer_text}\nVerdict:",
            )
        ],
    )
    head = result.text.strip().split()[0].upper() if result.text.strip() else ""
    if head == "CORRECT":
        return "correct"
    if head == "PARTIAL":
        return "partially"
    return "wrong"


async def run_judged_track(
    inputs: Sequence[JudgedInput],
    *,
    answerer: LLMProvider,
    judge: LLMProvider,
) -> JudgedReport:
    """The e2e track: answer every query from its served hits, grade the
    answerable ones, and aggregate per category.

    Verdict rules: an abstention is ``abstained``; on an IMPOSSIBLE query
    (gold_answer None) abstention is the CORRECT outcome and a
    non-abstained answer is ``wrong`` by definition (whatever it says, it
    answered a question whose answer is not in memory)."""
    cases: list[JudgedCase] = []
    for item in inputs:
        answer = await answer_from_hits(answerer, query=item.query, hit_texts=item.hit_texts)
        verdict: Verdict
        if _is_abstention(answer):
            verdict = "correct" if item.gold_answer is None else "abstained"
        elif item.gold_answer is None:
            verdict = "wrong"
        else:
            verdict = await judge_answer(
                judge, query=item.query, gold_answer=item.gold_answer, answer_text=answer
            )
        cases.append(
            JudgedCase(
                qid=item.qid,
                category=item.category,
                retrieval_hit=item.retrieval_hit,
                answer_text=answer,
                verdict=verdict,
            )
        )
    by_cat: dict[str, list[JudgedCase]] = {}
    for c in cases:
        by_cat.setdefault(c.category, []).append(c)
    rows: list[JudgedCategoryRow] = []
    for cat in sorted(by_cat):
        cs = by_cat[cat]
        retrieval = sum(1 for c in cs if c.retrieval_hit) / len(cs)
        correct = sum(1 for c in cs if c.verdict == "correct") / len(cs)
        partial = sum(1 for c in cs if c.verdict == "partially") / len(cs)
        rows.append(
            JudgedCategoryRow(
                category=cat,
                n=len(cs),
                retrieval_rate=retrieval,
                e2e_correct=correct,
                e2e_partial=partial,
                gap=retrieval - correct,
            )
        )
    return JudgedReport(cases=tuple(cases), per_category=tuple(rows))


async def resolve_judged_llms(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    operation_id: str,
) -> tuple[LLMProvider, LLMProvider]:
    """The real (non-CI) path: answerer and judge through the metered
    ``resolve_llm`` seam with distinct op labels, so e2e runs are billed
    and attributable like every other LLM call. The protocol requires
    DECLARING which models these resolve to in the published config."""
    answerer = await resolve_llm(
        session,
        org_id,
        actor_id=actor_id,
        operation_id=f"{operation_id}-answer",
        op="eval_e2e_answer",
    )
    judge = await resolve_llm(
        session,
        org_id,
        actor_id=actor_id,
        operation_id=f"{operation_id}-judge",
        op="eval_e2e_judge",
    )
    return answerer, judge
