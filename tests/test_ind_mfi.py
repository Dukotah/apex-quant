"""Tests for apex.strategy.ind_mfi — hand-computed known values plus edge cases."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_mfi import mfi, typical_price


def test_typical_price_basic() -> None:
    # (high + low + close) / 3 per bar.
    tp = typical_price([3.0, 6.0], [1.0, 3.0], [2.0, 3.0])
    assert tp == [2.0, 4.0]


def test_typical_price_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        typical_price([1.0, 2.0], [1.0], [1.0, 2.0])


def test_mfi_invalid_period_raises() -> None:
    with pytest.raises(ValueError):
        mfi([1.0], [1.0], [1.0], [1.0], period=0)


def test_mfi_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        mfi([1.0, 2.0], [1.0, 2.0], [1.0, 2.0], [1.0], period=1)


def test_mfi_insufficient_data_all_none() -> None:
    # Need period + 1 bars; here period=3 with only 3 bars.
    out = mfi([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], [1.0, 2.0, 3.0], [10.0, 10.0, 10.0], period=3)
    assert out == [None, None, None]


def test_mfi_strictly_rising_typical_price_is_100() -> None:
    # Every typical price rises → all flow positive → neg_sum = 0 → MFI = 100.
    highs = [10.0, 11.0, 12.0, 13.0]
    lows = [10.0, 11.0, 12.0, 13.0]
    closes = [10.0, 11.0, 12.0, 13.0]
    vols = [100.0, 100.0, 100.0, 100.0]
    out = mfi(highs, lows, closes, vols, period=2)
    assert out[0] is None
    assert out[1] is None
    assert out[2] == 100.0
    assert out[3] == 100.0


def test_mfi_strictly_falling_typical_price_is_0() -> None:
    # Every typical price falls → all flow negative → pos_sum = 0 → MFI = 0.
    highs = [13.0, 12.0, 11.0, 10.0]
    lows = [13.0, 12.0, 11.0, 10.0]
    closes = [13.0, 12.0, 11.0, 10.0]
    vols = [100.0, 100.0, 100.0, 100.0]
    out = mfi(highs, lows, closes, vols, period=2)
    assert out[2] == 0.0
    assert out[3] == 0.0


def test_mfi_hand_computed_mixed() -> None:
    # Use high=low=close so typical price == the value, for easy hand math.
    # tp:   10, 12, 11, 13   vol: 100 per bar  → raw_flow = tp * 100
    # changes (index 1..3):
    #   i=1: 12 > 10 → positive = 12*100 = 1200
    #   i=2: 11 < 12 → negative = 11*100 = 1100
    #   i=3: 13 > 11 → positive = 13*100 = 1300
    # period = 3, first MFI at index 3 over changes 1..3:
    #   pos_sum = 1200 + 1300 = 2500
    #   neg_sum = 1100
    #   ratio   = 2500 / 1100 = 2.2727...
    #   MFI     = 100 - 100/(1+ratio) = 100 - 100/3.2727... = 69.4444...
    prices = [10.0, 12.0, 11.0, 13.0]
    vols = [100.0, 100.0, 100.0, 100.0]
    out = mfi(prices, prices, prices, vols, period=3)
    assert out[0] is None
    assert out[2] is None
    ratio = 2500.0 / 1100.0
    expected = 100.0 - (100.0 / (1.0 + ratio))
    assert out[3] == pytest.approx(expected)
    assert out[3] == pytest.approx(69.44444444, abs=1e-6)


def test_mfi_unchanged_typical_price_counts_for_neither() -> None:
    # tp: 10, 10, 12  vol 100. period=2, first MFI at index 2 over changes 1..2.
    #   i=1: 10 == 10 → neither
    #   i=2: 12 > 10  → positive = 1200
    #   pos_sum = 1200, neg_sum = 0 → MFI = 100.
    prices = [10.0, 10.0, 12.0]
    vols = [100.0, 100.0, 100.0]
    out = mfi(prices, prices, prices, vols, period=2)
    assert out[2] == 100.0


def test_mfi_sliding_window_drops_old_bars() -> None:
    # tp: 10, 12, 11, 9   vol 100. period=2.
    # changes: i1 +(1200), i2 -(1100), i3 -(900)
    # index 2 over changes 1..2: pos=1200 neg=1100 → ratio 1200/1100
    # index 3 over changes 2..3: pos=0 neg=2000 → MFI 0
    prices = [10.0, 12.0, 11.0, 9.0]
    vols = [100.0, 100.0, 100.0, 100.0]
    out = mfi(prices, prices, prices, vols, period=2)
    ratio2 = 1200.0 / 1100.0
    assert out[2] == pytest.approx(100.0 - 100.0 / (1.0 + ratio2))
    assert out[3] == 0.0


def test_mfi_accepts_decimal_inputs() -> None:
    # Decimal in, float oscillator out — same result as float inputs.
    prices_f = [10.0, 12.0, 11.0, 13.0]
    prices_d = [Decimal("10"), Decimal("12"), Decimal("11"), Decimal("13")]
    vols_f = [100.0, 100.0, 100.0, 100.0]
    vols_d = [Decimal("100")] * 4
    out_f = mfi(prices_f, prices_f, prices_f, vols_f, period=3)
    out_d = mfi(prices_d, prices_d, prices_d, vols_d, period=3)
    assert out_d[3] == pytest.approx(out_f[3])


def test_mfi_volume_weighting_matters() -> None:
    # Same price path, but a high-volume up-bar lifts MFI vs. equal volumes.
    prices = [10.0, 12.0, 11.0, 13.0]
    base = mfi(prices, prices, prices, [100.0, 100.0, 100.0, 100.0], period=3)
    heavy_up = mfi(prices, prices, prices, [100.0, 500.0, 100.0, 500.0], period=3)
    assert heavy_up[3] > base[3]


def test_mfi_output_length_matches_input() -> None:
    n = 30
    prices = [float(i % 7 + 1) for i in range(n)]
    vols = [float(100 + i) for i in range(n)]
    out = mfi(prices, prices, prices, vols, period=14)
    assert len(out) == n
    assert all(v is None for v in out[:14])
    assert all(v is not None for v in out[14:])
