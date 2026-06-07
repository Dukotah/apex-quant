"""Tests for apex.validation.regime_split_metrics."""
from __future__ import annotations

import math

import pytest

from apex.validation import metrics
from apex.validation.regime_split_metrics import (
    RegimeMetrics,
    metrics_for_returns,
    regime_split_metrics,
    split_returns_by_regime,
)


def test_split_groups_and_preserves_order():
    returns = [0.01, -0.02, 0.03, 0.04, -0.05]
    regimes = ["bull", "bear", "bull", "bull", "bear"]
    buckets = split_returns_by_regime(returns, regimes)
    assert buckets == {
        "bull": [0.01, 0.03, 0.04],
        "bear": [-0.02, -0.05],
    }


def test_split_empty_inputs_returns_empty():
    assert split_returns_by_regime([], []) == {}


def test_split_length_mismatch_fails_closed():
    assert split_returns_by_regime([0.1, 0.2], ["a"]) == {}


def test_regime_split_returns_one_entry_per_distinct_regime():
    returns = [0.01, -0.02, 0.03, 0.04, -0.05]
    regimes = ["bull", "bear", "bull", "bull", "bear"]
    result = regime_split_metrics(returns, regimes)
    assert set(result.keys()) == {"bull", "bear"}
    assert isinstance(result["bull"], RegimeMetrics)


def test_fraction_sums_to_one_and_counts_correct():
    returns = [0.01, -0.02, 0.03, 0.04, -0.05]
    regimes = ["bull", "bear", "bull", "bull", "bear"]
    result = regime_split_metrics(returns, regimes)
    assert result["bull"].n_periods == 3
    assert result["bear"].n_periods == 2
    assert result["bull"].fraction == pytest.approx(3 / 5)
    assert result["bear"].fraction == pytest.approx(2 / 5)
    total_frac = sum(m.fraction for m in result.values())
    assert total_frac == pytest.approx(1.0)


def test_hand_computed_bull_bucket_metrics():
    # bull returns: [0.01, 0.03, 0.04]
    returns = [0.01, -0.02, 0.03, 0.04, -0.05]
    regimes = ["bull", "bear", "bull", "bull", "bear"]
    result = regime_split_metrics(returns, regimes)
    bull = result["bull"]

    bull_r = [0.01, 0.03, 0.04]
    # total return via compounding: 1.01 * 1.03 * 1.04 - 1
    expected_total = 1.01 * 1.03 * 1.04 - 1.0
    assert bull.total_return == pytest.approx(expected_total)

    # mean return
    assert bull.mean_return == pytest.approx(sum(bull_r) / 3)

    # all positive -> win rate 1.0, profit factor inf (no losses)
    assert bull.win_rate == pytest.approx(1.0)
    assert math.isinf(bull.profit_factor)

    # max drawdown of a monotonically increasing curve is 0
    assert bull.max_drawdown == pytest.approx(0.0)

    # sharpe should match metrics.sharpe_ratio on the same returns
    assert bull.sharpe_ratio == pytest.approx(metrics.sharpe_ratio(bull_r))
    assert bull.sortino_ratio == pytest.approx(metrics.sortino_ratio(bull_r))


def test_hand_computed_bear_bucket_metrics():
    returns = [0.01, -0.02, 0.03, 0.04, -0.05]
    regimes = ["bull", "bear", "bull", "bull", "bear"]
    result = regime_split_metrics(returns, regimes)
    bear = result["bear"]

    bear_r = [-0.02, -0.05]
    expected_total = (1 - 0.02) * (1 - 0.05) - 1.0
    assert bear.total_return == pytest.approx(expected_total)
    assert bear.win_rate == pytest.approx(0.0)
    # all losses -> profit factor 0.0 (no gross profit)
    assert bear.profit_factor == pytest.approx(0.0)
    # drawdown is positive (declining curve)
    assert bear.max_drawdown > 0.0

    # sharpe/sortino should match metrics on the same returns
    assert bear.sharpe_ratio == pytest.approx(metrics.sharpe_ratio(bear_r))
    assert bear.sortino_ratio == pytest.approx(metrics.sortino_ratio(bear_r))


def test_empty_inputs_returns_empty_dict():
    assert regime_split_metrics([], []) == {}


def test_length_mismatch_returns_empty_dict():
    assert regime_split_metrics([0.1, 0.2, 0.3], ["a", "b"]) == {}


def test_single_period_regime_yields_neutral_ratios():
    # one period: sharpe/sortino need >=2 points -> 0.0; total_return still computed
    result = regime_split_metrics([0.05], ["only"])
    m = result["only"]
    assert m.n_periods == 1
    assert m.fraction == pytest.approx(1.0)
    assert m.total_return == pytest.approx(0.05)
    assert m.sharpe_ratio == 0.0
    assert m.sortino_ratio == 0.0
    assert m.win_rate == pytest.approx(1.0)


def test_metrics_for_empty_returns_all_neutral():
    m = metrics_for_returns("x", [], total_periods=0)
    assert m.n_periods == 0
    assert m.fraction == 0.0
    assert m.total_return == 0.0
    assert m.annualized_return == 0.0
    assert m.sharpe_ratio == 0.0
    assert m.sortino_ratio == 0.0
    assert m.max_drawdown == 0.0
    assert m.win_rate == 0.0
    assert m.profit_factor == 0.0
    assert m.mean_return == 0.0


def test_integer_regime_labels_supported():
    returns = [0.1, -0.1, 0.2, -0.2]
    regimes = [0, 1, 0, 1]
    result = regime_split_metrics(returns, regimes)
    assert set(result.keys()) == {0, 1}
    assert result[0].n_periods == 2
    assert result[1].n_periods == 2


def test_determinism_same_inputs_same_outputs():
    returns = [0.01, -0.02, 0.03, 0.04, -0.05, 0.02]
    regimes = ["a", "b", "a", "b", "a", "b"]
    r1 = regime_split_metrics(returns, regimes)
    r2 = regime_split_metrics(returns, regimes)
    assert r1 == r2


def test_summary_contains_regime_and_sharpe():
    result = regime_split_metrics([0.01, 0.02, -0.01], ["calm", "calm", "calm"])
    s = result["calm"].summary()
    assert "calm" in s
    assert "Sharpe" in s


def test_periods_per_year_affects_annualization():
    returns = [0.01, 0.02, 0.03, 0.04]
    regimes = ["x", "x", "x", "x"]
    low = regime_split_metrics(returns, regimes, periods_per_year=12)
    high = regime_split_metrics(returns, regimes, periods_per_year=252)
    # higher annualization factor -> larger annualized return for positive curve
    assert high["x"].annualized_return > low["x"].annualized_return
    # total return is independent of annualization
    assert low["x"].total_return == pytest.approx(high["x"].total_return)
