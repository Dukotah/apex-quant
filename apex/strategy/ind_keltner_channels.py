"""
apex.strategy.ind_keltner_channels
===================================
Keltner Channels: a volatility-based envelope around an EMA.

  middle = EMA(close, ema_period)
  upper  = middle + atr_mult * ATR(high, low, close, atr_period)
  lower  = middle - atr_mult * ATR(high, low, close, atr_period)

Unlike Bollinger Bands (which use standard deviation), Keltner Channels use the
Average True Range, so the band width reflects the bar-to-bar trading range
including gaps. Price riding the upper band signals momentum; a touch of a band
in a range-bound market signals mean reversion.

CONTRACT (matches apex.strategy.indicators):
  - Input: high/low/close sequences (floats or Decimals — converted to float
    internally; indicators are comparative, not accounting).
  - Output: three lists (upper, middle, lower), each the SAME LENGTH as the
    input, with None at positions where there isn't enough history yet. The
    bands are None wherever EITHER the EMA or the ATR is still in warmup — we
    never emit a band from partial data.
  - Deterministic: same input → same output, always. No I/O, no wall-clock.

This module is self-contained: it reimplements the EMA and ATR math locally
(mirroring apex/strategy/indicators.py exactly) so it has no cross-module
dependency. Tested in tests/test_ind_keltner_channels.py against hand-computed
values.
"""
from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def _ema(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Exponential Moving Average, seeded with the SMA of the first `period`
    values (standard convention), then smoothed with alpha = 2/(period+1).
    Mirrors apex.strategy.indicators.ema.
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


def _atr(
    high: Sequence, low: Sequence, close: Sequence, period: int
) -> list[Optional[float]]:
    """
    Average True Range (Wilder). Mirrors apex.strategy.indicators.atr.
    None until `period`+1 bars (true range needs the prior close).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    true_ranges: list[float] = [0.0] * n
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges[i] = tr

    first_atr = sum(true_ranges[1: period + 1]) / period
    out[period] = first_atr
    prev = first_atr
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + true_ranges[i]) / period
        out[i] = prev
    return out


def keltner_channels(
    high: Sequence,
    low: Sequence,
    close: Sequence,
    ema_period: int = 20,
    atr_period: int = 10,
    atr_mult: float = 2.0,
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """
    Keltner Channels. Returns (upper, middle, lower), each the same length as
    the input. The middle band is EMA(close, ema_period); the upper/lower bands
    are atr_mult ATRs (period atr_period) above/below it.

    A band value is None at any index where either the EMA or the ATR is still
    in its warmup window — we never emit a band from partial data (fail closed).

    Args:
        high, low, close: OHLC sequences of equal length.
        ema_period: lookback for the EMA middle band (must be positive).
        atr_period: lookback for the ATR band width (must be positive).
        atr_mult: how many ATRs the bands sit from the middle (must be >= 0;
            the classic Keltner setting is 2.0).
    """
    if ema_period <= 0:
        raise ValueError("ema_period must be positive")
    if atr_period <= 0:
        raise ValueError("atr_period must be positive")
    if atr_mult < 0:
        raise ValueError("atr_mult must be non-negative")

    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")

    middle = _ema(closes, ema_period)
    atr_vals = _atr(highs, lows, closes, atr_period)

    upper: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n
    out_middle: list[Optional[float]] = [None] * n
    for i in range(n):
        mid = middle[i]
        a = atr_vals[i]
        if mid is None or a is None:
            continue
        offset = atr_mult * a
        out_middle[i] = mid
        upper[i] = mid + offset
        lower[i] = mid - offset
    return upper, out_middle, lower
