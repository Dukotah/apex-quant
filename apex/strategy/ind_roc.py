"""
apex.strategy.ind_roc
=====================
Rate of Change (ROC) and price Momentum over a lookback.

These are the two classic price-velocity indicators:

  - **ROC** is the *percentage* change over `period` bars:
        ROC[i] = (price[i] / price[i-period] - 1) * 100
    It is scale-invariant, so it's comparable across instruments at very
    different price levels (a $10 stock vs a $4000 one).

  - **Momentum** is the *absolute* price difference over `period` bars:
        MOM[i] = price[i] - price[i-period]
    It keeps the units of the underlying price; some classic momentum
    oscillators use the raw difference rather than a ratio.

CONTRACT (mirrors apex.strategy.indicators):
  - Input: a sequence of values (floats or Decimals — we work in float
    internally for speed; indicators are comparative, not accounting, so the
    money-stays-Decimal rule does not bind this layer).
  - Output: a list the SAME LENGTH as the input, with None for positions where
    there isn't enough history yet (the warmup period). NEVER garbage for
    insufficient data — None means "not enough history, don't trade on this."
  - Deterministic: same input -> same output, always. No I/O, no wall-clock.

ROC needs `period`+1 values (one to look back to), so the first `period`
entries are None. A zero base price is undefined for ROC (division by zero) and
yields None at that position rather than inf/nan — fail closed.

All functions tested in tests/test_ind_roc.py against hand-computed values.
"""
from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def roc(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Rate of Change as a percentage over `period` bars.

        ROC[i] = (price[i] / price[i-period] - 1) * 100

    None for the first `period` positions (warmup), and None at any position
    whose base price (`price[i-period]`) is zero — division is undefined, so we
    fail closed rather than emit inf/nan.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    n = len(values)
    out: list[Optional[float]] = [None] * n
    for i in range(period, n):
        base = values[i - period]
        if base != 0.0:
            out[i] = (values[i] / base - 1.0) * 100.0
    return out


def momentum(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Absolute price Momentum over `period` bars.

        MOM[i] = price[i] - price[i-period]

    None for the first `period` positions (warmup). Keeps the units of the
    underlying price (unlike ROC, which is a percentage).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    n = len(values)
    out: list[Optional[float]] = [None] * n
    for i in range(period, n):
        out[i] = values[i] - values[i - period]
    return out
