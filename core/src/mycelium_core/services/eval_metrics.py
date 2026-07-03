"""Extended retrieval metrics for the WS-EVAL protocol (task d7c0693e,
nota WS-EVAL §7): pure functions over hit lists and gold ids, ADDITIVE to
the existing ``eval_offline`` outputs (whose shapes are pinned by CI and
must not change).

Everything here is deterministic and DB-free; the ingest/retrieve side
stays in ``eval_offline`` / ``eval_public_bench``.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

# The declared tokenizer of the efficiency axis: chars/4 (nota WS-EVAL
# §7 / T4 -- the denominator formula is published, not tunable).
CHARS_PER_TOKEN = 4


def tokens_chars4(text: str) -> int:
    """The protocol's deterministic token count: len(text) // 4."""
    return len(text) // CHARS_PER_TOKEN


def ndcg_at_k(
    hit_ids: Sequence[uuid.UUID],
    gold_ids: frozenset[uuid.UUID] | set[uuid.UUID],
    *,
    k: int,
) -> float:
    """Binary-relevance nDCG@k:

        DCG  = sum over positions i (1-based) of rel_i / log2(i + 1)
        IDCG = the DCG of the ideal ranking (all |gold| relevant units
               first, capped at k)
        nDCG = DCG / IDCG

    A query with no gold raises (the caller filtered impossibles out).
    """
    if k <= 0:
        raise ValueError("ndcg_at_k: k must be positive")
    if not gold_ids:
        raise ValueError("ndcg_at_k: empty gold set (impossible query?)")
    dcg = 0.0
    for i, hid in enumerate(hit_ids[:k], start=1):
        if hid in gold_ids:
            dcg += 1.0 / math.log2(i + 1)
    ideal_hits = min(len(gold_ids), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg


@dataclass(frozen=True)
class AbstentionMetrics:
    """Confusion of the abstain decision against the impossible label:
    abstaining on an impossible query is the true positive (the honest
    behaviour the contract demands, nota 9a2adb4a §1)."""

    n: int
    prevalence: float  # fraction of impossible queries in the set
    tp: int  # impossible, abstained
    fp: int  # answerable, abstained
    fn: int  # impossible, answered
    tn: int  # answerable, answered
    precision: float
    recall: float
    f1: float


def abstention_metrics(pairs: Sequence[tuple[bool, bool]]) -> AbstentionMetrics:
    """``pairs`` = (is_impossible, abstained) per query. Precision and
    recall are those of the ABSTAIN decision; F1 depends on the
    prevalence, which is therefore part of the result (a pre-registered
    quantity, never chosen after the fact -- nota WS-EVAL §8)."""
    if not pairs:
        raise ValueError("abstention_metrics: empty sample")
    tp = sum(1 for imp, abst in pairs if imp and abst)
    fp = sum(1 for imp, abst in pairs if not imp and abst)
    fn = sum(1 for imp, abst in pairs if imp and not abst)
    tn = sum(1 for imp, abst in pairs if not imp and not abst)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return AbstentionMetrics(
        n=len(pairs),
        prevalence=(tp + fn) / len(pairs),
        tp=tp,
        fp=fp,
        fn=fn,
        tn=tn,
        precision=precision,
        recall=recall,
        f1=f1,
    )


@dataclass(frozen=True)
class AbstentionCurvePoint:
    threshold: float
    precision: float
    recall: float
    f1: float


def abstention_curve(
    entries: Sequence[tuple[float, bool]],
    *,
    thresholds: Sequence[float] | None = None,
) -> list[AbstentionCurvePoint]:
    """The FULL abstention operating curve the protocol requires
    publishing (not just the frozen operating point): ``entries`` =
    (top fused score, is_impossible) per query -- a query with no hits
    carries score 0.0. At each threshold t the system abstains iff
    top_score < t; the point records precision/recall/F1 of that rule.

    Default thresholds: every distinct observed score plus one step
    above the maximum (the abstain-everything end), so the curve is
    exactly the set of achievable operating points.
    """
    if not entries:
        raise ValueError("abstention_curve: empty sample")
    if thresholds is None:
        distinct = sorted({s for s, _ in entries})
        thresholds = [*distinct, distinct[-1] + 1.0]
    points: list[AbstentionCurvePoint] = []
    for t in thresholds:
        m = abstention_metrics([(imp, score < t) for score, imp in entries])
        points.append(
            AbstentionCurvePoint(threshold=t, precision=m.precision, recall=m.recall, f1=m.f1)
        )
    return points


def freshness_ok(
    hit_ids: Sequence[uuid.UUID],
    *,
    current_id: uuid.UUID,
    stale_id: uuid.UUID,
    k: int,
) -> bool:
    """Freshness verdict for one query (nota WS-EVAL §4): the CURRENT
    version must be in the top-k and outrank the STALE one (which may be
    absent -- absence counts as outranked). Works unchanged for the
    anti-recency cases: there ``current_id`` is the OLDER unit that the
    as-of query makes correct, so a recency prior fails the check."""
    if k <= 0:
        raise ValueError("freshness_ok: k must be positive")
    top = list(hit_ids[:k])
    if current_id not in top:
        return False
    if stale_id not in top:
        return True
    return top.index(current_id) < top.index(stale_id)


def context_token_ratio(served_tokens: int, gold_tokens: int) -> float:
    """Context efficiency = served_tokens / gold_tokens, with the
    denominator FIXED by the protocol (sum of chars/4 over the gold
    units of the query -- nota WS-EVAL T4). 1.0 is a perfectly minimal
    context; values below 1 mean the gold was truncated/partially
    served, values above are overhead the agent pays per query."""
    if gold_tokens <= 0:
        raise ValueError("context_token_ratio: gold_tokens must be positive")
    if served_tokens < 0:
        raise ValueError("context_token_ratio: served_tokens must be non-negative")
    return served_tokens / gold_tokens
