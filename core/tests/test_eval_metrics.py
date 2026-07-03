"""WS-EVAL extended retrieval metrics (task d7c0693e): nDCG against
hand-computed cases, abstention confusion + full curve (with the
monotonicity the curve must satisfy), freshness incl. the anti-recency
reading, context ratio with the fixed denominator."""

from __future__ import annotations

import uuid

import pytest

from mycelium_core.services.eval_metrics import (
    abstention_curve,
    abstention_metrics,
    context_token_ratio,
    freshness_ok,
    ndcg_at_k,
    tokens_chars4,
)

A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_ndcg_hand_checked() -> None:
    # Gold first -> 1.0.
    assert ndcg_at_k([A, B], {A}, k=10) == pytest.approx(1.0)
    # Gold at rank 2 of one relevant: 1/log2(3) = 0.63093.
    assert ndcg_at_k([B, A], {A}, k=10) == pytest.approx(0.63093, abs=1e-5)
    # Two golds at ranks 1 and 3: DCG = 1 + 1/log2(4) = 1.5;
    # IDCG = 1 + 1/log2(3) = 1.63093 -> 0.91972.
    assert ndcg_at_k([A, C, B], {A, B}, k=3) == pytest.approx(0.91972, abs=1e-5)
    # Nothing relevant in top-k -> 0.
    assert ndcg_at_k([C], {A}, k=1) == 0.0
    with pytest.raises(ValueError):
        ndcg_at_k([A], set(), k=5)
    with pytest.raises(ValueError):
        ndcg_at_k([A], {A}, k=0)


def test_abstention_metrics_confusion() -> None:
    pairs = (
        [(True, True)] * 2  # TP: impossible, abstained
        + [(True, False)] * 1  # FN
        + [(False, True)] * 1  # FP
        + [(False, False)] * 6  # TN
    )
    m = abstention_metrics(pairs)
    assert (m.tp, m.fp, m.fn, m.tn) == (2, 1, 1, 6)
    assert m.precision == pytest.approx(2 / 3)
    assert m.recall == pytest.approx(2 / 3)
    assert m.f1 == pytest.approx(2 / 3)
    assert m.prevalence == pytest.approx(0.3)
    with pytest.raises(ValueError):
        abstention_metrics([])


def test_abstention_curve_monotone_recall() -> None:
    entries = [(0.10, True), (0.50, False), (0.05, True), (0.30, False), (0.20, True)]
    curve = abstention_curve(entries)
    # Thresholds sweep the observed scores plus the abstain-everything end.
    assert curve[0].threshold == 0.05 and curve[-1].threshold == pytest.approx(1.5)
    recalls = [p.recall for p in curve]
    assert recalls == sorted(recalls)  # abstaining more never loses an impossible
    assert curve[-1].recall == 1.0  # abstain everything -> full recall
    # At t just above every impossible score but below the answerables',
    # the rule is perfect: precision = recall = 1.
    perfect = [p for p in curve if p.threshold == pytest.approx(0.30)]
    assert perfect and perfect[0].precision == 1.0 and perfect[0].recall == 1.0


def test_freshness_ok_orderings() -> None:
    cur, stale = A, B
    assert freshness_ok([cur, stale], current_id=cur, stale_id=stale, k=10)
    assert not freshness_ok([stale, cur], current_id=cur, stale_id=stale, k=10)
    # Stale absent counts as outranked; current absent fails.
    assert freshness_ok([cur, C], current_id=cur, stale_id=stale, k=10)
    assert not freshness_ok([C, stale], current_id=cur, stale_id=stale, k=10)
    # The k cut applies to both.
    assert not freshness_ok([C, cur], current_id=cur, stale_id=stale, k=1)


def test_context_token_ratio_fixed_denominator() -> None:
    assert context_token_ratio(200, 100) == pytest.approx(2.0)
    assert context_token_ratio(50, 100) == pytest.approx(0.5)
    assert tokens_chars4("abcdefgh") == 2
    with pytest.raises(ValueError):
        context_token_ratio(10, 0)
    with pytest.raises(ValueError):
        context_token_ratio(-1, 10)
