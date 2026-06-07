"""Tests for apex.analytics.benchmark_comparison.

Hand-computed reference values plus edge cases (insufficient data, zero
benchmark variance, no up/down periods, length mismatch).
"""
from __future__ import annotations

import math

from apex.analytics.benchmark_comparison import (
    RELATIVE_KEYS,
    alpha,
    benchmark_comparison,
    beta,
    down_capture,
    tracking_error,
    up_capture,
)

TOL = 1e-12


# --------------------------------------------------------------------------
# beta
# --------------------------------------------------------------------------

def test_beta_perfectly_scaled_is_exact_factor():
    # s = 2 * b  =>  beta == 2.0 exactly (cov/var = 2 * var / var).
    b = [0.01, -0.02, 0.03]
    s = [2 * x for x in b]
    assert math.isclose(beta(s, b), 2.0, rel_tol=1e-9)


def test_beta_known_value():
    # Hand-computed:
    # s = [0.10, -0.10], b = [0.05, -0.05]
    # mean_s = 0, mean_b = 0
    # cov = 0.10*0.05 + (-0.10)*(-0.05) = 0.005 + 0.005 = 0.010
    # var_b = 0.05^2 + 0.05^2 = 0.005
    # beta = 0.010 / 0.005 = 2.0
    s = [0.10, -0.10]
    b = [0.05, -0.05]
    assert math.isclose(beta(s, b), 2.0, abs_tol=TOL)


def test_beta_insufficient_data_returns_zero():
    assert beta([0.01], [0.02]) == 0.0
    assert beta([], []) == 0.0


def test_beta_zero_benchmark_variance_fails_closed():
    # Flat benchmark => var_b == 0 => beta undefined => 0.0.
    assert beta([0.01, 0.02, -0.03], [0.005, 0.005, 0.005]) == 0.0


# --------------------------------------------------------------------------
# alpha
# --------------------------------------------------------------------------

def test_alpha_zero_when_strategy_equals_benchmark():
    # Identical series: beta == 1, ann returns equal => alpha == 0.
    r = [0.01, -0.005, 0.02, 0.0]
    assert math.isclose(alpha(r, r), 0.0, abs_tol=1e-9)


def test_alpha_positive_when_strategy_outperforms_at_same_beta():
    # Strategy is benchmark shifted up by a constant each period => same beta (1)
    # but a higher compounded return => positive alpha.
    b = [0.01, -0.01, 0.02, 0.0]
    s = [x + 0.01 for x in b]
    assert alpha(s, b) > 0.0


def test_alpha_insufficient_data_returns_zero():
    assert alpha([0.01], [0.02]) == 0.0


# --------------------------------------------------------------------------
# capture ratios
# --------------------------------------------------------------------------

def test_up_capture_known_value():
    # b up periods: 0.01 and 0.03 ; s on those: 0.02 and 0.06
    # b_growth = 1.01 * 1.03 = 1.0403 -> move 0.0403
    # s_growth = 1.02 * 1.06 = 1.0812 -> move 0.0812
    # ratio = 0.0812 / 0.0403
    b = [0.01, -0.02, 0.03]
    s = [0.02, -0.04, 0.06]
    expected = (1.02 * 1.06 - 1.0) / (1.01 * 1.03 - 1.0)
    assert math.isclose(up_capture(s, b), expected, rel_tol=1e-12)


def test_down_capture_known_value():
    # Only one down benchmark period: b = -0.02, s = -0.01 there.
    # ratio = (-0.01) / (-0.02) = 0.5  -> strategy cushioned the loss.
    b = [0.01, -0.02, 0.03]
    s = [0.02, -0.01, 0.06]
    assert math.isclose(down_capture(s, b), 0.5, rel_tol=1e-12)


def test_capture_no_qualifying_periods_returns_zero():
    # Benchmark never goes down => down_capture has no periods => 0.0.
    b = [0.01, 0.02, 0.03]
    s = [0.05, 0.05, 0.05]
    assert down_capture(s, b) == 0.0
    # Benchmark never goes up => up_capture 0.0.
    b2 = [-0.01, -0.02]
    assert up_capture(s, b2) == 0.0


# --------------------------------------------------------------------------
# tracking error
# --------------------------------------------------------------------------

def test_tracking_error_zero_when_identical():
    r = [0.01, -0.02, 0.03]
    assert tracking_error(r, r) == 0.0


def test_tracking_error_known_value():
    # active = s - b = [0.01, 0.01]  => constant => pstdev 0 => TE 0.
    s = [0.03, 0.00]
    b = [0.02, -0.01]
    assert math.isclose(tracking_error(s, b), 0.0, abs_tol=TOL)

    # active = [0.01, -0.01] => pstdev = 0.01 ; annualized * sqrt(252)
    s2 = [0.02, 0.00]
    b2 = [0.01, 0.01]
    expected = 0.01 * math.sqrt(252)
    assert math.isclose(tracking_error(s2, b2), expected, rel_tol=1e-12)


def test_tracking_error_insufficient_data_returns_zero():
    assert tracking_error([0.01], [0.02]) == 0.0


# --------------------------------------------------------------------------
# benchmark_comparison (the digest)
# --------------------------------------------------------------------------

def test_comparison_structure_and_keys():
    s = [0.01, -0.005, 0.02, 0.0, 0.01]
    b = [0.008, -0.004, 0.015, 0.002, 0.006]
    out = benchmark_comparison(s, b)
    assert set(out.keys()) == {"strategy", "benchmark", "relative"}
    assert set(out["relative"].keys()) == set(RELATIVE_KEYS)
    # num_periods reflects the paired count.
    assert out["relative"]["num_periods"] == 5.0


def test_comparison_truncates_to_common_length():
    s = [0.01, 0.02, 0.03, 0.04]
    b = [0.01, 0.02]
    out = benchmark_comparison(s, b)
    assert out["relative"]["num_periods"] == 2.0
    # excess_return computed on the paired slice; identical there => 0.
    assert math.isclose(out["relative"]["excess_return"], 0.0, abs_tol=TOL)


def test_comparison_excess_return_known():
    # Strategy total return over [0.10, 0.0] = 1.10*1.00 - 1 = 0.10
    # Benchmark over [0.05, 0.0] = 0.05
    # excess = 0.10 - 0.05 = 0.05
    out = benchmark_comparison([0.10, 0.0], [0.05, 0.0])
    assert math.isclose(out["relative"]["excess_return"], 0.05, rel_tol=1e-12)


def test_comparison_identical_series_self_consistent():
    r = [0.01, -0.01, 0.02, 0.0, 0.015]
    out = benchmark_comparison(r, r)
    rel = out["relative"]
    assert math.isclose(rel["beta"], 1.0, rel_tol=1e-9)
    assert math.isclose(rel["alpha"], 0.0, abs_tol=1e-9)
    assert math.isclose(rel["correlation"], 1.0, rel_tol=1e-9)
    assert math.isclose(rel["tracking_error"], 0.0, abs_tol=TOL)
    assert math.isclose(rel["excess_return"], 0.0, abs_tol=TOL)
    assert out["strategy"] == out["benchmark"]


def test_comparison_empty_series_zeroed_not_raising():
    out = benchmark_comparison([], [])
    rel = out["relative"]
    assert rel["num_periods"] == 0.0
    assert rel["beta"] == 0.0
    assert rel["alpha"] == 0.0
    assert rel["up_capture"] == 0.0
    assert rel["down_capture"] == 0.0
    assert rel["tracking_error"] == 0.0
