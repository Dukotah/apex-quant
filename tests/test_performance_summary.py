"""Tests for apex.analytics.performance_summary."""

from __future__ import annotations

import math

from apex.analytics.performance_summary import (
    SUMMARY_KEYS,
    equity_curve_from_returns,
    performance_summary,
)
from apex.validation.metrics import (
    sharpe_ratio,
    sortino_ratio,
)


def test_equity_curve_compounds_from_returns():
    curve = equity_curve_from_returns([0.10, -0.05, 0.20])
    assert curve[0] == 1.0
    assert math.isclose(curve[1], 1.10)
    assert math.isclose(curve[2], 1.045)
    assert math.isclose(curve[3], 1.254)


def test_equity_curve_custom_start():
    curve = equity_curve_from_returns([0.0, 0.0], starting_equity=100.0)
    assert curve == [100.0, 100.0, 100.0]


def test_equity_curve_empty_is_single_point():
    assert equity_curve_from_returns([]) == [1.0]


def test_summary_hand_computed_values():
    returns = [0.10, -0.05, 0.20]
    s = performance_summary(returns)

    # curve = [1.0, 1.1, 1.045, 1.254]
    assert math.isclose(s["total_return"], 0.254, rel_tol=1e-12)
    assert math.isclose(s["win_rate"], 2.0 / 3.0)
    # worst drawdown: peak 1.1 -> trough 1.045 => 0.05
    assert math.isclose(s["max_drawdown"], 0.05, rel_tol=1e-12)
    assert s["num_periods"] == 3.0

    # Sharpe / Sortino must match metrics.py exactly (pure delegation).
    assert math.isclose(s["sharpe_ratio"], sharpe_ratio(returns))
    assert math.isclose(s["sortino_ratio"], sortino_ratio(returns))


def test_summary_calmar_consistency():
    returns = [0.10, -0.05, 0.20]
    s = performance_summary(returns)
    # Calmar = annualized_return / max_drawdown.
    expected = s["annualized_return"] / s["max_drawdown"]
    assert math.isclose(s["calmar_ratio"], expected, rel_tol=1e-12)


def test_summary_has_all_keys():
    s = performance_summary([0.01, 0.02])
    assert set(s.keys()) == set(SUMMARY_KEYS)
    assert all(isinstance(v, float) for v in s.values())


def test_summary_empty_returns_all_zeros():
    s = performance_summary([])
    assert s["total_return"] == 0.0
    assert s["annualized_return"] == 0.0
    assert s["sharpe_ratio"] == 0.0
    assert s["sortino_ratio"] == 0.0
    assert s["max_drawdown"] == 0.0
    assert s["calmar_ratio"] == 0.0
    assert s["win_rate"] == 0.0
    assert s["num_periods"] == 0.0


def test_summary_single_period_graceful():
    # One return -> two-point curve, but <2 points for Sharpe/Sortino -> 0.0.
    s = performance_summary([0.05])
    assert math.isclose(s["total_return"], 0.05)
    assert s["sharpe_ratio"] == 0.0
    assert s["sortino_ratio"] == 0.0
    assert s["win_rate"] == 1.0
    assert s["num_periods"] == 1.0


def test_summary_no_drawdown_calmar_zero():
    # Monotonically rising -> zero drawdown -> calmar fails closed to 0.0.
    s = performance_summary([0.01, 0.01, 0.01])
    assert s["max_drawdown"] == 0.0
    assert s["calmar_ratio"] == 0.0


def test_summary_all_losses_win_rate_zero():
    s = performance_summary([-0.01, -0.02, -0.03])
    assert s["win_rate"] == 0.0
    assert s["total_return"] < 0.0
    assert s["max_drawdown"] > 0.0
