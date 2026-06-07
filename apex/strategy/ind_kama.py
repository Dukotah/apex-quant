"""
apex.strategy.ind_kama
======================
Kaufman's Adaptive Moving Average (KAMA). A trend-following moving average that
adapts its smoothing speed to market noise via the *efficiency ratio* (ER):

  - When price moves directionally (high ER), KAMA tracks closely (fast).
  - When price chops sideways (low ER), KAMA flattens out (slow), filtering noise.

This lives alongside apex.strategy.indicators and follows the SAME contract:
  - Input: a sequence of values (floats or Decimals — float internally for speed,
    since this is a comparative indicator, not accounting; money math is Decimal
    elsewhere).
  - Output: a list the SAME LENGTH as the input, with None for warmup positions
    where there isn't enough history yet. NEVER returns garbage for insufficient
    data — None means "don't trade on this".
  - Deterministic: same input → same output, always. No I/O, no clock, no RNG.

Math (Perry Kaufman's standard formulation):
  change      = |price[i] - price[i - period]|
  volatility  = sum(|price[j] - price[j - 1]|) over the last `period` changes
  ER          = change / volatility            (0 when volatility == 0)
  fast_sc     = 2 / (fast_period + 1)
  slow_sc     = 2 / (slow_period + 1)
  SC          = (ER * (fast_sc - slow_sc) + slow_sc) ** 2     (smoothing constant)
  KAMA[i]     = KAMA[i-1] + SC * (price[i] - KAMA[i-1])

The series is seeded at index `period` with the SMA of the first `period` values
(a standard, stable convention) and computed forward from there.

Tested in tests/test_ind_kama.py against hand-computed values.
"""
from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def efficiency_ratio(data: Sequence, period: int = 10) -> list[Optional[float]]:
    """
    Kaufman's Efficiency Ratio (a.k.a. the "fractal efficiency").

    ER = |net change over `period`| / sum of |bar-to-bar changes| over `period`.
    Ranges 0..1: 1.0 = perfectly directional move, 0.0 = pure noise / no net move.
    None until `period`+1 values exist (need `period` bar-to-bar changes).
    When the path is perfectly flat (zero volatility) ER is defined as 0.0.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    # Absolute bar-to-bar changes; abs_changes[i] = |values[i] - values[i-1]|.
    abs_changes = [0.0] * n
    for i in range(1, n):
        abs_changes[i] = abs(values[i] - values[i - 1])

    # Rolling sum of the last `period` absolute changes (the "volatility").
    volatility = sum(abs_changes[1: period + 1])
    for i in range(period, n):
        if i > period:
            volatility += abs_changes[i] - abs_changes[i - period]
        change = abs(values[i] - values[i - period])
        out[i] = 0.0 if volatility == 0 else change / volatility
    return out


def kama(
    data: Sequence,
    period: int = 10,
    fast_period: int = 2,
    slow_period: int = 30,
) -> list[Optional[float]]:
    """
    Kaufman's Adaptive Moving Average.

    `period`      — lookback for the efficiency ratio (Kaufman's default 10).
    `fast_period` — fastest EMA equivalent the SC may reach (default 2).
    `slow_period` — slowest EMA equivalent the SC may reach (default 30).

    Returns a list the same length as the input, with None for the warmup
    (indices 0..period-1). The series is seeded at index `period` with the SMA
    of the first `period` values, then adapts forward via the smoothing constant.
    None until `period`+1 values exist.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if fast_period <= 0 or slow_period <= 0:
        raise ValueError("fast_period and slow_period must be positive")

    values = _to_floats(data)
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    er = efficiency_ratio(values, period)
    fast_sc = 2.0 / (fast_period + 1)
    slow_sc = 2.0 / (slow_period + 1)

    # Seed at index `period` with the SMA of the first `period` values.
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        ratio = er[i]
        # er[i] is non-None for i >= period by construction.
        sc = (ratio * (fast_sc - slow_sc) + slow_sc) ** 2
        prev = prev + sc * (values[i] - prev)
        out[i] = prev
    return out
