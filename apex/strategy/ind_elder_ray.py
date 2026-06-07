"""
apex.strategy.ind_elder_ray
===========================
Elder Ray Index (Dr. Alexander Elder). Measures the strength of buyers and
sellers relative to a moving-average "consensus of value" (an EMA of close).

  - Bull Power = High - EMA(close)   → how far bulls pushed price above value.
  - Bear Power = Low  - EMA(close)   → how far bears pushed price below value.

Classic period is 13. Bull Power above zero with a rising EMA, and Bear Power
below zero but rising, is the textbook long setup; the mirror is the short.

Stateless, pure functions — same contract as apex.strategy.indicators:
  - Output lists are the SAME LENGTH as the input.
  - None for warmup positions (before the EMA has enough data). Never garbage.
  - Deterministic: same input → same output, always.

Float internally (comparative indicator math, not accounting) — matches the
convention of apex/strategy/indicators.py. Money math stays Decimal elsewhere.

Tested in tests/test_ind_elder_ray.py against hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def ema(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Exponential Moving Average. Seeded with the SMA of the first `period` values
    (standard convention), then smoothed with alpha = 2/(period+1).

    Re-implemented locally (self-contained module) and kept byte-for-byte
    consistent with apex.strategy.indicators.ema.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    out: list[Optional[float]] = [None] * len(values)
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


def bull_power(
    high: Sequence, low: Sequence, close: Sequence, period: int = 13
) -> list[Optional[float]]:
    """
    Bull Power = High - EMA(close). None until the EMA has enough data
    (the first `period`-1 positions are warmup).
    """
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")
    ema_close = ema(closes, period)
    out: list[Optional[float]] = [None] * n
    for i in range(n):
        if ema_close[i] is not None:
            out[i] = highs[i] - ema_close[i]
    return out


def bear_power(
    high: Sequence, low: Sequence, close: Sequence, period: int = 13
) -> list[Optional[float]]:
    """
    Bear Power = Low - EMA(close). None until the EMA has enough data
    (the first `period`-1 positions are warmup).
    """
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")
    ema_close = ema(closes, period)
    out: list[Optional[float]] = [None] * n
    for i in range(n):
        if ema_close[i] is not None:
            out[i] = lows[i] - ema_close[i]
    return out


def elder_ray(
    high: Sequence, low: Sequence, close: Sequence, period: int = 13
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """
    Elder Ray Index. Returns (bull_power, bear_power), each the same length as
    the input. Shares one EMA computation, so it's the preferred entry point
    when you need both legs.
    """
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")
    ema_close = ema(closes, period)
    bull: list[Optional[float]] = [None] * n
    bear: list[Optional[float]] = [None] * n
    for i in range(n):
        if ema_close[i] is not None:
            bull[i] = highs[i] - ema_close[i]
            bear[i] = lows[i] - ema_close[i]
    return bull, bear
