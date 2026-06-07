"""
apex.strategy.ind_donchian_channels
===================================
Donchian Channels: a breakout/volatility indicator made of three bands.

  - upper:  the highest high over the trailing `period` bars
  - lower:  the lowest low over the trailing `period` bars
  - middle: the midline, (upper + lower) / 2

A break above the upper band is the classic Turtle-style entry; a break below
the lower band the exit/short. The middle band is a mean reference.

CONTRACT (same as apex.strategy.indicators):
  - Input: sequences of values (floats or Decimals — we work in float internally
    for speed, since the channel is comparative, not accounting; money math stays
    Decimal elsewhere).
  - Output: lists the SAME LENGTH as the input, with None for positions where
    there isn't enough data yet (the "warmup" period, the first `period`-1 bars).
    NEVER returns garbage for insufficient data.
  - Deterministic: same input → same output, always. No I/O, no wall-clock time.

Tested in tests/test_ind_donchian_channels.py against hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def donchian_channels(
    high: Sequence, low: Sequence, period: int = 20
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """
    Donchian Channels over a trailing window of `period` bars.

    Returns (upper, middle, lower), each a list the same length as the inputs:
      - upper[i]  = max(high[i-period+1 .. i])
      - lower[i]  = min(low[i-period+1 .. i])
      - middle[i] = (upper[i] + lower[i]) / 2

    Each band is None for the first `period`-1 positions (insufficient history).

    `high` and `low` must be the same length. `period` must be positive.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    highs = _to_floats(high)
    lows = _to_floats(low)
    n = len(highs)
    if len(lows) != n:
        raise ValueError("high and low must be the same length")

    upper: list[Optional[float]] = [None] * n
    middle: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n
    if n < period:
        return upper, middle, lower

    for i in range(period - 1, n):
        window_high = max(highs[i - period + 1 : i + 1])
        window_low = min(lows[i - period + 1 : i + 1])
        upper[i] = window_high
        lower[i] = window_low
        middle[i] = (window_high + window_low) / 2.0
    return upper, middle, lower


def donchian_upper(high: Sequence, period: int = 20) -> list[Optional[float]]:
    """Rolling highest high over `period` bars. None during warmup."""
    if period <= 0:
        raise ValueError("period must be positive")
    highs = _to_floats(high)
    n = len(highs)
    out: list[Optional[float]] = [None] * n
    if n < period:
        return out
    for i in range(period - 1, n):
        out[i] = max(highs[i - period + 1 : i + 1])
    return out


def donchian_lower(low: Sequence, period: int = 20) -> list[Optional[float]]:
    """Rolling lowest low over `period` bars. None during warmup."""
    if period <= 0:
        raise ValueError("period must be positive")
    lows = _to_floats(low)
    n = len(lows)
    out: list[Optional[float]] = [None] * n
    if n < period:
        return out
    for i in range(period - 1, n):
        out[i] = min(lows[i - period + 1 : i + 1])
    return out


def donchian_middle(high: Sequence, low: Sequence, period: int = 20) -> list[Optional[float]]:
    """Rolling midline, (highest high + lowest low) / 2. None during warmup."""
    _, middle, _ = donchian_channels(high, low, period)
    return middle
