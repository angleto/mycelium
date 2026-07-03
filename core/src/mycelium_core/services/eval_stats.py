"""Statistical machinery for the WS-EVAL protocol (task d7c0693e, nota
WS-EVAL §8): confidence intervals, paired tests and cluster-aware
resampling that every published number must carry.

Pure functions, no DB, no third-party stats dependency (scipy is NOT a
project dependency -- closed forms are implemented directly and pinned
against hand-checked values in ``core/tests/test_eval_stats.py``).

Conventions:
- ``rng`` is always injected (``random.Random``) so every resampling
  procedure is reproducible from a declared seed.
- Functions raise ``ValueError`` on degenerate inputs instead of
  guessing (an eval harness must fail loudly, not silently report 0).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from random import Random

# 97.5th percentile of the standard normal: the z for two-sided 95% CIs.
_Z95 = 1.959963984540054


def wilson_ci(successes: int, n: int, *, z: float = _Z95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    center = (p + z^2/2n) / (1 + z^2/n)
    half   = z * sqrt(p(1-p)/n + z^2/4n^2) / (1 + z^2/n)

    Preferred over the Wald interval because it behaves at the extremes
    (0/n and n/n) where recall/leak rates actually live.
    """
    if n <= 0:
        raise ValueError("wilson_ci: n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("wilson_ci: successes out of range")
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    # The boundary cases are exact by definition (float rounding would
    # otherwise leave e.g. the n/n upper limit at 0.999...9).
    lo = 0.0 if successes == 0 else max(0.0, center - half)
    hi = 1.0 if successes == n else min(1.0, center + half)
    return (lo, hi)


def clopper_pearson_upper_zero(n: int, *, alpha: float = 0.05) -> float:
    """Exact one-sided upper bound for a rate when ZERO events were
    observed in ``n`` trials: ``1 - alpha**(1/n)``.

    This is the closed form of the Clopper-Pearson upper limit at x=0
    (the general x needs a Beta inverse; the protocol only makes
    zero-event claims -- leak, erasure survivors). The "rule of three"
    3/n is its first-order approximation.
    """
    if n <= 0:
        raise ValueError("clopper_pearson_upper_zero: n must be positive")
    if not 0 < alpha < 1:
        raise ValueError("clopper_pearson_upper_zero: alpha in (0,1)")
    return 1.0 - float(alpha ** (1.0 / n))


def percentile_bootstrap(
    values: Sequence[float],
    *,
    rng: Random,
    n_resamples: int = 2000,
    statistic: Callable[[Sequence[float]], float] | None = None,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile bootstrap CI for ``statistic`` (default: mean) of
    ``values``: resample n values with replacement ``n_resamples`` times,
    take the (alpha/2, 1-alpha/2) percentiles of the statistic.
    """
    if not values:
        raise ValueError("percentile_bootstrap: empty sample")
    if n_resamples <= 0:
        raise ValueError("percentile_bootstrap: n_resamples must be positive")
    stat = statistic or _mean
    n = len(values)
    estimates = sorted(
        stat([values[rng.randrange(n)] for _ in range(n)]) for _ in range(n_resamples)
    )
    return (
        _percentile(estimates, alpha / 2),
        _percentile(estimates, 1 - alpha / 2),
    )


def cluster_bootstrap(
    clustered: Sequence[tuple[str, float]],
    *,
    rng: Random,
    n_resamples: int = 2000,
    statistic: Callable[[Sequence[float]], float] | None = None,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile bootstrap that resamples CLUSTERS (e.g. the gold FACT a
    query was generated from, nota WS-EVAL §3) instead of observations:
    queries derived from the same fact are correlated, and a naive
    bootstrap would understate the variance (CI too narrow).

    ``clustered`` is (cluster_id, value) pairs; each resample draws k
    clusters with replacement (k = number of distinct clusters) and
    concatenates their values.
    """
    if not clustered:
        raise ValueError("cluster_bootstrap: empty sample")
    if n_resamples <= 0:
        raise ValueError("cluster_bootstrap: n_resamples must be positive")
    stat = statistic or _mean
    by_cluster: dict[str, list[float]] = {}
    for cid, v in clustered:
        by_cluster.setdefault(cid, []).append(v)
    clusters = list(by_cluster.values())
    k = len(clusters)
    estimates: list[float] = []
    for _ in range(n_resamples):
        sample: list[float] = []
        for _ in range(k):
            sample.extend(clusters[rng.randrange(k)])
        estimates.append(stat(sample))
    estimates.sort()
    return (
        _percentile(estimates, alpha / 2),
        _percentile(estimates, 1 - alpha / 2),
    )


def mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar test on the DISCORDANT pairs of two
    paired binary outcomes (config A right / config B wrong = b, the
    reverse = c): p = 2 * P(X <= min(b,c)) with X ~ Bin(b+c, 1/2),
    clamped to 1. The concordant pairs carry no information.

    The paired test the protocol mandates for config comparisons on the
    SAME query set (nota WS-EVAL §8): two independent proportions would
    throw the pairing away.
    """
    if b < 0 or c < 0:
        raise ValueError("mcnemar_exact: counts must be non-negative")
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2.0**n)
    return min(1.0, 2.0 * tail)


def paired_permutation_pvalue(
    diffs: Sequence[float],
    *,
    rng: Random,
    n_resamples: int = 2000,
) -> float:
    """Two-sided sign-flip permutation test on per-query paired
    differences (config A score - config B score): under H0 each
    difference is symmetric around 0, so random sign flips generate the
    null distribution of the mean. p = (1 + #{|mean_perm| >= |mean_obs|})
    / (n_resamples + 1) -- the +1 keeps p > 0 (Phipson-Smyth).
    """
    if not diffs:
        raise ValueError("paired_permutation_pvalue: empty sample")
    if n_resamples <= 0:
        raise ValueError("paired_permutation_pvalue: n_resamples must be positive")
    observed = abs(_mean(diffs))
    if observed == 0.0:
        return 1.0
    hits = 0
    for _ in range(n_resamples):
        flipped = [d if rng.random() < 0.5 else -d for d in diffs]
        if abs(_mean(flipped)) >= observed:
            hits += 1
    return (1 + hits) / (n_resamples + 1)


def binomial_tail_pvalue(successes: int, n: int, p0: float) -> float:
    """One-sided exact binomial p-value P(X >= successes | X~Bin(n, p0)):
    the primary-endpoint test "is the true rate ABOVE the threshold p0?"
    (nota WS-EVAL §8). Computed in log space (lgamma) so n in the
    thousands does not overflow.
    """
    if n <= 0:
        raise ValueError("binomial_tail_pvalue: n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("binomial_tail_pvalue: successes out of range")
    if not 0 < p0 < 1:
        raise ValueError("binomial_tail_pvalue: p0 in (0,1)")
    if successes == 0:
        return 1.0  # P(X >= 0) is 1 by definition, no float summation needed
    log_p0 = math.log(p0)
    log_q0 = math.log1p(-p0)
    total = 0.0
    for k in range(successes, n + 1):
        log_pmf = (
            math.lgamma(n + 1)
            - math.lgamma(k + 1)
            - math.lgamma(n - k + 1)
            + k * log_p0
            + (n - k) * log_q0
        )
        total += math.exp(log_pmf)
    return min(1.0, total)


@dataclass(frozen=True)
class HolmResult:
    name: str
    pvalue: float
    adjusted_alpha: float
    rejected: bool  # True = H0 rejected = endpoint PASSES its gate


def holm_bonferroni(
    named_pvalues: Sequence[tuple[str, float]], *, alpha: float = 0.05
) -> list[HolmResult]:
    """Holm-Bonferroni step-down over the PRIMARY endpoints: sort
    p-values ascending, compare the i-th (0-based) against
    alpha/(m-i); stop rejecting at the first failure (all subsequent
    hypotheses are not rejected). Controls FWER across the multiple
    primary gates (nota WS-EVAL §8).

    Results are returned in the INPUT order, each carrying the alpha it
    was compared against.
    """
    if not named_pvalues:
        raise ValueError("holm_bonferroni: no endpoints")
    m = len(named_pvalues)
    order = sorted(range(m), key=lambda i: named_pvalues[i][1])
    rejected = [False] * m
    adjusted = [0.0] * m
    still_rejecting = True
    for step, idx in enumerate(order):
        threshold = alpha / (m - step)
        adjusted[idx] = threshold
        if still_rejecting and named_pvalues[idx][1] <= threshold:
            rejected[idx] = True
        else:
            still_rejecting = False
    return [
        HolmResult(name=nm, pvalue=pv, adjusted_alpha=adjusted[i], rejected=rejected[i])
        for i, (nm, pv) in enumerate(named_pvalues)
    ]


def icc_oneway(groups: Sequence[Sequence[float]]) -> float:
    """ICC(1) from the one-way ANOVA estimator over clusters (queries
    grouped by gold fact): with k groups, N observations and
    n0 = (N - sum(n_i^2)/N) / (k-1),

        ICC = (MSB - MSW) / (MSB + (n0 - 1) * MSW)

    Can be negative when within-group variance dominates; callers clamp
    at 0 for the design effect (a negative ICC never inflates n).
    """
    ks = [g for g in groups if g]
    k = len(ks)
    if k < 2:
        raise ValueError("icc_oneway: need at least 2 non-empty groups")
    n_total = sum(len(g) for g in ks)
    if n_total <= k:
        raise ValueError("icc_oneway: need at least one group with 2+ members")
    grand = sum(sum(g) for g in ks) / n_total
    ss_between = sum(len(g) * (_mean(g) - grand) ** 2 for g in ks)
    ss_within = sum(sum((x - _mean(g)) ** 2 for x in g) for g in ks)
    msb = ss_between / (k - 1)
    msw = ss_within / (n_total - k)
    n0 = (n_total - sum(len(g) ** 2 for g in ks) / n_total) / (k - 1)
    denom = msb + (n0 - 1) * msw
    if denom == 0:
        return 0.0
    return (msb - msw) / denom


def design_effect(mean_cluster_size: float, icc: float) -> float:
    """Kish design effect ``1 + (m-1) * max(icc, 0)``: the factor by
    which clustering inflates the required sample size (applied when
    ICC > 0.2 per nota WS-EVAL §8)."""
    if mean_cluster_size < 1:
        raise ValueError("design_effect: mean cluster size >= 1")
    return 1.0 + (mean_cluster_size - 1.0) * max(icc, 0.0)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile on an ALREADY SORTED sequence."""
    if not sorted_values:
        raise ValueError("_percentile: empty")
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac
