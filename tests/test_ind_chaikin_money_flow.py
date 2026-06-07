"""Tests for apex.strategy.ind_chaikin_money_flow against hand-computed values."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_chaikin_money_flow import (
    chaikin_money_flow,
    money_flow_multiplier,
)


def test_money_flow_multiplier_known_values():
    # Close at high -> MFM = +1; close at low -> MFM = -1; midpoint -> 0.
    high = [10.0, 10.0, 10.0]
    low = [8.0, 8.0, 8.0]
    close = [10.0, 8.0, 9.0]
    assert money_flow_multiplier(high, low, close) == [1.0, -1.0, 0.0]


def test_money_flow_multiplier_quarter():
    # range 4, close 1 above low: ((1) - (3)) / 4 = -0.5
    assert money_flow_multiplier([14.0], [10.0], [11.0]) == [-0.5]


def test_money_flow_multiplier_zero_range_is_zero():
    # high == low -> undefined ratio, convention 0.0 (no division by zero).
    assert money_flow_multiplier([5.0], [5.0], [5.0]) == [0.0]


def test_cmf_warmup_is_none():
    high = [10.0, 11.0]
    low = [8.0, 9.0]
    close = [9.0, 10.0]
    volume = [100.0, 100.0]
    out = chaikin_money_flow(high, low, close, volume, period=3)
    assert out == [None, None]


def test_cmf_single_window_hand_computed():
    # period == n == 2. Bar0: MFM=+1, vol=100 -> MFV=100.
    # Bar1: MFM=-1, vol=300 -> MFV=-300. sum MFV=-200, sum vol=400 -> -0.5.
    high = [10.0, 10.0]
    low = [8.0, 8.0]
    close = [10.0, 8.0]
    volume = [100.0, 300.0]
    out = chaikin_money_flow(high, low, close, volume, period=2)
    assert out[0] is None
    assert out[1] == pytest.approx(-0.5)


def test_cmf_rolling_window():
    # period 2 over 3 bars.
    # MFMs: bar0 +1, bar1 -1, bar2 0.
    high = [10.0, 10.0, 10.0]
    low = [8.0, 8.0, 8.0]
    close = [10.0, 8.0, 9.0]
    volume = [100.0, 100.0, 100.0]
    out = chaikin_money_flow(high, low, close, volume, period=2)
    # window [0,1]: (100 - 100) / 200 = 0.0
    # window [1,2]: (-100 + 0) / 200 = -0.5
    assert out[0] is None
    assert out[1] == pytest.approx(0.0)
    assert out[2] == pytest.approx(-0.5)


def test_cmf_zero_volume_window_is_none():
    high = [10.0, 10.0]
    low = [8.0, 8.0]
    close = [10.0, 8.0]
    volume = [0.0, 0.0]
    out = chaikin_money_flow(high, low, close, volume, period=2)
    assert out[1] is None


def test_cmf_accepts_decimal_inputs():
    high = [Decimal("10"), Decimal("10")]
    low = [Decimal("8"), Decimal("8")]
    close = [Decimal("10"), Decimal("8")]
    volume = [Decimal("100"), Decimal("300")]
    out = chaikin_money_flow(high, low, close, volume, period=2)
    assert out[1] == pytest.approx(-0.5)


def test_cmf_bounds():
    # All bars closing at the high -> CMF should be +1 (max accumulation).
    high = [10.0, 12.0, 15.0, 9.0]
    low = [8.0, 9.0, 11.0, 5.0]
    close = [10.0, 12.0, 15.0, 9.0]
    volume = [100.0, 200.0, 50.0, 400.0]
    out = chaikin_money_flow(high, low, close, volume, period=4)
    assert out[3] == pytest.approx(1.0)


def test_cmf_empty_input():
    assert chaikin_money_flow([], [], [], [], period=20) == []


def test_cmf_invalid_period_raises():
    with pytest.raises(ValueError):
        chaikin_money_flow([1.0], [1.0], [1.0], [1.0], period=0)


def test_cmf_mismatched_lengths_raises():
    with pytest.raises(ValueError):
        chaikin_money_flow([1.0, 2.0], [1.0], [1.0], [1.0], period=1)


def test_cmf_output_length_matches_input():
    n = 10
    high = [float(i + 2) for i in range(n)]
    low = [float(i) for i in range(n)]
    close = [float(i + 1) for i in range(n)]
    volume = [100.0] * n
    out = chaikin_money_flow(high, low, close, volume, period=5)
    assert len(out) == n
    assert out[:4] == [None, None, None, None]
