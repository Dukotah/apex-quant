"""
apex.strategy.ind_choppiness_index
==================================
The Choppiness Index (CHOP) — a directionless volatility/trend indicator.

CHOP measures whether a market is trending or choppy (consolidating). It does
NOT indicate direction, only the *degree* of trendiness:

  - High values (toward 100) → choppy, sideways, range-bound market.
  - Low values (toward 0)    → strongly trending market (up OR down).

The classic period is 14. Common reference thresholds are 61.8 (above = choppy)
and 38.2 (below = trending), the Fibonacci-derived levels.

Formula (Dreiss):
    CHOP = 100 * log10( SUM(TrueRange, n) / (MaxHigh(n) - MinLow(n)) ) / log10(n)

where:
  - TrueRange is the standard Wilder true range of each bar,
  - SUM(TrueRange, n) is the sum of the true ranges over the trailing n bars,
  - MaxHigh(n) / MinLow(n) are the highest high / lowest low over those n bars.

The intuition: in a trend the price travels far (large range) relative to the
total path it took (sum of true ranges), so the ratio is near 1 and CHOP is low.
In chop, the price wanders a lot (large path) inside a tight range, so the ratio
is large and CHOP is high.

CONTRACT (mirrors apex.strategy.indicators):
  - Input: high/low/close sequences (floats or Decimals; we work in float
    internally for speed — this is comparative, not accounting).
  - Output: a list the SAME LENGTH as the input, with None for the warmup
    period where there isn't enough data. NEVER returns garbage for
    insufficient data — None means "not enough history, don't trade on this."
  - Deterministic: same input → same output, always.

All functions tested in tests/test_ind_choppiness_index.py.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def true_ranges(high: Sequence, low: Sequence, close: Sequence) -> list[Optional[float]]:
    """
    Per-bar Wilder true range, same length as the input.

    TR[i] = max(high[i]-low[i], |high[i]-close[i-1]|, |low[i]-close[i-1]|).
    Index 0 has no prior close, so it is None (the first bar's TR is undefined
    by Wilder's definition; the CHOP sum window therefore needs valid TRs).
    """
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")
    out: list[Optional[float]] = [None] * n
    for i in range(1, n):
        out[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    return out


def choppiness_index(
    high: Sequence, low: Sequence, close: Sequence, period: int = 14
) -> list[Optional[float]]:
    """
    Choppiness Index over a trailing `period` window. Returns 0-100.

    None until `period`+1 bars exist: the true range of bar i needs the prior
    close (bar i-1), so a window of `period` valid true ranges (indices
    i-period+1 .. i) requires the first index in the window to be >= 1. The
    earliest fully-valid window therefore ends at index `period` (0-based),
    i.e. the (period+1)-th bar.

    Returns None for any window where the high-low range collapses to zero
    (a perfectly flat window): CHOP is undefined there (log of an infinite
    ratio). Failing closed — None means "don't trade on this".
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if period < 2:
        # log10(1) == 0 would divide by zero; CHOP is only defined for period>=2.
        raise ValueError("period must be at least 2")

    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")

    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    trs = true_ranges(highs, lows, closes)
    log_period = math.log10(period)

    # The first valid window ends at index `period` (covers indices 1..period,
    # which are exactly `period` consecutive valid true ranges).
    for i in range(period, n):
        start = i - period + 1
        window_trs = trs[start : i + 1]
        # All true ranges in the window are valid (start >= 1 always here).
        atr_sum = math.fsum(window_trs)  # type: ignore[arg-type]
        max_high = max(highs[start : i + 1])
        min_low = min(lows[start : i + 1])
        price_range = max_high - min_low
        if price_range <= 0.0 or atr_sum <= 0.0:
            # Flat / degenerate window — CHOP undefined. Fail closed.
            out[i] = None
            continue
        out[i] = 100.0 * math.log10(atr_sum / price_range) / log_period
    return out
