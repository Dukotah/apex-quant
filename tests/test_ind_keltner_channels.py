"""
Tests for apex.strategy.ind_keltner_channels.

Hand-computed known values plus edge cases. Pure and fast.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from apex.strategy.ind_keltner_channels import keltner_channels


def _emas(values, period):
    """Reference EMA matching the module (SMA-seeded)."""
    out = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * alpha + prev
        out[i] = prev
    return out


def _atrs(high, low, close, period):
    """Reference ATR matching the module (Wilder)."""
    n = len(close)
    out = [None] * n
    if n < period + 1:
        return out
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    first = sum(tr[1 : period + 1]) / period
    out[period] = first
    prev = first
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def test_lengths_match_input():
    high = [10, 11, 12, 13, 14, 15]
    low = [9, 10, 11, 12, 13, 14]
    close = [9.5, 10.5, 11.5, 12.5, 13.5, 14.5]
    up, mid, lo = keltner_channels(high, low, close, ema_period=3, atr_period=3)
    assert len(up) == len(mid) == len(lo) == len(close)


def test_warmup_is_none_until_both_ready():
    # ema_period=3 -> EMA first non-None at index 2.
    # atr_period=3 -> ATR first non-None at index 3.
    # Bands start at index 3 (the later of the two).
    high = [10, 11, 12, 13, 14, 15]
    low = [9, 10, 11, 12, 13, 14]
    close = [9.5, 10.5, 11.5, 12.5, 13.5, 14.5]
    up, mid, lo = keltner_channels(high, low, close, ema_period=3, atr_period=3)
    for i in range(3):
        assert up[i] is None and mid[i] is None and lo[i] is None
    for i in range(3, 6):
        assert up[i] is not None and mid[i] is not None and lo[i] is not None


def test_matches_independent_reference():
    high = [22, 23, 25, 24, 26, 28, 27, 29, 30, 31]
    low = [20, 21, 22, 22, 23, 25, 24, 26, 27, 28]
    close = [21, 22, 24, 23, 25, 27, 26, 28, 29, 30]
    ema_p, atr_p, mult = 4, 3, 2.0
    up, mid, lo = keltner_channels(
        high, low, close, ema_period=ema_p, atr_period=atr_p, atr_mult=mult
    )

    ref_ema = _emas([float(c) for c in close], ema_p)
    ref_atr = _atrs(
        [float(x) for x in high], [float(x) for x in low], [float(c) for c in close], atr_p
    )

    for i in range(len(close)):
        if ref_ema[i] is None or ref_atr[i] is None:
            assert up[i] is None and mid[i] is None and lo[i] is None
        else:
            assert mid[i] == pytest.approx(ref_ema[i])
            assert up[i] == pytest.approx(ref_ema[i] + mult * ref_atr[i])
            assert lo[i] == pytest.approx(ref_ema[i] - mult * ref_atr[i])


def test_hand_computed_first_band():
    # Construct a series where EMA seed and ATR are easy to compute by hand.
    # ema_period=3, atr_period=3, atr_mult=2.
    # close: [10, 12, 14, 16]
    # high : [11, 13, 15, 17]
    # low  : [ 9, 11, 13, 15]
    high = [11, 13, 15, 17]
    low = [9, 11, 13, 15]
    close = [10, 12, 14, 16]
    up, mid, lo = keltner_channels(high, low, close, ema_period=3, atr_period=3, atr_mult=2.0)

    # EMA(3): seed at idx2 = mean(10,12,14) = 12.
    # idx3: alpha = 2/4 = 0.5; ema = (16 - 12)*0.5 + 12 = 14.
    # ATR(3): TR[1]=max(13-11, |13-10|, |11-10|)=3
    #         TR[2]=max(15-13, |15-12|, |13-12|)=3
    #         TR[3]=max(17-15, |17-14|, |15-14|)=3
    # first ATR at idx3 = mean(TR[1..3]) = 3.
    # Bands first appear at idx3 (EMA ready at idx2, ATR ready at idx3).
    assert mid[3] == pytest.approx(14.0)
    assert up[3] == pytest.approx(14.0 + 2.0 * 3.0)  # 20.0
    assert lo[3] == pytest.approx(14.0 - 2.0 * 3.0)  # 8.0
    # idx2: EMA ready but ATR not -> bands None.
    assert mid[2] is None and up[2] is None and lo[2] is None


def test_zero_mult_collapses_to_ema():
    high = [11, 13, 15, 17, 19]
    low = [9, 11, 13, 15, 17]
    close = [10, 12, 14, 16, 18]
    up, mid, lo = keltner_channels(high, low, close, ema_period=3, atr_period=3, atr_mult=0.0)
    for i in range(len(close)):
        if mid[i] is None:
            assert up[i] is None and lo[i] is None
        else:
            assert up[i] == pytest.approx(mid[i])
            assert lo[i] == pytest.approx(mid[i])


def test_upper_above_middle_above_lower():
    high = [22, 23, 25, 24, 26, 28, 27, 29, 30, 31, 30, 32]
    low = [20, 21, 22, 22, 23, 25, 24, 26, 27, 28, 27, 29]
    close = [21, 22, 24, 23, 25, 27, 26, 28, 29, 30, 28, 31]
    up, mid, lo = keltner_channels(high, low, close)  # defaults need >= 21 bars? no
    # defaults: ema_period=20 -> too long for 12 bars -> all None.
    assert all(v is None for v in up)
    assert all(v is None for v in mid)
    assert all(v is None for v in lo)

    up, mid, lo = keltner_channels(high, low, close, ema_period=3, atr_period=3)
    for i in range(len(close)):
        if mid[i] is not None:
            assert up[i] >= mid[i] >= lo[i]


def test_accepts_decimal_input():
    high = [Decimal("11"), Decimal("13"), Decimal("15"), Decimal("17")]
    low = [Decimal("9"), Decimal("11"), Decimal("13"), Decimal("15")]
    close = [Decimal("10"), Decimal("12"), Decimal("14"), Decimal("16")]
    up, mid, lo = keltner_channels(high, low, close, ema_period=3, atr_period=3, atr_mult=2.0)
    assert mid[3] == pytest.approx(14.0)
    assert up[3] == pytest.approx(20.0)
    assert lo[3] == pytest.approx(8.0)
    # outputs are plain floats
    assert isinstance(mid[3], float)


def test_insufficient_data_all_none():
    # Fewer bars than either window can satisfy.
    high = [11, 13]
    low = [9, 11]
    close = [10, 12]
    up, mid, lo = keltner_channels(high, low, close, ema_period=3, atr_period=3)
    assert up == [None, None]
    assert mid == [None, None]
    assert lo == [None, None]


def test_empty_input():
    up, mid, lo = keltner_channels([], [], [], ema_period=3, atr_period=3)
    assert up == [] and mid == [] and lo == []


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        keltner_channels([1, 2, 3], [1, 2], [1, 2, 3], ema_period=2, atr_period=2)


def test_invalid_params_raise():
    high = [11, 13, 15, 17]
    low = [9, 11, 13, 15]
    close = [10, 12, 14, 16]
    with pytest.raises(ValueError):
        keltner_channels(high, low, close, ema_period=0)
    with pytest.raises(ValueError):
        keltner_channels(high, low, close, atr_period=0)
    with pytest.raises(ValueError):
        keltner_channels(high, low, close, atr_mult=-1.0)


def test_no_nan_in_output():
    high = [22, 23, 25, 24, 26, 28, 27, 29, 30, 31]
    low = [20, 21, 22, 22, 23, 25, 24, 26, 27, 28]
    close = [21, 22, 24, 23, 25, 27, 26, 28, 29, 30]
    up, mid, lo = keltner_channels(high, low, close, ema_period=4, atr_period=3)
    for series in (up, mid, lo):
        for v in series:
            if v is not None:
                assert not math.isnan(v)
