"""
Tests for apex.analytics.rolling_beta — verified against hand-computed values.

Beta = Cov(strategy, benchmark) / Var(benchmark), using population moments
(matching the manual computations below).
"""
from __future__ import annotations

from apex.analytics.rolling_beta import beta, latest_beta, rolling_beta


def test_beta_perfect_one():
    # strategy == benchmark → beta exactly 1.0.
    b = [0.01, -0.02, 0.03, 0.00, -0.01]
    assert abs(beta(b, b) - 1.0) < 1e-12


def test_beta_double_benchmark():
    # strategy = 2 * benchmark → beta exactly 2.0.
    bench = [0.01, -0.02, 0.03, 0.00, -0.01]
    strat = [2 * x for x in bench]
    assert abs(beta(strat, bench) - 2.0) < 1e-12


def test_beta_negative():
    # strategy = -benchmark → beta exactly -1.0.
    bench = [0.01, -0.02, 0.03, 0.00, -0.01]
    strat = [-x for x in bench]
    assert abs(beta(strat, bench) - (-1.0)) < 1e-12


def test_beta_hand_computed():
    # bench = [1, 2, 3], mean 2; strat = [2, 2, 5], mean 3.
    # Cov = (1-2)(2-3)+(2-2)(2-3)+(3-2)(5-3) = 1 + 0 + 2 = 3
    # Var_b = 1 + 0 + 1 = 2 → beta = 1.5
    assert abs(beta([2, 2, 5], [1, 2, 3]) - 1.5) < 1e-12


def test_beta_insufficient_data():
    assert beta([], []) is None
    assert beta([0.01], [0.01]) is None


def test_beta_zero_variance_benchmark():
    # Benchmark flat → variance 0 → undefined.
    assert beta([0.01, -0.02, 0.03], [0.0, 0.0, 0.0]) is None


def test_beta_aligns_to_shorter():
    # Extra benchmark points beyond strategy length are ignored.
    assert abs(beta([2, 2, 5], [1, 2, 3, 99, 100]) - 1.5) < 1e-12


def test_rolling_beta_shape_and_warmup():
    bench = [0.01, -0.02, 0.03, 0.00, -0.01]
    strat = [2 * x for x in bench]
    res = rolling_beta(strat, bench, window=3)
    assert len(res) == 5
    # First window-1 entries are None (warmup).
    assert res[0] is None
    assert res[1] is None
    # Every full window of strat = 2*bench gives beta 2.0.
    for v in res[2:]:
        assert v is not None
        assert abs(v - 2.0) < 1e-12


def test_rolling_beta_window_values():
    # bench windows of size 3 over [1,2,3,4,5]; strat chosen so each window
    # has a known beta. Use strat = bench → every window beta 1.0.
    bench = [1.0, 2.0, 3.0, 4.0, 5.0]
    res = rolling_beta(bench, bench, window=3)
    assert res[0] is None and res[1] is None
    for v in res[2:]:
        assert abs(v - 1.0) < 1e-12


def test_rolling_beta_zero_variance_window():
    # A flat benchmark window yields None at those positions.
    strat = [0.01, 0.02, 0.03, 0.04]
    bench = [5.0, 5.0, 5.0, 0.0]
    res = rolling_beta(strat, bench, window=3)
    # window [5,5,5] flat → None; window [5,5,0] has variance → a number.
    assert res[2] is None
    assert res[3] is not None


def test_rolling_beta_too_short_or_bad_window():
    assert rolling_beta([0.01, 0.02], [0.01, 0.02], window=3) == []
    assert rolling_beta([0.01, 0.02, 0.03], [0.01, 0.02, 0.03], window=1) == []
    assert rolling_beta([0.01, 0.02, 0.03], [0.01, 0.02, 0.03], window=0) == []


def test_rolling_beta_aligns_to_shorter():
    bench = [1.0, 2.0, 3.0, 4.0]
    strat = [1.0, 2.0, 3.0]  # shorter
    res = rolling_beta(strat, bench, window=3)
    assert len(res) == 3
    assert res[0] is None and res[1] is None
    assert abs(res[2] - 1.0) < 1e-12


def test_latest_beta():
    bench = [1.0, 2.0, 3.0, 1.0, 2.0, 3.0]
    strat = [2.0, 2.0, 5.0, 2.0, 2.0, 5.0]
    # Last 3 paired: strat [2,2,5], bench [1,2,3] → beta 1.5.
    assert abs(latest_beta(strat, bench, window=3) - 1.5) < 1e-12


def test_latest_beta_insufficient():
    assert latest_beta([0.01, 0.02], [0.01, 0.02], window=3) is None
    assert latest_beta([0.01, 0.02, 0.03], [0.01, 0.02, 0.03], window=1) is None


def test_latest_beta_zero_variance():
    assert latest_beta([0.01, 0.02, 0.03], [5.0, 5.0, 5.0], window=3) is None
