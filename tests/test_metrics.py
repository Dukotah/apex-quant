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


# ---------------------------------------------------------------------------
# Additional tests to lift coverage to ≥ 85%
# ---------------------------------------------------------------------------


def test_returns_from_equity_zero_prev_element():
    """When a prev value is 0, returns_from_equity should append 0.0 (no ZeroDivisionError)."""
    # Equity curve with a zero element mid-stream.
    rets = metrics.returns_from_equity([100.0, 0.0, 50.0])
    # First: (0 / 100) - 1 = -1.0
    assert abs(rets[0] - (-1.0)) < 1e-9
    # Second: prev=0 → yields 0.0 (guarded path)
    assert rets[1] == 0.0


def test_sharpe_too_few_returns():
    """sharpe_ratio with fewer than 2 data points must return 0.0 without raising."""
    assert metrics.sharpe_ratio([]) == 0.0
    assert metrics.sharpe_ratio([0.01]) == 0.0


def test_sortino_too_few_returns():
    """sortino_ratio with fewer than 2 data points must return 0.0 without raising."""
    assert metrics.sortino_ratio([]) == 0.0
    assert metrics.sortino_ratio([0.01]) == 0.0


def test_sortino_zero_downside():
    """
    When all returns exceed the risk-free rate the downside deviation is 0.
    sortino_ratio must return 0.0 rather than dividing by zero.
    """
    # All returns strictly positive; rf=0 → no downside samples.
    assert metrics.sortino_ratio([0.01, 0.02, 0.03, 0.04]) == 0.0


def test_max_drawdown_empty_curve():
    """max_drawdown of an empty curve must return 0.0."""
    assert metrics.max_drawdown([]) == 0.0


def test_max_drawdown_zero_peak():
    """
    If the first element is 0 (zero peak), max_drawdown must not divide by zero
    and should return 0.0 (no positive peak to draw down from).
    """
    # Curve starts at 0, peak never goes positive in the standard case —
    # the guard `if peak > 0` skips the drawdown calculation.
    assert metrics.max_drawdown([0.0, 0.0, 0.0]) == 0.0


def test_annualized_return_empty_curve():
    """annualized_return with fewer than 2 points must return 0.0."""
    assert metrics.annualized_return([]) == 0.0
    assert metrics.annualized_return([100.0]) == 0.0


def test_annualized_return_zero_start():
    """annualized_return with equity[0] == 0 must return 0.0 (guard against divide-by-zero)."""
    assert metrics.annualized_return([0.0, 100.0, 200.0]) == 0.0


def test_annualized_return_negative_growth():
    """
    When the equity curve ends at or below zero, growth <= 0 → returns -1.0
    (total loss, cannot take a power of a non-positive number).
    """
    result = metrics.annualized_return([100.0, 50.0, 0.0])
    assert result == -1.0


def test_annualized_return_positive_known_value():
    """
    25% return over 252 periods (one year) → annualized return ≈ 25%.
    Hand-computed: growth=1.25, exponent=252/252=1, result=0.25.
    """
    curve = [100.0] + [125.0] * 252  # 252 period-to-period steps
    ann = metrics.annualized_return(curve, periods_per_year=252)
    assert abs(ann - 0.25) < 1e-9


def test_calmar_ratio_zero_drawdown():
    """
    A monotonically rising equity curve has zero drawdown; calmar_ratio must
    return 0.0 rather than dividing by zero.
    """
    assert metrics.calmar_ratio([100.0, 110.0, 120.0, 130.0]) == 0.0


def test_calmar_ratio_positive_known_value():
    """
    Calmar = annualized_return / max_drawdown. With a curve of [100, 80, 120]
    over 2 periods we can hand-check: drawdown=20%, annualized return uses
    growth=1.2, exponent=252/2=126 → very high CAGR. Assert just that it's
    positive (the ratio of a positive return to a positive drawdown).
    """
    result = metrics.calmar_ratio([100.0, 80.0, 120.0])
    assert result > 0.0


def test_correlation_too_short():
    """correlation returns 0.0 when either input has fewer than 2 elements."""
    assert metrics.correlation([], [1.0, 2.0]) == 0.0
    assert metrics.correlation([1.0, 2.0], []) == 0.0
    assert metrics.correlation([1.0], [2.0]) == 0.0


def test_correlation_constant_series():
    """
    A constant series has zero variance. correlation must return 0.0 rather
    than dividing by zero (denom == 0 branch).
    """
    constant = [5.0, 5.0, 5.0, 5.0]
    varying = [1.0, 2.0, 3.0, 4.0]
    assert metrics.correlation(constant, varying) == 0.0
    assert metrics.correlation(varying, constant) == 0.0
    assert metrics.correlation(constant, constant) == 0.0


def test_profit_factor_all_losses():
    """When there are only losing trades, gross_profit=0 and the ratio is 0.0."""
    assert metrics.profit_factor([-1.0, -2.0, -0.5]) == 0.0


def test_win_rate_all_losers():
    assert metrics.win_rate([-1.0, -2.0, -3.0]) == 0.0


def test_win_rate_all_winners():
    assert metrics.win_rate([1.0, 2.0, 3.0]) == 1.0


def test_total_return_zero_start():
    """total_return with equity[0] == 0 must return 0.0 (guard against divide-by-zero)."""
    assert metrics.total_return([0.0, 100.0]) == 0.0


def test_sharpe_with_nonzero_risk_free():
    """Sharpe should deduct the per-period risk-free rate from each return."""
    # Flat 1% daily returns minus a 252% annualized rf (= 1% per day) → excess = 0 → Sharpe 0.
    daily_rf = 1.0  # 100% per year → per-period = 1.0/252 ≈ 0.00397
    rets = [0.01] * 10  # returns equal to roughly the daily rf → near-zero excess
    # Just assert it doesn't raise and returns a float.
    result = metrics.sharpe_ratio(rets, risk_free_rate=daily_rf)
    assert isinstance(result, float)


def test_sortino_with_nonzero_risk_free():
    """sortino_ratio with a nonzero risk-free rate should still run without error."""
    rets = [0.02, -0.01, 0.03, -0.02, 0.01]
    result = metrics.sortino_ratio(rets, risk_free_rate=0.05)
    assert isinstance(result, float)


# ---------------------------------------------------------------------------
# Hand-computed exact values (CLAUDE.md: metric math is tested against
# known-correct numbers, not just signs).
# ---------------------------------------------------------------------------


def test_sharpe_exact_hand_computed_value():
    """
    Sharpe = (mean / pstdev) * sqrt(252).
    rets = [0.02, 0.00, 0.04, 0.02] → mean = 0.02, pstdev = 0.01414213562373095.
    Sharpe = (0.02 / 0.01414213562373095) * sqrt(252) ≈ 22.449944320643652.
    """
    rets = [0.02, 0.00, 0.04, 0.02]
    assert abs(metrics.sharpe_ratio(rets) - 22.449944320643652) < 1e-9


def test_sortino_exact_hand_computed_value():
    """
    Sortino = (mean / downside_dev) * sqrt(252), rf = 0.
    rets = [0.02, -0.02, 0.04, 0.02] → mean = 0.015.
    downside samples = [0, -0.02, 0, 0]; downside_dev = sqrt(mean([0, 0.0004, 0, 0]))
    = sqrt(0.0001) = 0.01. Sortino = (0.015 / 0.01) * sqrt(252) ≈ 23.811761799581316.
    """
    rets = [0.02, -0.02, 0.04, 0.02]
    assert abs(metrics.sortino_ratio(rets) - 23.811761799581316) < 1e-9


def test_sharpe_risk_free_cancels_excess_to_zero():
    """
    A flat return series equal to the per-period risk-free rate has zero excess
    return → zero variance of excess → Sharpe is exactly 0.0 (the sd==0 guard).
    rf annualized = 1.0 → per-period = 1.0/252; a constant series at that level
    yields excess = 0 for every period.
    """
    per_period = 1.0 / 252
    rets = [per_period] * 8
    assert metrics.sharpe_ratio(rets, risk_free_rate=1.0) == 0.0


def test_profit_factor_exact_with_mixed_trades():
    """gross profit = 4+1 = 5, gross loss = |-2-3| = 5 → profit factor exactly 1.0."""
    assert abs(metrics.profit_factor([4.0, -2.0, 1.0, -3.0]) - 1.0) < 1e-9


def test_correlation_partial_overlap_truncates_to_shorter():
    """
    correlation truncates both series to the shorter length before computing.
    a = [1, 2, 3] vs b = [2, 4, 6, 99] → only first 3 of b are used, giving a
    perfect positive correlation of 1.0 (the trailing 99 is ignored).
    """
    a = [1.0, 2.0, 3.0]
    b = [2.0, 4.0, 6.0, 99.0]
    assert abs(metrics.correlation(a, b) - 1.0) < 1e-9
