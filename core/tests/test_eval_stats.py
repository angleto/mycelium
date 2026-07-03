"""WS-EVAL statistical machinery (task d7c0693e): every closed form is
pinned against a hand-checked value, resampling procedures against
degenerate cases where the answer is exact, plus seeded determinism."""

from __future__ import annotations

from random import Random

import pytest

from mycelium_core.services.eval_stats import (
    binomial_tail_pvalue,
    clopper_pearson_upper_zero,
    cluster_bootstrap,
    design_effect,
    holm_bonferroni,
    icc_oneway,
    mcnemar_exact,
    paired_permutation_pvalue,
    percentile_bootstrap,
    wilson_ci,
)


def test_wilson_ci_hand_checked() -> None:
    # 50/100 at z=1.95996...: the textbook (0.40383, 0.59617).
    lo, hi = wilson_ci(50, 100)
    assert lo == pytest.approx(0.40383, abs=1e-4)
    assert hi == pytest.approx(0.59617, abs=1e-4)
    # 0/10: lower clamps to 0, upper is the known 0.27753.
    lo0, hi0 = wilson_ci(0, 10)
    assert lo0 == 0.0
    assert hi0 == pytest.approx(0.27753, abs=1e-4)
    # n/n mirrors 0/n.
    lo1, hi1 = wilson_ci(10, 10)
    assert lo1 == pytest.approx(1 - 0.27753, abs=1e-4)
    assert hi1 == 1.0
    with pytest.raises(ValueError):
        wilson_ci(1, 0)
    with pytest.raises(ValueError):
        wilson_ci(5, 4)


def test_clopper_pearson_upper_zero_hand_checked() -> None:
    # 1 - 0.05^(1/1000) = 0.0029912 (the "3/n rule" says 0.003).
    assert clopper_pearson_upper_zero(1000) == pytest.approx(0.0029912, abs=1e-6)
    # n=3: 1 - 0.05^(1/3) = 0.63160.
    assert clopper_pearson_upper_zero(3) == pytest.approx(0.63160, abs=1e-4)
    with pytest.raises(ValueError):
        clopper_pearson_upper_zero(0)


def test_mcnemar_exact_hand_checked() -> None:
    # b=1, c=9: 2 * (C(10,0)+C(10,1)) / 2^10 = 22/1024 = 0.0214844.
    assert mcnemar_exact(1, 9) == pytest.approx(0.0214844, abs=1e-6)
    # Symmetric discordants clamp to 1.
    assert mcnemar_exact(2, 2) == 1.0
    # No discordant pairs carry no evidence.
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(9, 1) == mcnemar_exact(1, 9)


def test_binomial_tail_pvalue_hand_checked() -> None:
    # P(X >= 9 | Bin(10, 0.5)) = 11/1024 = 0.0107422.
    assert binomial_tail_pvalue(9, 10, 0.5) == pytest.approx(0.0107422, abs=1e-6)
    assert binomial_tail_pvalue(0, 10, 0.5) == 1.0
    # Large-n log-space path stays finite and sane.
    assert 0.0 < binomial_tail_pvalue(960, 1000, 0.94) < 1.0


def test_holm_bonferroni_step_down() -> None:
    res = holm_bonferroni([("a", 0.01), ("b", 0.04), ("c", 0.03)], alpha=0.05)
    by = {r.name: r for r in res}
    # a: 0.01 <= 0.05/3 -> rejected; c: 0.03 > 0.05/2 -> stop; b follows.
    assert by["a"].rejected and by["a"].adjusted_alpha == pytest.approx(0.05 / 3)
    assert not by["c"].rejected and by["c"].adjusted_alpha == pytest.approx(0.025)
    assert not by["b"].rejected
    with pytest.raises(ValueError):
        holm_bonferroni([])


def test_icc_and_design_effect() -> None:
    # Perfect clustering: all variance between groups -> ICC = 1.
    assert icc_oneway([[1.0, 1.0], [0.0, 0.0]]) == pytest.approx(1.0)
    # No between-group variance -> ICC = -1 with two groups of two
    # (MSB=0, MSW=0.5: (0-0.5)/(0+0.5)).
    assert icc_oneway([[1.0, 0.0], [1.0, 0.0]]) == pytest.approx(-1.0)
    assert design_effect(2, 0.5) == pytest.approx(1.5)
    # Negative ICC never inflates the sample.
    assert design_effect(3, -0.4) == 1.0
    with pytest.raises(ValueError):
        icc_oneway([[1.0, 2.0]])


def test_percentile_bootstrap_degenerate_and_deterministic() -> None:
    # A constant sample has a point CI.
    lo, hi = percentile_bootstrap([2.0] * 10, rng=Random(7), n_resamples=200)  # noqa: S311 (resampling determinism, not crypto)
    assert (lo, hi) == (2.0, 2.0)
    vals = [0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0]
    a = percentile_bootstrap(vals, rng=Random(42), n_resamples=500)  # noqa: S311 (resampling determinism, not crypto)
    b = percentile_bootstrap(vals, rng=Random(42), n_resamples=500)  # noqa: S311 (resampling determinism, not crypto)
    assert a == b  # seeded reproducibility, digit for digit
    assert 0.0 <= a[0] <= a[1] <= 1.0
    with pytest.raises(ValueError):
        percentile_bootstrap([], rng=Random(1))  # noqa: S311 (resampling determinism, not crypto)


def test_cluster_bootstrap_single_cluster_is_point() -> None:
    # One cluster: every resample is the whole cluster -> point CI.
    pairs = [("f1", 1.0), ("f1", 0.0)]
    lo, hi = cluster_bootstrap(pairs, rng=Random(3), n_resamples=100)  # noqa: S311 (resampling determinism, not crypto)
    assert lo == pytest.approx(0.5) and hi == pytest.approx(0.5)
    # Two extreme clusters: the CI spans them (wider than naive).
    pairs2 = [("a", 1.0), ("a", 1.0), ("b", 0.0), ("b", 0.0)]
    lo2, hi2 = cluster_bootstrap(pairs2, rng=Random(3), n_resamples=500)  # noqa: S311 (resampling determinism, not crypto)
    assert lo2 == 0.0 and hi2 == 1.0


def test_paired_permutation_pvalue() -> None:
    assert paired_permutation_pvalue([0.0] * 10, rng=Random(1)) == 1.0  # noqa: S311 (resampling determinism, not crypto)
    # A consistent positive difference across 20 pairs is significant.
    p = paired_permutation_pvalue([1.0] * 20, rng=Random(1), n_resamples=2000)  # noqa: S311 (resampling determinism, not crypto)
    assert p < 0.01
    a = paired_permutation_pvalue([0.3, -0.1, 0.2, 0.4], rng=Random(5), n_resamples=999)  # noqa: S311 (resampling determinism, not crypto)
    b = paired_permutation_pvalue([0.3, -0.1, 0.2, 0.4], rng=Random(5), n_resamples=999)  # noqa: S311 (resampling determinism, not crypto)
    assert a == b
