"""Tests for apex.strategy.ind_aroon — hand-computed Aroon values + edge cases."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_aroon import aroon, aroon_down, aroon_up


def test_warmup_is_none_until_period_plus_one_values():
    # period=3 needs 4 values (today + 3 prior) before producing anything.
    highs = [1.0, 2.0, 3.0]
    assert aroon_up(highs, period=3) == [None, None, None]
    lows = [1.0, 2.0, 3.0]
    assert aroon_down(lows, period=3) == [None, None, None]


def test_aroon_up_current_bar_is_highest():
    # Strictly increasing → newest bar is always the window high → since=0 → 100.
    highs = [1.0, 2.0, 3.0, 4.0]  # period=3 window = all four, high at index 3
    out = aroon_up(highs, period=3)
    assert out[:3] == [None, None, None]
    # (3 - 0) / 3 * 100 = 100
    assert out[3] == pytest.approx(100.0)


def test_aroon_up_oldest_bar_is_highest():
    # Strictly decreasing → window high sits at the OLDEST bar → since=period → 0.
    highs = [4.0, 3.0, 2.0, 1.0]  # period=3, high at index 0 (3 bars ago)
    out = aroon_up(highs, period=3)
    # (3 - 3) / 3 * 100 = 0
    assert out[3] == pytest.approx(0.0)


def test_aroon_down_oldest_bar_is_lowest():
    # Strictly increasing → window low at the oldest bar → since=period → 0.
    lows = [1.0, 2.0, 3.0, 4.0]
    out = aroon_down(lows, period=3)
    assert out[3] == pytest.approx(0.0)


def test_aroon_down_current_bar_is_lowest():
    lows = [4.0, 3.0, 2.0, 1.0]  # newest is lowest → since=0 → 100
    out = aroon_down(lows, period=3)
    assert out[3] == pytest.approx(100.0)


def test_extreme_mid_window_intermediate_value():
    # period=4 window = 5 bars. High at index 2 (2 bars ago) for the last output.
    highs = [1.0, 2.0, 9.0, 3.0, 4.0]
    out = aroon_up(highs, period=4)
    # since = 2, (4 - 2) / 4 * 100 = 50
    assert out[4] == pytest.approx(50.0)


def test_recency_tie_break_picks_most_recent():
    # Two equal highs of 5.0 at indices 1 and 3; the most recent (index 3, 1 bar
    # ago for the index-4 window) must win → since=1.
    highs = [1.0, 5.0, 2.0, 5.0, 3.0]
    out = aroon_up(highs, period=4)
    # since = 1, (4 - 1) / 4 * 100 = 75
    assert out[4] == pytest.approx(75.0)


def test_oscillator_equals_up_minus_down():
    highs = [1.0, 2.0, 3.0, 4.0, 5.0]  # up: newest high → 100
    lows = [5.0, 4.0, 3.0, 2.0, 1.0]  # down: newest low → 100
    up, down, osc = aroon(highs, lows, period=4)
    assert up[4] == pytest.approx(100.0)
    assert down[4] == pytest.approx(100.0)
    assert osc[4] == pytest.approx(0.0)


def test_oscillator_full_range():
    # Up trending highs (newest high) and lows whose minimum is old → osc near +100.
    highs = [1.0, 2.0, 3.0, 4.0, 5.0]
    lows = [1.0, 2.0, 3.0, 4.0, 5.0]  # newest low is the largest → low is oldest
    up, down, osc = aroon(highs, lows, period=4)
    # up: high at newest → since=0 → 100; down: low at oldest → since=4 → 0
    assert up[4] == pytest.approx(100.0)
    assert down[4] == pytest.approx(0.0)
    assert osc[4] == pytest.approx(100.0)


def test_oscillator_none_during_warmup():
    highs = [1.0, 2.0, 3.0]
    lows = [1.0, 2.0, 3.0]
    up, down, osc = aroon(highs, lows, period=3)
    assert up == [None, None, None]
    assert down == [None, None, None]
    assert osc == [None, None, None]


def test_accepts_decimal_input():
    highs = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]
    lows = [Decimal("4"), Decimal("3"), Decimal("2"), Decimal("1")]
    up, down, osc = aroon(highs, lows, period=3)
    assert up[3] == pytest.approx(100.0)
    assert down[3] == pytest.approx(100.0)
    assert osc[3] == pytest.approx(0.0)


def test_rolling_output_length_matches_input():
    highs = list(range(50))
    out = aroon_up(highs, period=25)
    assert len(out) == 50
    assert out[:25] == [None] * 25
    assert all(v is not None for v in out[25:])


def test_default_period_warmup():
    # Default period=25 → needs 26 values.
    highs = [float(i) for i in range(26)]
    out = aroon_up(highs)  # default period
    assert out[24] is None
    assert out[25] == pytest.approx(100.0)  # strictly increasing → newest high


def test_invalid_period_raises():
    with pytest.raises(ValueError):
        aroon_up([1.0, 2.0], period=0)
    with pytest.raises(ValueError):
        aroon_down([1.0, 2.0], period=-1)
    with pytest.raises(ValueError):
        aroon([1.0], [1.0], period=0)


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        aroon([1.0, 2.0, 3.0], [1.0, 2.0], period=1)
