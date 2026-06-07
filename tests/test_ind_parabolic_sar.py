"""
Tests for apex.strategy.ind_parabolic_sar.

Hand-computed known values plus edge cases. Pure and fast.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_parabolic_sar import parabolic_sar


def test_insufficient_data_returns_all_none():
    assert parabolic_sar([], []) == []
    assert parabolic_sar([10.0], [8.0]) == [None]


def test_index_zero_is_always_none():
    out = parabolic_sar([10, 11, 12], [8, 9, 10])
    assert out[0] is None
    assert all(v is not None for v in out[1:])


def test_clean_uptrend_hand_computed():
    """
    high = [10, 11, 12], low = [8, 9, 10]. rising=True.
    Seed: sar=low[0]=8, ep=high[0]=10, af=0.02.

    i=1: sar = 8 + 0.02*(10-8) = 8.04, clamped to min(low[0]=8) = 8.0.
         new high 11>10 -> ep=11, af=0.04.
    i=2: sar = 8.0 + 0.04*(11-8.0) = 8.12, clamped to min(low[1]=9, low[0]=8) = 8.0.
         new high 12>11 -> ep=12, af=0.06.
    """
    out = parabolic_sar([10, 11, 12], [8, 9, 10])
    assert out[0] is None
    assert out[1] == pytest.approx(8.0)
    assert out[2] == pytest.approx(8.0)


def test_uptrend_sar_stays_below_lows():
    highs = [10, 11, 12, 13, 14, 15]
    lows = [8, 9, 10, 11, 12, 13]
    out = parabolic_sar(highs, lows)
    for i in range(1, len(out)):
        # In a clean uptrend, SAR must never exceed the bar's low (no stop hit).
        assert out[i] <= lows[i]


def test_clean_downtrend_hand_computed():
    """
    high = [12, 11, 10], low = [10, 9, 8]. rising = False
    (high[1]=11 < high[0]=12 and low[1]=9 < low[0]=10).
    Seed: sar=high[0]=12, ep=low[0]=10, af=0.02, short.

    i=1: sar = 12 + 0.02*(10-12) = 11.96, clamped to max(high[0]=12) = 12.0.
         high[1]=11 <= 12 no flip; new low 9<10 -> ep=9, af=0.04.
    i=2: sar = 12.0 + 0.04*(9-12.0) = 11.88, clamped max(high[1]=11, high[0]=12)=12.0.
         high[2]=10 <= 12 no flip; new low 8<9 -> ep=8, af=0.06.
    """
    out = parabolic_sar([12, 11, 10], [10, 9, 8])
    assert out[0] is None
    assert out[1] == pytest.approx(12.0)
    assert out[2] == pytest.approx(12.0)


def test_downtrend_sar_stays_above_highs():
    highs = [15, 14, 13, 12, 11, 10]
    lows = [13, 12, 11, 10, 9, 8]
    out = parabolic_sar(highs, lows)
    for i in range(1, len(out)):
        assert out[i] >= highs[i]


def test_trend_reversal_stop_and_reverse():
    """
    Uptrend that then drops sharply forces a flip. After the flip the SAR
    jumps to the prior extreme point (above price) and trends downward.
    """
    highs = [10, 11, 12, 13, 8, 7]
    lows = [8, 9, 10, 11, 6, 5]
    out = parabolic_sar(highs, lows)
    # Build the uptrend, then bar index 4 plunges below the rising SAR.
    # On / after the flip the SAR should sit above the falling price.
    assert out[4] >= lows[4]  # flip bar: SAR set to prior EP, above the drop
    assert out[5] >= highs[5]  # now in a downtrend, SAR above the high


def test_accepts_decimal_inputs():
    out = parabolic_sar(
        [Decimal("10"), Decimal("11"), Decimal("12")],
        [Decimal("8"), Decimal("9"), Decimal("10")],
    )
    assert out[1] == pytest.approx(8.0)


def test_af_max_caps_acceleration():
    # Long monotonic uptrend; with a tiny cap the SAR should converge slowly.
    highs = list(range(10, 40))
    lows = [h - 2 for h in highs]
    capped = parabolic_sar(highs, lows, af_start=0.02, af_step=0.02, af_max=0.04)
    fast = parabolic_sar(highs, lows, af_start=0.02, af_step=0.02, af_max=0.20)
    # A higher cap accelerates the SAR toward price faster, so it ends higher.
    assert fast[-1] >= capped[-1]


def test_mismatched_lengths_raises():
    with pytest.raises(ValueError):
        parabolic_sar([10, 11], [8])


def test_high_below_low_raises():
    with pytest.raises(ValueError):
        parabolic_sar([10, 5], [8, 9])  # index 1: high 5 < low 9


def test_nonpositive_af_raises():
    with pytest.raises(ValueError):
        parabolic_sar([10, 11], [8, 9], af_start=0.0)
    with pytest.raises(ValueError):
        parabolic_sar([10, 11], [8, 9], af_step=-0.01)
    with pytest.raises(ValueError):
        parabolic_sar([10, 11], [8, 9], af_max=0.0)


def test_af_max_below_start_raises():
    with pytest.raises(ValueError):
        parabolic_sar([10, 11], [8, 9], af_start=0.05, af_max=0.02)


def test_determinism():
    highs = [10, 11, 9, 12, 8, 14, 13]
    lows = [8, 9, 7, 10, 6, 12, 11]
    a = parabolic_sar(highs, lows)
    b = parabolic_sar(highs, lows)
    assert a == b
