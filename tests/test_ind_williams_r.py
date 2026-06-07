"""Tests for apex.strategy.ind_williams_r — hand-computed values + edge cases."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_williams_r import williams_r


def test_warmup_is_none_then_full_length():
    highs = [10, 11, 12, 13]
    lows = [8, 9, 10, 11]
    closes = [9, 10, 11, 12]
    out = williams_r(highs, lows, closes, period=3)
    assert len(out) == len(closes)
    # First period-1 = 2 indices are warmup.
    assert out[0] is None
    assert out[1] is None
    assert out[2] is not None
    assert out[3] is not None


def test_known_value_close_at_top():
    # Window of 3 bars. At i=2: highs window=[10,11,12]->HH=12,
    # lows window=[8,9,10]->LL=8, close=12 (at the top).
    # %R = (12 - 12) / (12 - 8) * -100 = 0.0
    highs = [10, 11, 12]
    lows = [8, 9, 10]
    closes = [9, 10, 12]
    out = williams_r(highs, lows, closes, period=3)
    assert out[2] == pytest.approx(0.0)


def test_known_value_close_at_bottom():
    # HH=12, LL=8, close=8 (at the bottom).
    # %R = (12 - 8) / (12 - 8) * -100 = -100.0
    highs = [10, 11, 12]
    lows = [8, 9, 8]
    closes = [9, 10, 8]
    out = williams_r(highs, lows, closes, period=3)
    assert out[2] == pytest.approx(-100.0)


def test_known_value_midpoint():
    # HH=12, LL=8, close=10 (midpoint).
    # %R = (12 - 10) / (12 - 8) * -100 = -50.0
    highs = [10, 11, 12]
    lows = [8, 9, 8]
    closes = [9, 10, 10]
    out = williams_r(highs, lows, closes, period=3)
    assert out[2] == pytest.approx(-50.0)


def test_rolling_window_uses_only_last_period():
    # period=2. At i=3: window highs=[14,13]->HH=14? no, highs[2:4].
    highs = [10, 20, 14, 13]
    lows = [5, 6, 7, 8]
    closes = [7, 8, 10, 9]
    out = williams_r(highs, lows, closes, period=2)
    # i=3: window indices 2..3 -> HH=max(14,13)=14, LL=min(7,8)=7, close=9
    # %R = (14 - 9) / (14 - 7) * -100 = -71.4285...
    assert out[3] == pytest.approx((14 - 9) / (14 - 7) * -100.0)
    # The spike high of 20 at index 1 must NOT affect index 3.


def test_flat_range_returns_zero():
    # high == low == close across the window -> denominator 0 -> 0.0
    highs = [5, 5, 5]
    lows = [5, 5, 5]
    closes = [5, 5, 5]
    out = williams_r(highs, lows, closes, period=2)
    assert out[1] == 0.0
    assert out[2] == 0.0


def test_insufficient_data_all_none():
    highs = [10, 11]
    lows = [8, 9]
    closes = [9, 10]
    out = williams_r(highs, lows, closes, period=5)
    assert out == [None, None]


def test_empty_input():
    assert williams_r([], [], [], period=3) == []


def test_output_within_bounds():
    highs = [10, 12, 11, 15, 14, 13, 16, 12]
    lows = [8, 9, 7, 10, 11, 9, 12, 8]
    closes = [9, 11, 8, 14, 12, 10, 15, 9]
    out = williams_r(highs, lows, closes, period=4)
    for v in out:
        if v is not None:
            assert -100.0 <= v <= 0.0


def test_accepts_decimal_inputs():
    highs = [Decimal("10"), Decimal("11"), Decimal("12")]
    lows = [Decimal("8"), Decimal("9"), Decimal("8")]
    closes = [Decimal("9"), Decimal("10"), Decimal("10")]
    out = williams_r(highs, lows, closes, period=3)
    assert out[2] == pytest.approx(-50.0)


def test_period_must_be_positive():
    with pytest.raises(ValueError):
        williams_r([1, 2], [1, 2], [1, 2], period=0)
    with pytest.raises(ValueError):
        williams_r([1, 2], [1, 2], [1, 2], period=-1)


def test_mismatched_lengths_raises():
    with pytest.raises(ValueError):
        williams_r([1, 2, 3], [1, 2], [1, 2, 3], period=2)


def test_determinism():
    highs = [10, 12, 11, 15, 14]
    lows = [8, 9, 7, 10, 11]
    closes = [9, 11, 8, 14, 12]
    a = williams_r(highs, lows, closes, period=3)
    b = williams_r(highs, lows, closes, period=3)
    assert a == b
