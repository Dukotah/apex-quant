"""Tests for apex.strategy.ind_stochastic — hand-computed values + edge cases."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_stochastic import stochastic, stochastic_d, stochastic_k


def test_k_warmup_returns_none():
    highs = [10, 11, 12]
    lows = [9, 10, 11]
    closes = [9.5, 10.5, 11.5]
    # k_period=3, so only the last index can have a value.
    k = stochastic_k(highs, lows, closes, k_period=3)
    assert k == [None, None] + [k[2]]
    assert k[2] is not None


def test_k_insufficient_data_all_none():
    k = stochastic_k([10, 11], [9, 10], [9, 10], k_period=3)
    assert k == [None, None]


def test_k_hand_computed():
    # Window of 3. At index 2: highs 10,11,12 -> HH=12; lows 9,10,11 -> LL=9.
    # close[2]=11.5 -> %K = 100*(11.5-9)/(12-9) = 100*2.5/3 = 83.3333...
    highs = [10, 11, 12]
    lows = [9, 10, 11]
    closes = [9.5, 10.5, 11.5]
    k = stochastic_k(highs, lows, closes, k_period=3)
    assert k[2] == pytest.approx(100.0 * 2.5 / 3.0)


def test_k_close_at_high_is_100():
    highs = [10, 11, 12]
    lows = [9, 10, 11]
    closes = [9, 10, 12]  # close == HH at last window
    k = stochastic_k(highs, lows, closes, k_period=3)
    assert k[2] == pytest.approx(100.0)


def test_k_close_at_low_is_0():
    highs = [10, 11, 12]
    lows = [9, 10, 9]
    closes = [9.5, 10.5, 9]  # close == LL of window
    k = stochastic_k(highs, lows, closes, k_period=3)
    assert k[2] == pytest.approx(0.0)


def test_k_flat_range_returns_midpoint():
    # All highs == all lows over the window -> range 0 -> 50.0 guard.
    highs = [5, 5, 5]
    lows = [5, 5, 5]
    closes = [5, 5, 5]
    k = stochastic_k(highs, lows, closes, k_period=3)
    assert k[2] == 50.0


def test_k_accepts_decimal_inputs():
    highs = [Decimal("10"), Decimal("11"), Decimal("12")]
    lows = [Decimal("9"), Decimal("10"), Decimal("11")]
    closes = [Decimal("9.5"), Decimal("10.5"), Decimal("11.5")]
    k = stochastic_k(highs, lows, closes, k_period=3)
    assert k[2] == pytest.approx(100.0 * 2.5 / 3.0)
    assert all(isinstance(v, float) for v in k if v is not None)


def test_k_rolling_window_slides():
    # 4 bars, k_period=2.
    # Index1: HH=max(10,11)=11, LL=min(8,9)=8, close=10 -> 100*(2)/3 = 66.67
    # Index2: HH=max(11,9)=11,  LL=min(9,7)=7,  close=8  -> 100*(1)/4 = 25
    # Index3: HH=max(9,12)=12,  LL=min(7,10)=7, close=11 -> 100*(4)/5 = 80
    highs = [10, 11, 9, 12]
    lows = [8, 9, 7, 10]
    closes = [9, 10, 8, 11]
    k = stochastic_k(highs, lows, closes, k_period=2)
    assert k[0] is None
    assert k[1] == pytest.approx(100.0 * (10 - 8) / (11 - 8))  # 66.67
    assert k[2] == pytest.approx(100.0 * (8 - 7) / (11 - 7))  # 25
    assert k[3] == pytest.approx(100.0 * (11 - 7) / (12 - 7))  # 80


def test_k_invalid_period_raises():
    with pytest.raises(ValueError):
        stochastic_k([1], [1], [1], k_period=0)


def test_k_mismatched_lengths_raises():
    with pytest.raises(ValueError):
        stochastic_k([1, 2], [1], [1, 2], k_period=1)


def test_d_is_sma_of_k():
    k = [None, 10.0, 20.0, 30.0, 40.0]
    d = stochastic_d(k, d_period=3)
    # First valid index where 3 consecutive non-None: index 3 -> (10+20+30)/3=20
    assert d == [None, None, None, pytest.approx(20.0), pytest.approx(30.0)]


def test_d_skips_none_windows():
    k = [None, None, 30.0, 40.0]
    d = stochastic_d(k, d_period=3)
    # index2 window (None,None,30) has Nones -> None.
    # index3 window (None,30,40) has None -> None.
    assert d == [None, None, None, None]


def test_d_period_one_passthrough():
    k = [None, 10.0, 20.0]
    d = stochastic_d(k, d_period=1)
    assert d == [None, pytest.approx(10.0), pytest.approx(20.0)]


def test_d_invalid_period_raises():
    with pytest.raises(ValueError):
        stochastic_d([1.0], d_period=0)


def test_stochastic_wrapper_returns_k_and_d():
    highs = [10, 11, 12, 13, 14]
    lows = [9, 10, 11, 12, 13]
    closes = [9.5, 10.5, 11.5, 12.5, 13.5]
    k, d = stochastic(highs, lows, closes, k_period=3, d_period=2)
    assert len(k) == 5 and len(d) == 5
    # Every windowed %K here: close is 2.5 above LL over a range of 3 -> 83.33
    expected_k = 100.0 * 2.5 / 3.0
    for i in (2, 3, 4):
        assert k[i] == pytest.approx(expected_k)
    # %D = SMA of %K (d_period=2); first valid at index 3.
    assert d[2] is None
    assert d[3] == pytest.approx(expected_k)
    assert d[4] == pytest.approx(expected_k)


def test_stochastic_wrapper_warmup_all_none():
    k, d = stochastic([1, 2], [1, 2], [1, 2], k_period=5, d_period=3)
    assert k == [None, None]
    assert d == [None, None]
