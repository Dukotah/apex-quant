"""Tests for apex.risk.trade_risk_reward — hand-computed R-multiple,
risk/reward ratio and expectancy values, plus degenerate-geometry edges."""
from __future__ import annotations

from decimal import Decimal

from apex.core.models import OrderSide
from apex.risk.trade_risk_reward import (
    ExpectancySummary,
    expectancy,
    r_multiple,
    reward_per_unit,
    risk_per_unit,
    risk_reward_ratio,
)

BUY = OrderSide.BUY
SELL = OrderSide.SELL
D = Decimal


# --------------------------------------------------------------------------
# risk_per_unit / reward_per_unit
# --------------------------------------------------------------------------

def test_risk_per_unit_long():
    # entry 100, stop 90 -> risk 10
    assert risk_per_unit(D("100"), D("90"), BUY) == D("10")


def test_risk_per_unit_short():
    # short entry 100, stop 110 -> risk 10
    assert risk_per_unit(D("100"), D("110"), SELL) == D("10")


def test_risk_per_unit_wrong_side_long_is_none():
    # long with stop ABOVE entry is invalid geometry
    assert risk_per_unit(D("100"), D("110"), BUY) is None


def test_risk_per_unit_wrong_side_short_is_none():
    assert risk_per_unit(D("100"), D("90"), SELL) is None


def test_risk_per_unit_zero_distance_is_none():
    assert risk_per_unit(D("100"), D("100"), BUY) is None


def test_reward_per_unit_long():
    # entry 100, target 130 -> reward 30
    assert reward_per_unit(D("100"), D("130"), BUY) == D("30")


def test_reward_per_unit_short():
    # short entry 100, target 70 -> reward 30
    assert reward_per_unit(D("100"), D("70"), SELL) == D("30")


def test_reward_per_unit_wrong_side_is_none():
    assert reward_per_unit(D("100"), D("90"), BUY) is None
    assert reward_per_unit(D("100"), D("110"), SELL) is None


def test_accepts_float_and_str_inputs():
    # coercion through str(): 100.5 -> 90.5 gives risk 10.0 exactly
    assert risk_per_unit(100.5, 90.5, BUY) == D("10.0")
    assert reward_per_unit("100", "130", BUY) == D("30")


# --------------------------------------------------------------------------
# risk_reward_ratio
# --------------------------------------------------------------------------

def test_rrr_long_2to1():
    # entry 100, stop 90 (risk 10), target 120 (reward 20) -> 2.0
    assert risk_reward_ratio(D("100"), D("90"), D("120"), BUY) == D("2")


def test_rrr_short_3to1():
    # short entry 100, stop 110 (risk 10), target 70 (reward 30) -> 3.0
    assert risk_reward_ratio(D("100"), D("110"), D("70"), SELL) == D("3")


def test_rrr_invalid_risk_is_none():
    # stop on wrong side -> risk undefined -> None
    assert risk_reward_ratio(D("100"), D("110"), D("120"), BUY) is None


def test_rrr_invalid_reward_is_none():
    # target on wrong side -> reward undefined -> None
    assert risk_reward_ratio(D("100"), D("90"), D("80"), BUY) is None


# --------------------------------------------------------------------------
# r_multiple
# --------------------------------------------------------------------------

def test_r_multiple_long_win():
    # entry 100, stop 90 (R=10), exit 120 -> +2R
    assert r_multiple(D("100"), D("90"), D("120"), BUY) == D("2")


def test_r_multiple_long_stop_hit():
    # exit at the stop -> exactly -1R
    assert r_multiple(D("100"), D("90"), D("90"), BUY) == D("-1")


def test_r_multiple_long_gap_through_stop():
    # exit 85 below the 90 stop -> worse than -1R
    assert r_multiple(D("100"), D("90"), D("85"), BUY) == D("-1.5")


def test_r_multiple_short_win():
    # short entry 100, stop 110 (R=10), exit 80 -> +2R
    assert r_multiple(D("100"), D("110"), D("80"), SELL) == D("2")


def test_r_multiple_short_loss():
    # short entry 100, stop 110, exit 105 -> -0.5R
    assert r_multiple(D("100"), D("110"), D("105"), SELL) == D("-0.5")


def test_r_multiple_invalid_risk_is_none():
    assert r_multiple(D("100"), D("100"), D("120"), BUY) is None


# --------------------------------------------------------------------------
# expectancy
# --------------------------------------------------------------------------

def test_expectancy_basic():
    # Rs: +2, -1, +2, -1  -> total 2, mean 0.5
    rs = [D("2"), D("-1"), D("2"), D("-1")]
    s = expectancy(rs)
    assert isinstance(s, ExpectancySummary)
    assert s.expectancy == D("0.5")
    assert s.total_r == D("2")
    assert s.trade_count == 4
    assert s.win_rate == D("0.5")
    assert s.avg_win_r == D("2")
    assert s.avg_loss_r == D("-1")


def test_expectancy_all_losses():
    rs = [D("-1"), D("-1"), D("-2")]
    s = expectancy(rs)
    assert s.expectancy == D("-4") / D("3")
    assert s.win_rate == D("0")
    assert s.avg_win_r == D("0")
    assert s.avg_loss_r == D("-4") / D("3")


def test_expectancy_skips_none_entries():
    # None (undefined-geometry trade) is dropped; mean over the 2 valid Rs
    s = expectancy([D("3"), None, D("-1")])
    assert s.trade_count == 2
    assert s.expectancy == D("1")


def test_expectancy_empty_is_none():
    assert expectancy([]) is None


def test_expectancy_all_none_is_none():
    assert expectancy([None, None]) is None


def test_expectancy_breakeven_trade_not_counted_as_win_or_loss():
    # a 0R trade counts toward trade_count but not wins or losses
    s = expectancy([D("2"), D("0"), D("-1")])
    assert s.trade_count == 3
    assert s.win_rate == D("1") / D("3")
    assert s.avg_win_r == D("2")
    assert s.avg_loss_r == D("-1")
    assert s.expectancy == D("1") / D("3")


# --------------------------------------------------------------------------
# Cross-check: r_multiple at target equals the planned RRR
# --------------------------------------------------------------------------

def test_r_multiple_at_target_equals_rrr():
    entry, stop, target = D("100"), D("90"), D("125")
    rrr = risk_reward_ratio(entry, stop, target, BUY)
    realized = r_multiple(entry, stop, target, BUY)
    assert rrr == realized == D("2.5")
