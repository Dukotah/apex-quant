"""Tests for apex.strategy.ind_zlema — Zero-Lag EMA.

Hand-computed known values plus edge cases. ZLEMA de-lags price by feeding an
EMA the series `2*price[i] - price[i-lag]` with lag = (period-1)//2.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_zlema import zlema
from apex.strategy.indicators import ema


def test_known_values_period_3():
    # period=3 -> lag=1, alpha=0.5, first usable index = lag + period - 1 = 3.
    # de_lagged (i=1..5): [3,4,5,6,7]; seed=avg(3,4,5)=4.
    #   out[3]=4; out[4]=(6-4)*.5+4=5; out[5]=(7-5)*.5+5=6.
    assert zlema([1, 2, 3, 4, 5, 6], 3) == [None, None, None, 4.0, 5.0, 6.0]


def test_linear_ramp_has_zero_lag():
    # On a perfectly linear series the de-lag correction is exact, so ZLEMA
    # equals the current price once it is defined — the indicator's whole point.
    data = [10, 12, 14, 16, 18, 20, 22]
    result = zlema(data, 3)
    for i in range(3, len(data)):
        assert result[i] == pytest.approx(float(data[i]))


def test_lag_zero_reduces_to_plain_ema():
    # period<=2 -> lag=0, de_lagged == data, so ZLEMA is exactly EMA.
    data = [1.0, 3.0, 2.0, 5.0, 4.0, 6.0]
    assert zlema(data, 2) == ema(data, 2)


def test_period_1_returns_data_unchanged():
    # period=1 -> lag=0, alpha=1 -> output tracks the input exactly.
    data = [7.0, 2.0, 9.0, 4.0]
    assert zlema(data, 1) == data


def test_warmup_is_none():
    # First non-None is at index lag + period - 1. For period=5: lag=2 -> idx 6.
    result = zlema(list(range(1, 11)), 5)
    assert result[:6] == [None] * 6
    assert result[6] is not None


def test_insufficient_data_all_none():
    # n <= first usable index -> all None, never garbage.
    assert zlema([1, 2], 3) == [None, None]
    assert zlema([], 4) == []
    assert zlema([1.0], 5) == [None]


def test_accepts_decimal_input():
    # Decimals are coerced to float internally (indicators work in float).
    out = zlema([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")], 3)
    assert out == [None, None, None, 4.0]


def test_invalid_period_raises():
    with pytest.raises(ValueError):
        zlema([1, 2, 3], 0)
    with pytest.raises(ValueError):
        zlema([1, 2, 3], -2)


def test_output_length_matches_input():
    for p in (1, 2, 3, 4, 7):
        data = list(range(20))
        assert len(zlema(data, p)) == len(data)


def test_deterministic():
    data = [3.0, 1.4, 1.5, 9.2, 6.5, 3.5, 8.9, 7.9, 3.2]
    assert zlema(data, 4) == zlema(data, 4)


def test_constant_series_is_constant():
    # A flat series stays flat: de_lagged = 2c - c = c, EMA of constant = c.
    data = [5.0] * 8
    result = zlema(data, 4)
    first = (4 - 1) // 2 + 4 - 1  # lag + period - 1 = 1 + 3 = 4
    for i in range(first, len(data)):
        assert result[i] == pytest.approx(5.0)
