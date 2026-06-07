"""
Tests for apex.strategy.ind_ultimate_oscillator.

Hand-computed known values plus warmup, bounds, and edge-case coverage.
Imported by full path so no package __init__ edits are needed.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_ultimate_oscillator import ultimate_oscillator


def test_warmup_is_none_until_long_period():
    n = 40
    high = [float(i) for i in range(1, n + 1)]
    low = [h - 0.5 for h in high]
    close = [h - 0.25 for h in high]
    out = ultimate_oscillator(high, low, close)
    assert len(out) == n
    # First computable index is long_period (default 28).
    assert all(v is None for v in out[:28])
    assert out[28] is not None


def test_too_short_all_none():
    # Need at least long_period + 1 bars.
    high = [1.0] * 28
    low = [0.5] * 28
    close = [0.75] * 28
    out = ultimate_oscillator(high, low, close)
    assert out == [None] * 28


def test_strong_uptrend_pegs_near_100():
    """
    Construct bars where buying pressure equals true range on every bar, so
    every windowed avg = sum(BP)/sum(TR) = 1.0 and UO = 100*(4+2+1)/7 = 100.

    BP = close - min(low, prior_close); TR = max(high, prior_close) - min(low, prior_close).
    If low == prior_close and high == close on every bar (gap up each bar, then
    rally to the high which is the close), then:
        true_low  = min(low, prior_close) = prior_close
        true_high = max(high, prior_close) = high = close
        BP = close - prior_close
        TR = close - prior_close  ->  BP == TR.
    """
    n = 40
    close = [100.0 + i for i in range(n)]      # +1 each bar
    high = close[:]                            # high == close
    low = [close[0]] + close[:-1]              # low == prior close
    out = ultimate_oscillator(high, low, close)
    for v in out[28:]:
        assert v == pytest.approx(100.0)


def test_strong_downtrend_pegs_near_zero():
    """
    Mirror image: zero buying pressure each bar => UO = 0.
    If close == low and high == prior_close on every (declining) bar then:
        true_low  = min(low, prior_close) = low = close
        true_high = max(high, prior_close) = prior_close
        BP = close - true_low = 0
        TR = prior_close - close > 0  ->  avg = 0  ->  UO = 0.
    """
    n = 40
    close = [200.0 - i for i in range(n)]      # -1 each bar
    low = close[:]                             # low == close
    high = [close[0]] + close[:-1]             # high == prior close
    out = ultimate_oscillator(high, low, close)
    for v in out[28:]:
        assert v == pytest.approx(0.0)


def test_flat_market_returns_none():
    # Perfectly flat: every TR is 0, ratio undefined -> fail closed with None.
    n = 40
    high = [50.0] * n
    low = [50.0] * n
    close = [50.0] * n
    out = ultimate_oscillator(high, low, close)
    assert all(v is None for v in out)


def test_output_bounded_0_to_100():
    # Deterministic zig-zag; every defined value must be within [0, 100].
    n = 60
    high, low, close = [], [], []
    for i in range(n):
        base = 100.0 + (i % 5)
        high.append(base + 1.0)
        low.append(base - 1.0)
        close.append(base + (0.5 if i % 2 == 0 else -0.5))
    out = ultimate_oscillator(high, low, close)
    defined = [v for v in out if v is not None]
    assert defined  # at least some defined
    for v in defined:
        assert 0.0 <= v <= 100.0


def test_decimal_input_accepted():
    n = 40
    close = [Decimal(100 + i) for i in range(n)]
    high = close[:]
    low = [close[0]] + close[:-1]
    out = ultimate_oscillator(high, low, close)
    assert out[28] == pytest.approx(100.0)


def test_custom_periods_warmup():
    n = 20
    high = [float(i) for i in range(1, n + 1)]
    low = [h - 0.5 for h in high]
    close = [h - 0.25 for h in high]
    out = ultimate_oscillator(high, low, close, short_period=2, medium_period=4, long_period=8)
    assert all(v is None for v in out[:8])
    assert out[8] is not None


def test_hand_computed_small_case():
    """
    Tiny periods (1/2/3) hand-computed at the first defined index (index 3).

    Bars (i: high, low, close):
      0: 10, 8, 9
      1: 11, 9, 10
      2: 12, 10, 11
      3: 13, 11, 12

    BP_i = close_i - min(low_i, close_{i-1});  TR_i = max(high_i, close_{i-1}) - min(low_i, close_{i-1})
      i=1: min(9, 9)=9   -> BP=10-9=1 ; max(11,9)-9 = 11-9 = 2
      i=2: min(10,10)=10 -> BP=11-10=1; max(12,10)-10 = 12-10 = 2
      i=3: min(11,11)=11 -> BP=12-11=1; max(13,11)-11 = 13-11 = 2

    short(1) over i=3:        BP=1, TR=2 -> 0.5
    medium(2) over i=2..3:    BP=2, TR=4 -> 0.5
    long(3) over i=1..3:      BP=3, TR=6 -> 0.5
    UO = 100 * (4*0.5 + 2*0.5 + 0.5) / 7 = 100 * 3.5/7 = 50.0
    """
    high = [10.0, 11.0, 12.0, 13.0]
    low = [8.0, 9.0, 10.0, 11.0]
    close = [9.0, 10.0, 11.0, 12.0]
    out = ultimate_oscillator(high, low, close, short_period=1, medium_period=2, long_period=3)
    assert out[:3] == [None, None, None]
    assert out[3] == pytest.approx(50.0)


def test_invalid_periods_raise():
    high = [1.0] * 5
    low = [0.0] * 5
    close = [0.5] * 5
    with pytest.raises(ValueError):
        ultimate_oscillator(high, low, close, short_period=0, medium_period=2, long_period=3)
    with pytest.raises(ValueError):
        ultimate_oscillator(high, low, close, short_period=5, medium_period=2, long_period=3)


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        ultimate_oscillator([1.0, 2.0], [1.0], [1.0, 2.0])
