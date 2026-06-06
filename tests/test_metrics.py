"""
Tests for apex.validation.metrics — verified against hand-computed values.
A metric you can't trust is worse than no metric. These lock in correctness.
"""

from __future__ import annotations

import math

from apex.validation import metrics


def test_total_return():
    assert abs(metrics.total_return([100, 110]) - 0.10) < 1e-9
    assert abs(metrics.total_return([100, 50]) - (-0.50)) < 1e-9
    assert metrics.total_return([100]) == 0.0  # too short
    assert metrics.total_return([]) == 0.0


def test_returns_from_equity():
    rets = metrics.returns_from_equity([100, 110, 99])
    assert abs(rets[0] - 0.10) < 1e-9
    assert abs(rets[1] - (-0.10)) < 1e-9


def test_max_drawdown():
    # Peak 120, trough 60 → 50% drawdown.
    curve = [100, 120, 90, 60, 80]
    assert abs(metrics.max_drawdown(curve) - 0.50) < 1e-9
    # Monotonic up → zero drawdown.
    assert metrics.max_drawdown([100, 110, 120, 130]) == 0.0


def test_profit_factor():
    # gross profit 3+2=5, gross loss 1+1=2 → 2.5
    assert abs(metrics.profit_factor([3, -1, 2, -1]) - 2.5) < 1e-9
    # no losses → inf
    assert metrics.profit_factor([1, 2, 3]) == math.inf
    # no trades → 0
    assert metrics.profit_factor([]) == 0.0


def test_win_rate():
    assert metrics.win_rate([1, -1, 1, 1]) == 0.75
    assert metrics.win_rate([]) == 0.0


def test_sharpe_zero_variance():
    # Constant returns → no variance → Sharpe 0 (not a crash, not infinity).
    assert metrics.sharpe_ratio([0.01, 0.01, 0.01]) == 0.0


def test_sharpe_positive_edge():
    # A series with positive mean and modest variance → positive Sharpe.
    rets = [0.01, 0.02, -0.005, 0.015, 0.008, -0.002, 0.012]
    s = metrics.sharpe_ratio(rets)
    assert s > 0


def test_sharpe_sign():
    # Net-negative returns → negative Sharpe.
    rets = [-0.02, -0.01, 0.005, -0.015, -0.008]
    assert metrics.sharpe_ratio(rets) < 0


def test_sortino_ignores_upside():
    # Big upside spikes shouldn't be penalized like downside.
    calm = [0.01, 0.01, -0.01, 0.01, -0.01]
    spiky_up = [0.01, 0.10, -0.01, 0.01, -0.01]
    assert metrics.sortino_ratio(spiky_up) >= metrics.sortino_ratio(calm)


def test_correlation():
    # Perfectly correlated.
    a = [1, 2, 3, 4]
    b = [2, 4, 6, 8]
    assert abs(metrics.correlation(a, b) - 1.0) < 1e-9
    # Perfectly anti-correlated.
    c = [4, 3, 2, 1]
    assert abs(metrics.correlation(a, c) - (-1.0)) < 1e-9


def test_annualized_return():
    # Doubling over exactly one year (252 daily bars) → ~100% annualized.
    curve = [1.0] + [1.0 * (2 ** (i / 252)) for i in range(1, 253)]
    ann = metrics.annualized_return(curve)
    assert abs(ann - 1.0) < 0.05  # ~100%
