"""Tests for apex.strategy.ind_supertrend (Supertrend indicator)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_supertrend import supertrend, supertrend_flips
from apex.strategy.indicators import atr

# ---------------------------------------------------------------------------
# Output shape & warmup contract
# ---------------------------------------------------------------------------


def test_output_same_length_and_warmup_none():
    n = 30
    highs = [float(i) + 1.0 for i in range(n)]
    lows = [float(i) for i in range(n)]
    closes = [float(i) + 0.5 for i in range(n)]
    period = 5

    line, direction = supertrend(highs, lows, closes, period=period, multiplier=2.0)

    assert len(line) == n
    assert len(direction) == n
    # ATR (Wilder) is None until index `period`; Supertrend mirrors that warmup.
    for i in range(period):
        assert line[i] is None
        assert direction[i] is None
    # First defined value is exactly at the ATR seed bar.
    assert line[period] is not None
    assert direction[period] is not None


def test_insufficient_data_all_none():
    # Fewer than period+1 bars => ATR never defined => all None.
    highs = [10.0, 11.0, 12.0]
    lows = [9.0, 10.0, 11.0]
    closes = [9.5, 10.5, 11.5]
    line, direction = supertrend(highs, lows, closes, period=10)
    assert line == [None, None, None]
    assert direction == [None, None, None]


def test_empty_input():
    line, direction = supertrend([], [], [], period=10)
    assert line == []
    assert direction == []


# ---------------------------------------------------------------------------
# Validation / fail-closed
# ---------------------------------------------------------------------------


def test_bad_period_raises():
    with pytest.raises(ValueError):
        supertrend([1.0], [1.0], [1.0], period=0)


def test_bad_multiplier_raises():
    with pytest.raises(ValueError):
        supertrend([1.0, 2.0], [1.0, 2.0], [1.0, 2.0], period=1, multiplier=0.0)


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        supertrend([1.0, 2.0], [1.0], [1.0, 2.0], period=1)


# ---------------------------------------------------------------------------
# Hand-computed known values (period=1, easy ATR)
# ---------------------------------------------------------------------------


def test_hand_computed_uptrend_seed_and_line():
    # period=1 => ATR is just the prior-aware true range, simple to reason about.
    # Use a clean rising series.
    highs = [10.0, 11.0, 12.0, 13.0]
    lows = [9.0, 10.0, 11.0, 12.0]
    closes = [9.5, 10.5, 11.5, 12.5]
    period = 1
    mult = 1.0

    atr_vals = atr(highs, lows, closes, period)
    # Bar 0: ATR None (warmup). Bars 1..3 defined.
    assert atr_vals[0] is None

    line, direction = supertrend(highs, lows, closes, period=period, multiplier=mult)

    # Warmup at index 0.
    assert line[0] is None
    assert direction[0] is None

    # Seed bar = index 1 (first ATR). hl2 = (11+10)/2 = 10.5, atr = TR1.
    # TR1 = max(11-10, |11-9.5|, |10-9.5|) = max(1, 1.5, 0.5) = 1.5
    a1 = atr_vals[1]
    assert a1 == pytest.approx(1.5)
    basic_lower_1 = 10.5 - mult * 1.5  # 9.0
    # close[1]=10.5 > basic_lower => seed direction up (+1), line = lower band.
    assert direction[1] == 1
    assert line[1] == pytest.approx(basic_lower_1)  # 9.0

    # Rising series stays up the whole way.
    assert direction[2] == 1
    assert direction[3] == 1
    # In an uptrend the Supertrend line sits below the close.
    for i in (1, 2, 3):
        assert line[i] < closes[i]


def test_downtrend_flips_from_up():
    # Rise then sharp fall to force an up->down flip.
    highs = [10.0, 11.0, 12.0, 11.0, 8.0, 6.0]
    lows = [9.0, 10.0, 11.0, 7.0, 5.0, 4.0]
    closes = [9.5, 10.5, 11.5, 7.5, 5.5, 4.5]
    period = 1
    mult = 1.0

    line, direction = supertrend(highs, lows, closes, period=period, multiplier=mult)

    # Starts up on the rising leg.
    assert direction[1] == 1
    assert direction[2] == 1
    # The collapse forces a flip to down at some point on the falling leg.
    assert direction[-1] == -1
    # When down, the line is above the close.
    assert line[-1] > closes[-1]


# ---------------------------------------------------------------------------
# Decimal inputs accepted (converted internally), determinism
# ---------------------------------------------------------------------------


def test_accepts_decimal_inputs():
    highs = [Decimal("10"), Decimal("11"), Decimal("12")]
    lows = [Decimal("9"), Decimal("10"), Decimal("11")]
    closes = [Decimal("9.5"), Decimal("10.5"), Decimal("11.5")]
    line, direction = supertrend(highs, lows, closes, period=1, multiplier=1.0)
    assert all(isinstance(x, float) for x in line if x is not None)
    assert direction[1] in (-1, 1)


def test_deterministic():
    highs = [10.0, 11.0, 12.0, 11.0, 13.0, 9.0, 8.0, 12.0]
    lows = [9.0, 10.0, 11.0, 9.5, 12.0, 7.0, 6.5, 10.0]
    closes = [9.5, 10.5, 11.5, 10.0, 12.5, 8.0, 7.0, 11.0]
    r1 = supertrend(highs, lows, closes, period=3, multiplier=2.0)
    r2 = supertrend(highs, lows, closes, period=3, multiplier=2.0)
    assert r1 == r2


# ---------------------------------------------------------------------------
# supertrend_flips helper
# ---------------------------------------------------------------------------


def test_flips_basic():
    direction = [None, None, 1, 1, -1, -1, 1, None]
    flips = supertrend_flips(direction)
    assert len(flips) == len(direction)
    # First defined direction (index 2) is NOT a flip.
    assert flips == [0, 0, 0, 0, -1, 0, 1, 0]


def test_flips_no_change():
    direction = [None, 1, 1, 1]
    assert supertrend_flips(direction) == [0, 0, 0, 0]


def test_flips_all_none():
    assert supertrend_flips([None, None]) == [0, 0]


def test_flips_match_supertrend_output():
    highs = [10.0, 11.0, 12.0, 11.0, 8.0, 6.0, 7.0, 9.0, 11.0]
    lows = [9.0, 10.0, 11.0, 7.0, 5.0, 4.0, 5.0, 7.0, 9.0]
    closes = [9.5, 10.5, 11.5, 7.5, 5.5, 4.5, 6.5, 8.5, 10.5]
    line, direction = supertrend(highs, lows, closes, period=1, multiplier=1.0)
    flips = supertrend_flips(direction)
    # Every nonzero flip value equals the direction at that index.
    for i, f in enumerate(flips):
        if f != 0:
            assert f == direction[i]
            assert direction[i - 1] is not None
            assert direction[i] != direction[i - 1]
