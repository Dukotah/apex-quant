"""
Tests for apex.strategy.ind_elder_ray — Elder Ray bull/bear power.

Full-path imports only (no package __init__ dependency). Hand-computed values
plus edge cases. Pure and fast.
"""
from __future__ import annotations

import math

import pytest

from apex.strategy.ind_elder_ray import (
    bear_power,
    bull_power,
    elder_ray,
    ema,
)


def _approx(a, b, tol=1e-9):
    return a is not None and b is not None and math.isclose(a, b, abs_tol=tol)


# ---------------------------------------------------------------------------
# EMA seed sanity (the consensus-of-value line under everything)
# ---------------------------------------------------------------------------

def test_ema_warmup_and_seed():
    closes = [10.0, 12.0, 14.0]
    out = ema(closes, 3)
    assert out[0] is None
    assert out[1] is None
    # Seed = SMA of first 3 = (10+12+14)/3 = 12.0
    assert _approx(out[2], 12.0)


def test_ema_smoothing_step():
    closes = [10.0, 12.0, 14.0, 16.0]
    out = ema(closes, 3)
    # seed = 12.0; alpha = 2/4 = 0.5; next = (16 - 12)*0.5 + 12 = 14.0
    assert _approx(out[3], 14.0)


# ---------------------------------------------------------------------------
# bull_power / bear_power with a hand-computed EMA
# ---------------------------------------------------------------------------

def test_bull_bear_hand_computed():
    # period=3. Build so the EMA is easy to compute by hand.
    close = [10.0, 12.0, 14.0, 16.0]
    high = [11.0, 13.0, 15.0, 18.0]
    low = [9.0, 11.0, 13.0, 15.0]
    # EMA(close,3): idx2 seed = 12.0, idx3 = 14.0 (see above)

    bull = bull_power(high, low, close, period=3)
    bear = bear_power(high, low, close, period=3)

    # Warmup
    assert bull[0] is None and bull[1] is None
    assert bear[0] is None and bear[1] is None

    # idx2: bull = high - ema = 15 - 12 = 3 ; bear = low - ema = 13 - 12 = 1
    assert _approx(bull[2], 3.0)
    assert _approx(bear[2], 1.0)

    # idx3: bull = 18 - 14 = 4 ; bear = 15 - 14 = 1
    assert _approx(bull[3], 4.0)
    assert _approx(bear[3], 1.0)


def test_bear_power_can_be_negative():
    close = [10.0, 12.0, 14.0]
    high = [11.0, 13.0, 15.0]
    low = [9.0, 11.0, 11.5]  # idx2 low below ema (12.0)
    bear = bear_power(high, low, close, period=3)
    # idx2: bear = 11.5 - 12.0 = -0.5
    assert _approx(bear[2], -0.5)


# ---------------------------------------------------------------------------
# elder_ray composite equals the individual legs
# ---------------------------------------------------------------------------

def test_elder_ray_matches_individual_legs():
    close = [10.0, 12.0, 14.0, 16.0, 13.0]
    high = [11.0, 13.0, 15.0, 18.0, 14.0]
    low = [9.0, 11.0, 13.0, 15.0, 12.0]
    period = 3

    bull, bear = elder_ray(high, low, close, period=period)
    assert bull == bull_power(high, low, close, period=period)
    assert bear == bear_power(high, low, close, period=period)


def test_output_length_matches_input():
    close = list(range(20))
    high = [c + 1 for c in close]
    low = [c - 1 for c in close]
    bull, bear = elder_ray(high, low, close, period=13)
    assert len(bull) == len(close) == 20
    assert len(bear) == len(close) == 20
    # Default period 13: indices 0..11 warmup, 12.. valid
    assert all(v is None for v in bull[:12])
    assert all(v is not None for v in bull[12:])


# ---------------------------------------------------------------------------
# Edge cases: insufficient data, empty, validation, Decimal-coercible input
# ---------------------------------------------------------------------------

def test_insufficient_data_all_none():
    close = [10.0, 11.0]
    high = [11.0, 12.0]
    low = [9.0, 10.0]
    bull, bear = elder_ray(high, low, close, period=3)
    assert bull == [None, None]
    assert bear == [None, None]


def test_empty_input():
    bull, bear = elder_ray([], [], [], period=13)
    assert bull == []
    assert bear == []


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        bull_power([1.0, 2.0], [1.0], [1.0, 2.0], period=2)
    with pytest.raises(ValueError):
        bear_power([1.0, 2.0], [1.0, 2.0], [1.0], period=2)
    with pytest.raises(ValueError):
        elder_ray([1.0], [1.0, 2.0], [1.0, 2.0], period=2)


def test_non_positive_period_raises():
    with pytest.raises(ValueError):
        ema([1.0, 2.0, 3.0], 0)
    with pytest.raises(ValueError):
        bull_power([1.0], [1.0], [1.0], period=-1)


def test_accepts_decimal_like_input():
    from decimal import Decimal

    close = [Decimal("10"), Decimal("12"), Decimal("14")]
    high = [Decimal("11"), Decimal("13"), Decimal("15")]
    low = [Decimal("9"), Decimal("11"), Decimal("13")]
    bull, bear = elder_ray(high, low, close, period=3)
    # ema idx2 = 12.0; bull = 15-12 = 3; bear = 13-12 = 1
    assert _approx(bull[2], 3.0)
    assert _approx(bear[2], 1.0)


def test_determinism():
    close = [10.0, 12.0, 11.0, 13.0, 15.0, 14.0, 16.0]
    high = [c + 0.5 for c in close]
    low = [c - 0.5 for c in close]
    first = elder_ray(high, low, close, period=4)
    second = elder_ray(high, low, close, period=4)
    assert first == second
