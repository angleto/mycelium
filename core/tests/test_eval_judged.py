"""WS-EVAL judged e2e track (task d7c0693e) with deterministic fakes:
the real-LLM path is config-driven and never runs in CI. Covers the
abstention sentinel, the impossible-query verdict rules (judge NOT
consulted), the empty-hits short-circuit and the per-category report."""

from __future__ import annotations

from collections.abc import Sequence

from mycelium_core.ai_providers import LLMResult
from mycelium_core.services.eval_judged import (
    ABSTAIN_SENTINEL,
    JudgedInput,
    run_judged_track,
)


class _FakeAnswerer:
    """Answers by keyword lookup on the question line; counts calls so
    the short-circuit paths are observable."""

    def __init__(self, by_question: dict[str, str]) -> None:
        self._by_question = by_question
        self.calls = 0

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        self.calls += 1
        question = messages[-1][1].rsplit("Question: ", 1)[-1]
        text = self._by_question.get(question, ABSTAIN_SENTINEL)
        return LLMResult(text=text, tokens_in=10, tokens_out=5, model_id="fake-answerer")


class _FakeJudge:
    """CORRECT iff the gold string appears verbatim in the answer;
    'circa' marks a partial. Counts calls to pin when judging is skipped."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        self.calls += 1
        body = messages[-1][1]
        gold = body.split("Gold: ", 1)[1].split("\n", 1)[0]
        answer = body.split("Answer: ", 1)[1].rsplit("\nVerdict:", 1)[0]
        if gold in answer:
            verdict = "CORRECT"
        elif "circa" in answer:
            verdict = "PARTIAL"
        else:
            verdict = "WRONG"
        return LLMResult(text=verdict, tokens_in=20, tokens_out=1, model_id="fake-judge")


async def test_judged_track_verdicts_and_report() -> None:
    answerer = _FakeAnswerer(
        {
            "quando scade il progetto?": "il progetto scade il 12 marzo",
            "quanto costa la licenza?": "circa mille euro",
            "chi firma il contratto?": ABSTAIN_SENTINEL,
            "qual e il codice fiscale del cliente marziano?": "ABCDEF12G34H567I",
        }
    )
    judge = _FakeJudge()
    inputs = [
        JudgedInput(
            qid="q1",
            category="single-fact",
            query="quando scade il progetto?",
            gold_answer="12 marzo",
            hit_texts=("nota: il progetto scade il 12 marzo",),
            retrieval_hit=True,
        ),
        JudgedInput(
            qid="q2",
            category="single-fact",
            query="quanto costa la licenza?",
            gold_answer="1000 euro",
            hit_texts=("nota: licenza 1000 euro",),
            retrieval_hit=True,
        ),
        # Answerable but the answerer abstains -> 'abstained', judge skipped.
        JudgedInput(
            qid="q3",
            category="single-fact",
            query="chi firma il contratto?",
            gold_answer="Mario Rossi",
            hit_texts=("nota: firma Mario Rossi",),
            retrieval_hit=True,
        ),
        # Impossible + abstention = the CORRECT behaviour.
        JudgedInput(
            qid="q4",
            category="impossible",
            query="qual e l'iban del fornitore mai citato?",
            gold_answer=None,
            hit_texts=("nota qualunque",),
            retrieval_hit=False,
        ),
        # Impossible but answered = wrong BY DEFINITION, judge skipped.
        JudgedInput(
            qid="q5",
            category="impossible",
            query="qual e il codice fiscale del cliente marziano?",
            gold_answer=None,
            hit_texts=("nota qualunque",),
            retrieval_hit=False,
        ),
    ]
    report = await run_judged_track(inputs, answerer=answerer, judge=judge)
    verdicts = {c.qid: c.verdict for c in report.cases}
    assert verdicts == {
        "q1": "correct",
        "q2": "partially",
        "q3": "abstained",
        "q4": "correct",
        "q5": "wrong",
    }
    # The judge grades ONLY q1 and q2: abstentions and impossibles skip it.
    assert judge.calls == 2
    rows = {r.category: r for r in report.per_category}
    sf = rows["single-fact"]
    assert sf.n == 3
    assert sf.retrieval_rate == 1.0
    assert sf.e2e_correct == 1 / 3
    assert sf.e2e_partial == 1 / 3
    assert sf.gap == 1.0 - 1 / 3
    imp = rows["impossible"]
    assert imp.n == 2 and imp.e2e_correct == 0.5
    assert "single-fact" in report.render()


async def test_empty_hits_short_circuit() -> None:
    answerer = _FakeAnswerer({})
    judge = _FakeJudge()
    inputs = [
        JudgedInput(
            qid="q1",
            category="single-fact",
            query="qualsiasi",
            gold_answer="x",
            hit_texts=(),
            retrieval_hit=False,
        )
    ]
    report = await run_judged_track(inputs, answerer=answerer, judge=judge)
    # No hits: the sentinel is emitted without calling either model.
    assert answerer.calls == 0 and judge.calls == 0
    assert report.cases[0].verdict == "abstained"
