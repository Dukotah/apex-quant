"""Tests for apex.validation.bootstrap_ci (seeded percentile bootstrap)."""

from __future__ import annotations

import statistics

import pytest

from apex.validation.bootstrap_ci import (
    BootstrapCI,
    _percentile,
    bootstrap_metric_ci,
)


def mean_metric(xs):
    return statistics.fmean(xs)


# --------------------------------------------------------------------------- #
# _percentile: hand-computed linear-interpolation values                      #
# --------------------------------------------------------------------------- #


def test_percentile_endpoints():
    vals = [10.0, 20.0, 30.0, 40.0]
    assert _percentile(vals, 0.0) == 10.0
    assert _percentile(vals, 1.0) == 40.0


def test_percentile_median_even_length():
    # pos = 0.5 * 3 = 1.5 -> between 20 and 30 -> 25
    vals = [10.0, 20.0, 30.0, 40.0]
    assert _percentile(vals, 0.5) == 25.0


def test_percentile_interpolation():
    # [0,1,2,3,4], q=0.25 -> pos = 0.25*4 = 1.0 -> exactly 1.0
    vals = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert _percentile(vals, 0.25) == 1.0
    # q=0.1 -> pos = 0.4 -> 0 + (1-0)*0.4 = 0.4
    assert _percentile(vals, 0.1) == pytest.approx(0.4)


def test_percentile_single_element():
    assert _percentile([7.0], 0.0) == 7.0
    assert _percentile([7.0], 0.5) == 7.0
    assert _percentile([7.0], 1.0) == 7.0


# --------------------------------------------------------------------------- #
# bootstrap_metric_ci: basic behavior                                         #
# --------------------------------------------------------------------------- #


def test_constant_series_zero_width_interval():
    # Every resample of a constant series has the same mean -> degenerate CI.
    data = [0.05] * 50
    ci = bootstrap_metric_ci(data, mean_metric, iterations=500, seed=1)
    assert ci is not None
    assert ci.point_estimate == pytest.approx(0.05)
    assert ci.lower == pytest.approx(0.05)
    assert ci.upper == pytest.approx(0.05)
    assert ci.std_error == pytest.approx(0.0)
    assert ci.median == pytest.approx(0.05)
    assert ci.mean == pytest.approx(0.05)


def test_point_estimate_is_metric_on_original():
    data = [0.01, -0.02, 0.03, 0.04, -0.01, 0.02]
    ci = bootstrap_metric_ci(data, mean_metric, iterations=300, seed=7)
    assert ci is not None
    assert ci.point_estimate == pytest.approx(statistics.fmean(data))


def test_interval_brackets_point_estimate():
    data = [0.01, 0.02, -0.03, 0.05, -0.02, 0.04, 0.0, 0.03, -0.01, 0.02]
    ci = bootstrap_metric_ci(data, mean_metric, iterations=1000, seed=123)
    assert ci is not None
    # The point estimate should sit within the 95% interval for a symmetric-ish
    # metric like the mean.
    assert ci.lower <= ci.point_estimate <= ci.upper
    assert ci.lower <= ci.upper


def test_determinism_same_seed():
    data = [0.01, -0.02, 0.03, 0.04, -0.01, 0.02, 0.05, -0.03]
    a = bootstrap_metric_ci(data, mean_metric, iterations=400, seed=99)
    b = bootstrap_metric_ci(data, mean_metric, iterations=400, seed=99)
    assert a == b


def test_different_seed_changes_distribution():
    data = [0.01, -0.02, 0.03, 0.04, -0.01, 0.02, 0.05, -0.03]
    a = bootstrap_metric_ci(data, mean_metric, iterations=400, seed=1)
    b = bootstrap_metric_ci(data, mean_metric, iterations=400, seed=2)
    assert a is not None and b is not None
    # Point estimate identical (same original data); resample stats differ.
    assert a.point_estimate == pytest.approx(b.point_estimate)
    assert (a.lower, a.upper) != (b.lower, b.upper)


def test_confidence_width_monotonic():
    data = [0.01, -0.02, 0.03, 0.04, -0.01, 0.02, 0.05, -0.03, 0.0, 0.01]
    narrow = bootstrap_metric_ci(data, mean_metric, iterations=1000, seed=5, confidence=0.80)
    wide = bootstrap_metric_ci(data, mean_metric, iterations=1000, seed=5, confidence=0.99)
    assert narrow is not None and wide is not None
    assert (wide.upper - wide.lower) >= (narrow.upper - narrow.lower)


def test_metadata_fields():
    data = [0.01, 0.02, 0.03, 0.04]
    ci = bootstrap_metric_ci(data, mean_metric, iterations=250, seed=3, confidence=0.90)
    assert ci is not None
    assert ci.iterations == 250
    assert ci.n == 4
    assert ci.confidence == 0.90


# --------------------------------------------------------------------------- #
# Insufficient data / invalid params -> fail closed (None)                    #
# --------------------------------------------------------------------------- #


def test_empty_returns_none():
    assert bootstrap_metric_ci([], mean_metric) is None


def test_single_observation_returns_none():
    assert bootstrap_metric_ci([0.01], mean_metric) is None


def test_zero_iterations_returns_none():
    assert bootstrap_metric_ci([0.01, 0.02, 0.03], mean_metric, iterations=0) is None


def test_invalid_confidence_returns_none():
    data = [0.01, 0.02, 0.03]
    assert bootstrap_metric_ci(data, mean_metric, confidence=0.0) is None
    assert bootstrap_metric_ci(data, mean_metric, confidence=1.0) is None
    assert bootstrap_metric_ci(data, mean_metric, confidence=1.5) is None


# --------------------------------------------------------------------------- #
# BootstrapCI helper methods                                                  #
# --------------------------------------------------------------------------- #


def test_contains():
    ci = BootstrapCI(
        point_estimate=0.5,
        lower=0.1,
        upper=0.9,
        median=0.5,
        mean=0.5,
        std_error=0.2,
        confidence=0.95,
        iterations=100,
        n=10,
    )
    assert ci.contains(0.5)
    assert ci.contains(0.1)
    assert ci.contains(0.9)
    assert not ci.contains(0.0)
    assert not ci.contains(1.0)


def test_excludes_zero_positive_interval():
    ci = BootstrapCI(
        point_estimate=0.5,
        lower=0.1,
        upper=0.9,
        median=0.5,
        mean=0.5,
        std_error=0.2,
        confidence=0.95,
        iterations=100,
        n=10,
    )
    assert ci.excludes_zero()


def test_excludes_zero_negative_interval():
    ci = BootstrapCI(
        point_estimate=-0.5,
        lower=-0.9,
        upper=-0.1,
        median=-0.5,
        mean=-0.5,
        std_error=0.2,
        confidence=0.95,
        iterations=100,
        n=10,
    )
    assert ci.excludes_zero()


def test_does_not_exclude_zero_straddling_interval():
    ci = BootstrapCI(
        point_estimate=0.05,
        lower=-0.1,
        upper=0.2,
        median=0.05,
        mean=0.05,
        std_error=0.1,
        confidence=0.95,
        iterations=100,
        n=10,
    )
    assert not ci.excludes_zero()


def test_summary_string():
    ci = bootstrap_metric_ci([0.01, 0.02, 0.03, 0.04], mean_metric, iterations=100, seed=1)
    assert ci is not None
    s = ci.summary()
    assert "Bootstrap CI [95%]" in s
    assert "estimate=" in s


# --------------------------------------------------------------------------- #
# Works with a real metric from apex.validation.metrics                       #
# --------------------------------------------------------------------------- #


def test_works_with_metrics_sharpe():
    from apex.validation import metrics

    data = [0.01, -0.005, 0.02, 0.015, -0.01, 0.008, 0.012, -0.003, 0.01, 0.006]
    ci = bootstrap_metric_ci(data, lambda xs: metrics.sharpe_ratio(xs), iterations=500, seed=42)
    assert ci is not None
    assert ci.point_estimate == pytest.approx(metrics.sharpe_ratio(data))
    assert ci.lower <= ci.upper
    assert ci.n == len(data)
