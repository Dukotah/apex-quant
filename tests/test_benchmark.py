"""
Tests for apex.validation.benchmark — verified against hand-computed values.
Benchmark-relative metrics are only useful if they're exactly right; these lock
the math against known cases: a 2x-scaled clone (beta 2, alpha ~0, captures 2),
an identical series (IR/TE 0), and the documented degenerate edge cases.
"""

from __future__ import annotations

import math

import pytest

from apex.validation import benchmark

# A small benchmark return series with both up and down days.
BENCH = [0.01, -0.02, 0.03, -0.01, 0.02]
# A strategy that is exactly twice the benchmark on every period.
DOUBLE = [2 * b for b in BENCH]


def test_beta_of_2x_clone_is_two():
    # cov(2b, b) / var(b) = 2 * var(b) / var(b) = 2 exactly.
    assert abs(benchmark.beta(DOUBLE, BENCH) - 2.0) < 1e-12


def test_beta_of_identical_series_is_one():
    assert abs(benchmark.beta(BENCH, BENCH) - 1.0) < 1e-12


def test_beta_hand_computed():
    # strategy = [1, 2, 3], benchmark = [2, 4, 6]
    # mean_s=2, mean_b=4; cov = (-1)(-2)+(0)(0)+(1)(2) = 4
    # var_b = 4 + 0 + 4 = 8; beta = 4/8 = 0.5
    assert abs(benchmark.beta([1, 2, 3], [2, 4, 6]) - 0.5) < 1e-12


def test_beta_zero_variance_benchmark_returns_zero():
    # Flat benchmark → variance 0 → documented 0.0.
    assert benchmark.beta([0.01, -0.02, 0.03], [0.05, 0.05, 0.05]) == 0.0


def test_beta_too_few_points():
    assert benchmark.beta([0.01], [0.02]) == 0.0


def test_alpha_of_scaled_clone_is_zero():
    # A pure 2x clone of the benchmark earns nothing beyond beta exposure.
    # rf=0: per-period alpha = mean(2b) - 2*mean(b) = 0 → annualized 0.
    assert abs(benchmark.alpha(DOUBLE, BENCH)) < 1e-12


def test_alpha_hand_computed_with_constant_outperformance():
    # strategy = benchmark + 0.001 every period, rf=0.
    # beta = 1 (identical shape), mean_s - mean_b = 0.001 each period.
    # per-period alpha = 0.001 → annualized = 0.001 * 252 = 0.252.
    strat = [b + 0.001 for b in BENCH]
    expected = 0.001 * benchmark.TRADING_DAYS_PER_YEAR
    assert abs(benchmark.alpha(strat, BENCH) - expected) < 1e-9


def test_alpha_flat_benchmark_collapses_to_excess_return():
    # Flat benchmark → beta 0 → alpha = mean(strategy) annualized (rf=0).
    strat = [0.01, 0.02, 0.03]  # mean 0.02
    flat = [0.0, 0.0, 0.0]
    expected = 0.02 * benchmark.TRADING_DAYS_PER_YEAR
    assert abs(benchmark.alpha(strat, flat) - expected) < 1e-9


def test_alpha_too_few_points():
    assert benchmark.alpha([0.01], [0.02]) == 0.0


def test_tracking_error_identical_series_is_zero():
    assert benchmark.tracking_error(BENCH, BENCH) == 0.0


def test_tracking_error_hand_computed():
    # active return constant at +0.001 → pstdev of a constant is 0.
    strat = [b + 0.001 for b in BENCH]
    assert abs(benchmark.tracking_error(strat, BENCH)) < 1e-12
    # active = [0.01, 0.00] → pstdev = 0.005; annualized = 0.005*sqrt(252)
    te = benchmark.tracking_error([0.02, 0.00], [0.01, 0.00])
    expected = 0.005 * math.sqrt(benchmark.TRADING_DAYS_PER_YEAR)
    assert abs(te - expected) < 1e-12


def test_information_ratio_identical_series_is_zero():
    assert benchmark.information_ratio(BENCH, BENCH) == 0.0


def test_information_ratio_hand_computed():
    # active = [0.02, -0.02, ...] designed so mean active != 0 and TE != 0.
    strat = [0.03, -0.01]
    bench = [0.01, 0.01]
    # active = [0.02, -0.02]; mean active = 0.0 → IR numerator 0 → IR 0.
    assert abs(benchmark.information_ratio(strat, bench)) < 1e-12
    # Now a case with nonzero mean active:
    # active = [0.03, 0.01]; mean = 0.02; pstdev = 0.01
    # annual active = 0.02*252; te = 0.01*sqrt(252)
    # IR = (0.02*252)/(0.01*sqrt(252)) = 2*sqrt(252)
    strat2 = [0.04, 0.02]
    bench2 = [0.01, 0.01]
    ir = benchmark.information_ratio(strat2, bench2)
    expected = (0.02 * 252) / (0.01 * math.sqrt(252))
    assert abs(ir - expected) < 1e-9
    assert abs(ir - 2 * math.sqrt(252)) < 1e-9


def test_information_ratio_too_few_points():
    assert benchmark.information_ratio([0.01], [0.02]) == 0.0


def test_up_capture_of_2x_clone_is_two():
    assert abs(benchmark.up_capture(DOUBLE, BENCH) - 2.0) < 1e-12


def test_down_capture_of_2x_clone_is_two():
    assert abs(benchmark.down_capture(DOUBLE, BENCH) - 2.0) < 1e-12


def test_up_capture_hand_computed():
    # benchmark up days are index 0,2,4 → bench=[0.01,0.03,0.02], mean=0.02
    # strategy on those days: pick values whose mean = 0.03 → up_capture 1.5
    strat = [0.015, 0.0, 0.045, 0.0, 0.03]  # up-day vals: 0.015,0.045,0.03 mean=0.03
    assert abs(benchmark.up_capture(strat, BENCH) - 1.5) < 1e-12


def test_down_capture_cushioned_downside_below_one():
    # benchmark down days index 1,3 → bench=[-0.02,-0.01], mean=-0.015
    # strategy on those days small losses → mean -0.0075 → ratio 0.5
    strat = [0.0, -0.01, 0.0, -0.005, 0.0]  # down vals -0.01,-0.005 mean=-0.0075
    assert abs(benchmark.down_capture(strat, BENCH) - 0.5) < 1e-12


def test_up_capture_no_up_periods_returns_zero():
    assert benchmark.up_capture([-0.01, -0.02], [-0.01, -0.02]) == 0.0


def test_down_capture_no_down_periods_returns_zero():
    assert benchmark.down_capture([0.01, 0.02], [0.01, 0.02]) == 0.0


def test_length_mismatch_raises_everywhere():
    s, b = [0.01, 0.02, 0.03], [0.01, 0.02]
    for fn in (
        benchmark.beta,
        benchmark.alpha,
        benchmark.tracking_error,
        benchmark.information_ratio,
        benchmark.up_capture,
        benchmark.down_capture,
    ):
        with pytest.raises(ValueError):
            fn(s, b)


def test_empty_series_handled_gracefully():
    # Length-matched empties: ratio metrics return 0.0, captures return 0.0.
    assert benchmark.beta([], []) == 0.0
    assert benchmark.alpha([], []) == 0.0
    assert benchmark.tracking_error([], []) == 0.0
    assert benchmark.information_ratio([], []) == 0.0
    assert benchmark.up_capture([], []) == 0.0
    assert benchmark.down_capture([], []) == 0.0
