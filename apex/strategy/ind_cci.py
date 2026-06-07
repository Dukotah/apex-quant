"""
apex.strategy.ind_cci
=====================
Commodity Channel Index (CCI) — a momentum oscillator that measures how far the
typical price has deviated from its moving average, normalized by the average
(absolute) deviation. Values typically oscillate in the +/-100 band; readings
beyond that flag overbought/oversold extremes.

Standalone companion to apex.strategy.indicators, following the same CONTRACT:
  - Input: high/low/close sequences (floats or Decimals — we work in float
    internally for speed, as this is comparative/indicator math, not accounting).
  - Output: a list the SAME LENGTH as the input, with None for warmup positions
    where there isn't enough data yet. NEVER returns garbage for insufficient
    data — None means "not enough history, don't trade on this."
  - Deterministic: same input → same output, always.

Tested in tests/test_ind_cci.py against hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence

# Lambert's constant. Scales the mean deviation so that ~70-80% of CCI values
# fall within the +/-100 channel. This is the classic, universally-used value.
_CCI_CONSTANT = 0.015


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def typical_price(high: Sequence, low: Sequence, close: Sequence) -> list[float]:
    """
    Typical price = (high + low + close) / 3 for each bar. Same length as input.
    The series CCI is built from.
    """
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")
    return [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]


def cci(high: Sequence, low: Sequence, close: Sequence, period: int = 20) -> list[Optional[float]]:
    """
    Commodity Channel Index over `period` bars.

        TP   = (high + low + close) / 3                  (typical price)
        ma   = SMA(TP, period)                           (over the window)
        md   = mean(|TP - ma|) over the window           (mean absolute deviation)
        CCI  = (TP - ma) / (0.015 * md)

    Returns a list the same length as the input, with None for the first
    `period`-1 positions (warmup) and for any window where the mean deviation
    is zero (flat prices → CCI undefined; we return None rather than divide).

    period=20 is Donald Lambert's original default.
    """
    if period <= 0:
        raise ValueError("period must be positive")

    tp = typical_price(high, low, close)
    n = len(tp)
    out: list[Optional[float]] = [None] * n
    if n < period:
        return out

    for i in range(period - 1, n):
        window = tp[i - period + 1 : i + 1]
        ma = sum(window) / period
        mean_dev = sum(abs(x - ma) for x in window) / period
        if mean_dev == 0:
            # Flat window: deviation undefined. Fail closed — no signal.
            continue
        out[i] = (tp[i] - ma) / (_CCI_CONSTANT * mean_dev)
    return out
