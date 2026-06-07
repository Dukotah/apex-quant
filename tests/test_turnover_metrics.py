"""Tests for apex.validation.turnover_metrics — hand-computed values + edges."""

from __future__ import annotations

import math

from apex.validation.turnover_metrics import (
    TurnoverReport,
    annualized_turnover,
    average_turnover,
    implied_holding_period,
    one_way_turnover,
    turnover_report,
    turnover_series,
)

# --- one_way_turnover -------------------------------------------------------


def test_one_way_turnover_no_change_is_zero():
    w = {"A": 0.5, "B": 0.5}
    assert one_way_turnover(w, w) == 0.0


def test_one_way_turnover_full_swap():
    # Sell all of A, buy all of B: L1 = |0-1| + |1-0| = 2, half = 1.0.
    prev = {"A": 1.0}
    curr = {"B": 1.0}
    assert one_way_turnover(prev, curr) == 1.0


def test_one_way_turnover_partial_rebalance():
    # A 0.6->0.4 (0.2), B 0.4->0.6 (0.2): L1 = 0.4, half = 0.2.
    prev = {"A": 0.6, "B": 0.4}
    curr = {"A": 0.4, "B": 0.6}
    assert math.isclose(one_way_turnover(prev, curr), 0.2)


def test_one_way_turnover_missing_asset_treated_as_zero():
    # A 0.5->0.5 (0), B 0.5->0 (0.5), C 0->0.5 (0.5): L1 = 1.0, half = 0.5.
    prev = {"A": 0.5, "B": 0.5}
    curr = {"A": 0.5, "C": 0.5}
    assert one_way_turnover(prev, curr) == 0.5


def test_one_way_turnover_positional_sequences():
    # Same as partial rebalance but positional.
    assert math.isclose(one_way_turnover([0.6, 0.4], [0.4, 0.6]), 0.2)


def test_one_way_turnover_handles_shorts():
    # +0.5 -> -0.5 is a 1.0 swing on that leg: L1 = 1.0, half = 0.5.
    assert one_way_turnover({"A": 0.5}, {"A": -0.5}) == 0.5


# --- turnover_series --------------------------------------------------------


def test_turnover_series_length_is_n_minus_one():
    vectors = [{"A": 1.0}, {"A": 0.5, "B": 0.5}, {"A": 1.0}]
    series = turnover_series(vectors)
    assert len(series) == 2
    # transition 1: A 1->0.5 (0.5), B 0->0.5 (0.5) -> 0.5
    # transition 2: A 0.5->1 (0.5), B 0.5->0 (0.5) -> 0.5
    assert series == [0.5, 0.5]


def test_turnover_series_insufficient_data():
    assert turnover_series([]) == []
    assert turnover_series([{"A": 1.0}]) == []


# --- average / annualized ---------------------------------------------------


def test_average_turnover_known():
    vectors = [{"A": 1.0}, {"A": 0.8, "B": 0.2}, {"A": 1.0}]
    # t1: 0.2, t2: 0.2 -> avg 0.2
    assert math.isclose(average_turnover(vectors), 0.2)


def test_average_turnover_insufficient_is_zero():
    assert average_turnover([{"A": 1.0}]) == 0.0


def test_annualized_turnover_scales():
    vectors = [{"A": 1.0}, {"A": 0.9, "B": 0.1}]  # turnover 0.1
    assert math.isclose(annualized_turnover(vectors, periods_per_year=252), 252 * 0.1)


# --- implied_holding_period -------------------------------------------------


def test_implied_holding_period_known():
    # avg turnover 0.25 -> holding period 4 periods.
    vectors = [{"A": 1.0}, {"A": 0.75, "B": 0.25}]
    assert implied_holding_period(vectors) == 4.0


def test_implied_holding_period_no_trading_is_inf():
    vectors = [{"A": 1.0}, {"A": 1.0}, {"A": 1.0}]
    assert implied_holding_period(vectors) == math.inf


def test_implied_holding_period_insufficient_is_inf():
    assert implied_holding_period([{"A": 1.0}]) == math.inf


# --- turnover_report --------------------------------------------------------


def test_turnover_report_known_values():
    vectors = [
        {"A": 1.0},
        {"A": 0.8, "B": 0.2},  # turnover 0.2
        {"A": 1.0},  # turnover 0.2
        {"A": 0.6, "B": 0.4},  # turnover 0.4
    ]
    rep = turnover_report(vectors, periods_per_year=252)
    assert isinstance(rep, TurnoverReport)
    assert rep.n_periods == 3
    # avg = (0.2 + 0.2 + 0.4) / 3
    assert rep.average_turnover == (0.2 + 0.2 + 0.4) / 3
    assert rep.max_turnover == 0.4
    assert math.isclose(rep.min_turnover, 0.2)
    assert math.isclose(rep.annualized_turnover, rep.average_turnover * 252)
    assert math.isclose(rep.implied_holding_period, 1.0 / rep.average_turnover)


def test_turnover_report_insufficient_data():
    rep = turnover_report([{"A": 1.0}])
    assert rep.n_periods == 0
    assert rep.average_turnover == 0.0
    assert rep.annualized_turnover == 0.0
    assert rep.implied_holding_period == math.inf
    assert rep.max_turnover == 0.0
    assert rep.min_turnover == 0.0


def test_turnover_report_no_trading():
    rep = turnover_report([{"A": 1.0}, {"A": 1.0}])
    assert rep.n_periods == 1
    assert rep.average_turnover == 0.0
    assert rep.implied_holding_period == math.inf


def test_turnover_report_summary_is_str():
    rep = turnover_report([{"A": 1.0}, {"A": 0.5, "B": 0.5}])
    s = rep.summary()
    assert isinstance(s, str)
    assert "Turnover" in s


def test_turnover_report_summary_handles_inf_holding_period():
    rep = turnover_report([{"A": 1.0}, {"A": 1.0}])
    assert "inf" in rep.summary()


# --- determinism ------------------------------------------------------------


def test_deterministic():
    vectors = [{"A": 0.5, "B": 0.5}, {"A": 0.3, "B": 0.7}, {"A": 0.6, "B": 0.4}]
    a = turnover_report(vectors)
    b = turnover_report(vectors)
    assert a == b
