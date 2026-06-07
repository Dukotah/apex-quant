"""
apex.strategy.ind_chaikin_money_flow
====================================
Chaikin Money Flow (CMF). A volume-weighted measure of buying vs selling
pressure over a rolling window, in the range [-1, +1].

Follows the indicator-library contract (see apex.strategy.indicators):
  - Inputs: parallel sequences of high/low/close/volume (floats or Decimals;
    we work in float internally for speed, as this is a comparative signal,
    not accounting — money math stays Decimal elsewhere).
  - Output: a list the SAME LENGTH as the inputs, with None for positions
    where there isn't enough data yet (the warmup period) or where the
    window's total volume is zero (no defined ratio). NEVER returns garbage
    for insufficient data.
  - Deterministic: same input -> same output, always.

Definitions (standard Chaikin):
    Money Flow Multiplier (MFM) = ((C - L) - (H - C)) / (H - L)
        — when H == L the bar has no range, so MFM is taken as 0.
    Money Flow Volume (MFV)     = MFM * Volume
    CMF(period)                 = sum(MFV over period) / sum(Volume over period)
        — when the window's total volume is 0, CMF is None.

Tested in tests/test_ind_chaikin_money_flow.py against hand-computed values.
"""
from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def money_flow_multiplier(
    high: Sequence, low: Sequence, close: Sequence
) -> list[float]:
    """
    Per-bar Money Flow Multiplier in [-1, +1].

    ((close - low) - (high - close)) / (high - low). A bar with no range
    (high == low) has an undefined ratio; by convention it contributes 0.
    """
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")
    out: list[float] = [0.0] * n
    for i in range(n):
        rng = highs[i] - lows[i]
        if rng == 0:
            out[i] = 0.0
        else:
            out[i] = ((closes[i] - lows[i]) - (highs[i] - closes[i])) / rng
    return out


def chaikin_money_flow(
    high: Sequence,
    low: Sequence,
    close: Sequence,
    volume: Sequence,
    period: int = 20,
) -> list[Optional[float]]:
    """
    Chaikin Money Flow over `period` bars. Returns a list the same length as
    the inputs; None until `period` bars are available, and None for any
    window whose total volume is 0 (ratio undefined). Values fall in [-1, +1]:
    positive = net accumulation (buying pressure), negative = distribution.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    volumes = _to_floats(volume)
    n = len(closes)
    if not (len(highs) == len(lows) == len(volumes) == n):
        raise ValueError("high, low, close, volume must be the same length")

    out: list[Optional[float]] = [None] * n
    if n < period:
        return out

    mfm = money_flow_multiplier(highs, lows, closes)
    mfv = [mfm[i] * volumes[i] for i in range(n)]

    mfv_sum = sum(mfv[:period])
    vol_sum = sum(volumes[:period])
    out[period - 1] = (mfv_sum / vol_sum) if vol_sum != 0 else None

    for i in range(period, n):
        mfv_sum += mfv[i] - mfv[i - period]
        vol_sum += volumes[i] - volumes[i - period]
        out[i] = (mfv_sum / vol_sum) if vol_sum != 0 else None
    return out
