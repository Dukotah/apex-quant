"""
apex.strategy.ind_williams_r
============================
Williams %R — a momentum oscillator measuring where the current close sits
relative to the high/low range of the last `period` bars.

CONTRACT (matches apex.strategy.indicators):
  - Inputs: high, low, close sequences (floats or Decimals — coerced to float
    internally; this is comparative indicator math, not accounting, so money
    math stays Decimal elsewhere).
  - Output: a list the SAME LENGTH as the inputs, with None for positions that
    don't yet have a full `period`-bar window (the warmup). NEVER garbage for
    insufficient data — None means "not enough history, don't trade on this."
  - Deterministic: same input → same output, always.

Formula (each index i, over the trailing window of `period` bars ending at i):
    highest_high = max(high[i-period+1 .. i])
    lowest_low   = min(low [i-period+1 .. i])
    %R = (highest_high - close[i]) / (highest_high - lowest_low) * -100

Range is [-100, 0]: 0 means the close is at the top of the range (strong),
-100 means it's at the bottom (weak). Conventionally -20/-80 mark
overbought/oversold. When the range is flat (highest_high == lowest_low) the
denominator is zero; we return 0.0 (the close equals the single price level).

Tested in tests/test_ind_williams_r.py against hand-computed values.
"""
from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def williams_r(
    high: Sequence, low: Sequence, close: Sequence, period: int = 14
) -> list[Optional[float]]:
    """
    Williams %R over a trailing `period`-bar window. Returns values in [-100, 0],
    same length as the inputs, None during the warmup (first `period`-1 indices).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")
    out: list[Optional[float]] = [None] * n
    if n < period:
        return out

    for i in range(period - 1, n):
        window_high = max(highs[i - period + 1: i + 1])
        window_low = min(lows[i - period + 1: i + 1])
        span = window_high - window_low
        if span == 0:
            # Flat range: high == low across the window; close sits on that level.
            out[i] = 0.0
        else:
            out[i] = (window_high - closes[i]) / span * -100.0
    return out
