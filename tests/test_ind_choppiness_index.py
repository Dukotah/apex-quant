"""
Tests for apex.strategy.ind_choppiness_index.

CHOP is verified against hand-computed values plus the structural edge cases
(warmup, flat window, length mismatch, period validation, Decimal inputs).
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from apex.strategy.ind_choppiness_index import choppiness_index, true_ranges


def test_true_ranges_known_values():
    # Bar 0 has no prior close -> None. Others are standard Wilder TR.
    highs = [10, 11, 9]
    lows = [9, 10, 8]
    closes = [9, 11, 8]
    trs = true_ranges(highs, lows, closes)
    assert trs[0] is None
    # TR[1] = max(11-10, |11-9|, |10-9|) = max(1, 2, 1) = 2
    assert trs[1] == pytest.approx(2.0)
    # TR[2] = max(9-8, |9-11|, |8-11|) = max(1, 2, 3) = 3
    assert trs[2] == pytest.approx(3.0)


def test_true_ranges_length_mismatch_raises():
    with pytest.raises(ValueError):
        true_ranges([1, 2], [1], [1, 2])


def test_choppiness_known_value_period2():
    # Hand-computed: only index 2 is valid for period=2.
    # TR[1]=2, TR[2]=3 -> sum=5; range = max(11,9)-min(10,8) = 11-8 = 3.
    # CHOP = 100 * log10(5/3) / log10(2) = 73.6965594...
    highs = [10, 11, 9]
    lows = [9, 10, 8]
    closes = [9, 11, 8]
    chop = choppiness_index(highs, lows, closes, period=2)
    assert len(chop) == 3
    assert chop[0] is None  # warmup
    assert chop[1] is None  # warmup (need period+1 = 3 bars)
    expected = 100.0 * math.log10(5.0 / 3.0) / math.log10(2.0)
    assert chop[2] == pytest.approx(expected)
    assert chop[2] == pytest.approx(73.69655941662063)


def test_choppiness_output_length_matches_input():
    n = 30
    highs = [10.0 + i for i in range(n)]
    lows = [9.0 + i for i in range(n)]
    closes = [9.5 + i for i in range(n)]
    chop = choppiness_index(highs, lows, closes, period=14)
    assert len(chop) == n
    # First 14 indices are warmup (need period+1 bars).
    assert all(v is None for v in chop[:14])
    assert all(v is not None for v in chop[14:])


def test_choppiness_insufficient_data_all_none():
    # period=14 needs 15 bars; with 10 bars everything is None.
    highs = list(range(10))
    lows = [x - 0.5 for x in range(10)]
    closes = list(range(10))
    chop = choppiness_index(highs, lows, closes, period=14)
    assert chop == [None] * 10


def test_choppiness_bounded_0_to_100():
    # CHOP is mathematically bounded in [0, 100] for any normal window because
    # range <= sum(TR) (range is one path; sum of TR is the total path) and
    # sum(TR) <= period * range only loosely — but ratio >= 1 keeps CHOP >= 0.
    # Build a varied series and assert all valid values fall in [0, 100].
    rng_h = [10, 12, 11, 13, 10, 14, 9, 15, 11, 12, 13, 10, 16, 9, 14, 12]
    rng_l = [8, 9, 9, 10, 8, 11, 7, 12, 9, 10, 10, 8, 12, 7, 11, 9]
    rng_c = [9, 11, 10, 12, 9, 13, 8, 14, 10, 11, 12, 9, 15, 8, 13, 10]
    chop = choppiness_index(rng_h, rng_l, rng_c, period=5)
    valid = [v for v in chop if v is not None]
    assert valid  # there is at least one valid value
    for v in valid:
        assert -1e-9 <= v <= 100.0 + 1e-9


def test_trending_market_low_chop():
    # A clean, gapless uptrend where each bar perfectly continues the prior:
    # high[i]=low[i-1]+2, etc., so range >> path overlap -> CHOP near 0.
    # Each bar steps up by 1, TR each bar = 1 (high-low) but the cumulative
    # range grows, driving the ratio toward 1 (CHOP toward 0).
    n = 20
    highs = [float(i + 1) for i in range(n)]
    lows = [float(i) for i in range(n)]
    closes = [float(i) + 0.5 for i in range(n)]
    chop = choppiness_index(highs, lows, closes, period=10)
    last = chop[-1]
    assert last is not None
    # Strong trend -> distinctly below the 38.2 "trending" threshold.
    assert last < 38.2


def test_choppy_market_high_chop():
    # A tight oscillation inside a fixed band: large path, small net range.
    # CHOP should be high (above the 61.8 choppy threshold).
    n = 21
    highs = []
    lows = []
    closes = []
    for i in range(n):
        if i % 2 == 0:
            highs.append(10.0)
            lows.append(8.0)
            closes.append(9.8)
        else:
            highs.append(10.2)
            lows.append(8.0)
            closes.append(8.2)
    chop = choppiness_index(highs, lows, closes, period=10)
    last = chop[-1]
    assert last is not None
    assert last > 61.8


def test_flat_window_returns_none():
    # A perfectly flat window: every bar identical -> range = 0 and sum(TR) = 0.
    # CHOP is undefined; must return None (fail closed), not NaN/garbage.
    n = 6
    highs = [10.0] * n
    lows = [10.0] * n
    closes = [10.0] * n
    chop = choppiness_index(highs, lows, closes, period=3)
    assert all(v is None for v in chop)


def test_decimal_inputs_supported():
    highs = [Decimal("10"), Decimal("11"), Decimal("9")]
    lows = [Decimal("9"), Decimal("10"), Decimal("8")]
    closes = [Decimal("9"), Decimal("11"), Decimal("8")]
    chop = choppiness_index(highs, lows, closes, period=2)
    expected = 100.0 * math.log10(5.0 / 3.0) / math.log10(2.0)
    assert chop[2] == pytest.approx(expected)


def test_period_validation():
    highs = [1.0, 2.0, 3.0]
    lows = [0.5, 1.5, 2.5]
    closes = [1.0, 2.0, 3.0]
    with pytest.raises(ValueError):
        choppiness_index(highs, lows, closes, period=0)
    with pytest.raises(ValueError):
        choppiness_index(highs, lows, closes, period=-3)
    # period=1 -> log10(1)=0 division; must be rejected.
    with pytest.raises(ValueError):
        choppiness_index(highs, lows, closes, period=1)


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        choppiness_index([1, 2, 3], [1, 2], [1, 2, 3], period=2)


def test_determinism():
    highs = [10, 12, 11, 13, 10, 14, 9, 15, 11, 12]
    lows = [8, 9, 9, 10, 8, 11, 7, 12, 9, 10]
    closes = [9, 11, 10, 12, 9, 13, 8, 14, 10, 11]
    a = choppiness_index(highs, lows, closes, period=4)
    b = choppiness_index(highs, lows, closes, period=4)
    assert a == b
