"""
tests.test_deflated_sharpe
==========================
Tests for the Probabilistic Sharpe Ratio (PSR) and Deflated Sharpe Ratio (DSR).

Math is checked against hand-computed values where feasible; behavioral tests
cover the headline properties: strong low-noise edge → high PSR, pure noise →
ambiguous, DSR shrinks as the number of trials grows. Determinism is verified
explicitly (same inputs → same output).
"""
from __future__ import annotations

import math
import random

import pytest

from apex.validation.deflated_sharpe import (
    _norm_cdf,
    _norm_ppf,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
    sample_sharpe,
)


# --------------------------------------------------------------------------- #
# Helpers for the normal CDF / inverse CDF building blocks.
# --------------------------------------------------------------------------- #
def test_norm_cdf_known_values() -> None:
    assert _norm_cdf(0.0) == pytest.approx(0.5, abs=1e-12)
    # ~68% within +/-1 sigma, ~95% within +/-2 sigma.
    assert _norm_cdf(1.0) == pytest.approx(0.8413447, abs=1e-6)
    assert _norm_cdf(-1.0) == pytest.approx(0.1586553, abs=1e-6)
    assert _norm_cdf(1.959964) == pytest.approx(0.975, abs=1e-5)


def test_norm_ppf_inverts_cdf() -> None:
    # ppf is the inverse of cdf.
    for p in (0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99):
        assert _norm_cdf(_norm_ppf(p)) == pytest.approx(p, abs=1e-6)
    # Known quantiles.
    assert _norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
    assert _norm_ppf(0.975) == pytest.approx(1.959964, abs=1e-4)


def test_norm_ppf_extremes() -> None:
    assert _norm_ppf(0.0) == -math.inf
    assert _norm_ppf(1.0) == math.inf


# --------------------------------------------------------------------------- #
# sample_sharpe
# --------------------------------------------------------------------------- #
def test_sample_sharpe_basic() -> None:
    # mean=2, pstdev of [1,2,3] = sqrt(2/3) -> sharpe = 2 / 0.8164966 = 2.4494897
    assert sample_sharpe([1.0, 2.0, 3.0]) == pytest.approx(2.4494897, abs=1e-6)


def test_sample_sharpe_degenerate() -> None:
    assert sample_sharpe([0.5]) == 0.0          # too few points
    assert sample_sharpe([]) == 0.0
    assert sample_sharpe([0.3, 0.3, 0.3]) == 0.0  # no variance


# --------------------------------------------------------------------------- #
# probabilistic_sharpe_ratio — hand-computable Gaussian-ish case.
# --------------------------------------------------------------------------- #
def test_psr_hand_computed_symmetric_series() -> None:
    """
    A perfectly symmetric series has zero skew. We hand-compute the PSR.

    returns = [-1, 1] repeated so mean and std are exact, then shifted to give
    a positive Sharpe. We use a constructed series with known moments.

    Take returns r where mean=0.5, pstdev=1.0 by construction:
        base symmetric around 0.5 with spread 1.0.
    Use [0.5-1, 0.5+1] * k  -> [-0.5, 1.5] repeated.
        mean = 0.5, pstdev = 1.0, skew = 0, kurtosis = 1 (two-point dist).
    SR_hat = 0.5.
    variance = 1 - skew*SR + (kurt-1)/4 * SR^2 = 1 - 0 + (1-1)/4*0.25 = 1.0
    n = number of points. z = (0.5 - 0)*sqrt(n-1)/sqrt(1) = 0.5*sqrt(n-1).
    """
    series = [-0.5, 1.5] * 25  # n = 50
    n = len(series)
    sr = sample_sharpe(series)
    assert sr == pytest.approx(0.5, abs=1e-12)

    # Two-point symmetric distribution: skew 0, kurtosis 1, so variance term = 1.
    z = 0.5 * math.sqrt(n - 1)
    expected = _norm_cdf(z)
    assert probabilistic_sharpe_ratio(series, 0.0) == pytest.approx(expected, abs=1e-9)


def test_psr_against_own_sharpe_is_one_half() -> None:
    """PSR against a benchmark equal to the observed Sharpe must be exactly 0.5."""
    series = [-0.5, 1.5] * 25
    sr = sample_sharpe(series)
    assert probabilistic_sharpe_ratio(series, sr_benchmark=sr) == pytest.approx(0.5, abs=1e-9)


def test_psr_in_unit_interval() -> None:
    rng = random.Random(7)
    series = [rng.gauss(0.001, 0.02) for _ in range(200)]
    p = probabilistic_sharpe_ratio(series, 0.0)
    assert 0.0 <= p <= 1.0


def test_psr_strong_low_noise_edge_is_high() -> None:
    """A strongly positive, low-noise series → PSR close to 1."""
    # Tight cluster of positive returns: high Sharpe, long sample.
    rng = random.Random(123)
    series = [0.01 + rng.gauss(0.0, 0.002) for _ in range(250)]
    p = probabilistic_sharpe_ratio(series, 0.0)
    assert p > 0.99


def test_psr_pure_noise_is_ambiguous() -> None:
    """Zero-mean noise → PSR is NOT confidently positive (well below ~1)."""
    rng = random.Random(99)
    series = [rng.gauss(0.0, 0.02) for _ in range(250)]
    p = probabilistic_sharpe_ratio(series, 0.0)
    # Pure noise must never produce a confident edge claim. Depending on the
    # random sign of the in-sample drift it can land low or middling, but never
    # near 1 the way a true low-noise edge does.
    assert p < 0.9


def test_psr_grows_with_sample_length() -> None:
    """Same per-period edge, more data → higher confidence."""
    short = [-0.5, 1.5] * 5    # n=10
    long = [-0.5, 1.5] * 100   # n=200
    assert sample_sharpe(short) == pytest.approx(sample_sharpe(long), abs=1e-12)
    assert probabilistic_sharpe_ratio(long, 0.0) > probabilistic_sharpe_ratio(short, 0.0)


def test_psr_skew_correction_direction() -> None:
    """
    Direct check of the skew term: for a positive Sharpe, negative skewness
    (a fat left tail = occasional big losses) reduces the PSR relative to a
    symmetric series with the SAME mean, std, and sample length.
    """
    import statistics as _st

    # Negative-skew series: mostly small gains, a few large negative outliers.
    neg = [0.10] * 96 + [-1.0] * 4

    mean = _st.fmean(neg)
    sd = _st.pstdev(neg)
    sr = mean / sd
    assert sr > 0  # confirm positive edge

    # Symmetric twin with identical mean, std, n=2-point -> skew 0.
    twin = [mean - sd, mean + sd] * (len(neg) // 2)

    p_neg_skew = probabilistic_sharpe_ratio(neg, 0.0)
    p_symmetric = probabilistic_sharpe_ratio(twin, 0.0)
    # neg has negative skewness (heavy left tail) -> lower confidence.
    assert p_neg_skew < p_symmetric


# --------------------------------------------------------------------------- #
# expected_max_sharpe
# --------------------------------------------------------------------------- #
def test_expected_max_sharpe_single_trial_is_zero() -> None:
    assert expected_max_sharpe(1) == 0.0
    assert expected_max_sharpe(0) == 0.0


def test_expected_max_sharpe_increases_with_trials() -> None:
    e2 = expected_max_sharpe(2)
    e10 = expected_max_sharpe(10)
    e1000 = expected_max_sharpe(1000)
    assert 0.0 < e2 < e10 < e1000


def test_expected_max_sharpe_scales_with_variance() -> None:
    # E[max] scales with sqrt(variance).
    base = expected_max_sharpe(50, variance_of_trial_sharpes=1.0)
    quad = expected_max_sharpe(50, variance_of_trial_sharpes=4.0)
    assert quad == pytest.approx(2.0 * base, rel=1e-9)


def test_expected_max_sharpe_hand_computed() -> None:
    """Hand-check the formula for N=10, V=1."""
    from apex.validation.deflated_sharpe import _EULER_MASCHERONI as g

    n = 10.0
    expected = (1.0 - g) * _norm_ppf(1.0 - 1.0 / n) + g * _norm_ppf(1.0 - 1.0 / (n * math.e))
    assert expected_max_sharpe(10) == pytest.approx(expected, abs=1e-12)


# --------------------------------------------------------------------------- #
# deflated_sharpe_ratio
# --------------------------------------------------------------------------- #
def test_dsr_one_trial_equals_psr_vs_zero() -> None:
    series = [0.01 + 0.001 * ((-1) ** i) for i in range(100)]
    assert deflated_sharpe_ratio(series, num_trials=1) == pytest.approx(
        probabilistic_sharpe_ratio(series, 0.0), abs=1e-12
    )


def test_dsr_shrinks_as_trials_grow() -> None:
    """The headline DSR property: more trials → smaller probability."""
    rng = random.Random(2024)
    # A moderately positive series so the DSR has room to shrink.
    series = [0.003 + rng.gauss(0.0, 0.01) for _ in range(252)]
    d1 = deflated_sharpe_ratio(series, num_trials=1)
    d10 = deflated_sharpe_ratio(series, num_trials=10)
    d100 = deflated_sharpe_ratio(series, num_trials=100)
    d1000 = deflated_sharpe_ratio(series, num_trials=1000)
    assert d1 >= d10 >= d100 >= d1000
    assert d1 > d1000  # strictly shrinks overall


def test_dsr_in_unit_interval() -> None:
    rng = random.Random(5)
    series = [rng.gauss(0.001, 0.02) for _ in range(150)]
    for trials in (1, 5, 50, 500):
        d = deflated_sharpe_ratio(series, num_trials=trials)
        assert 0.0 <= d <= 1.0


def test_dsr_strong_edge_survives_modest_trials() -> None:
    """A genuinely strong, long, low-noise edge survives a handful of trials."""
    rng = random.Random(777)
    series = [0.008 + rng.gauss(0.0, 0.004) for _ in range(252)]
    assert deflated_sharpe_ratio(series, num_trials=10) > 0.9


def test_dsr_noise_with_many_trials_is_low() -> None:
    """Pure noise selected as the best of many trials → low DSR."""
    rng = random.Random(31)
    series = [rng.gauss(0.0005, 0.02) for _ in range(252)]
    assert deflated_sharpe_ratio(series, num_trials=1000) < 0.5


# --------------------------------------------------------------------------- #
# Edge cases / fail-closed behavior.
# --------------------------------------------------------------------------- #
def test_psr_too_few_points_fails_closed() -> None:
    assert probabilistic_sharpe_ratio([], 0.0) == 0.0
    assert probabilistic_sharpe_ratio([0.5], 0.0) == 0.0


def test_psr_no_variance_fails_closed() -> None:
    assert probabilistic_sharpe_ratio([0.01, 0.01, 0.01, 0.01], 0.0) == 0.0


def test_dsr_too_few_points_fails_closed() -> None:
    assert deflated_sharpe_ratio([0.5], num_trials=10) == 0.0
    assert deflated_sharpe_ratio([], num_trials=10) == 0.0


# --------------------------------------------------------------------------- #
# Determinism.
# --------------------------------------------------------------------------- #
def test_determinism() -> None:
    series = [0.01, -0.02, 0.03, 0.005, -0.001, 0.02, 0.015, -0.03] * 10
    a = probabilistic_sharpe_ratio(series, 0.0)
    b = probabilistic_sharpe_ratio(series, 0.0)
    assert a == b
    c = deflated_sharpe_ratio(series, num_trials=20)
    d = deflated_sharpe_ratio(series, num_trials=20)
    assert c == d
