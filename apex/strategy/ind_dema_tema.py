"""
apex.strategy.ind_dema_tema
===========================
DEMA (Double Exponential Moving Average) and TEMA (Triple Exponential Moving
Average) — lag-reduced moving averages built by cascading EMAs.

These are smoothing/trend indicators, so — like the rest of
apex.strategy.indicators — they work in float internally (comparative, not
accounting). Money math stays Decimal elsewhere.

CONTRACT for every function (mirrors apex.strategy.indicators):
  - Input: a sequence of values (floats or Decimals).
  - Output: a list the SAME LENGTH as the input, with None for positions where
    there isn't enough data yet (the warmup period). NEVER returns garbage for
    insufficient data — None means "not enough history, don't trade on this."
  - Deterministic: same input -> same output, always.

Math (standard definitions, Patrick Mulloy):
  Let E1 = EMA(price, period)
      E2 = EMA(E1,    period)   (EMA of the first EMA)
      E3 = EMA(E2,    period)   (EMA of the second EMA)
  DEMA = 2*E1 - E2
  TEMA = 3*E1 - 3*E2 + E3

The seeded-EMA convention matches apex.strategy.indicators.ema exactly: each
EMA is seeded with the SMA of its first `period` valid inputs, then smoothed
with alpha = 2/(period+1). Because each cascade stage warms up after `period`
of the previous stage's outputs, DEMA needs 2*period-1 bars and TEMA needs
3*period-2 bars before producing a value.

Tested in tests/test_ind_dema_tema.py against hand-computed values.
"""
from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def _ema(values: Sequence[float], period: int) -> list[Optional[float]]:
    """
    Exponential Moving Average over a list of floats. Seeded with the SMA of the
    first `period` values, then smoothed with alpha = 2/(period+1). None until
    `period` values are available. Same convention as apex.strategy.indicators.ema.
    """
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period:
        return out
    alpha = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (values[i] - prev) * alpha + prev
        out[i] = prev
    return out


def _ema_of_optional(
    series: Sequence[Optional[float]], period: int
) -> list[Optional[float]]:
    """
    EMA of a series that may contain leading None warmup values. The EMA is
    computed over only the non-None (contiguous trailing) portion and re-aligned
    to the original indices. Returns a list the same length as `series`.
    """
    n = len(series)
    out: list[Optional[float]] = [None] * n
    valid = [(i, v) for i, v in enumerate(series) if v is not None]
    if len(valid) < period:
        return out
    vals = [v for _, v in valid]
    sub = _ema(vals, period)
    for (orig_i, _), s in zip(valid, sub):
        out[orig_i] = s
    return out


def dema(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Double Exponential Moving Average: 2*EMA1 - EMA2, where EMA1 = EMA(price)
    and EMA2 = EMA(EMA1). Reduces the lag of a plain EMA.

    None until 2*period - 1 values exist (EMA1 warms up at index period-1, then
    EMA2 needs `period` more EMA1 values).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    n = len(values)
    e1 = _ema(values, period)
    e2 = _ema_of_optional(e1, period)
    out: list[Optional[float]] = [None] * n
    for i in range(n):
        if e1[i] is not None and e2[i] is not None:
            out[i] = 2.0 * e1[i] - e2[i]
    return out


def tema(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Triple Exponential Moving Average: 3*EMA1 - 3*EMA2 + EMA3, where
    EMA1 = EMA(price), EMA2 = EMA(EMA1), EMA3 = EMA(EMA2). Reduces lag further
    than DEMA.

    None until 3*period - 2 values exist (each cascade stage adds `period`-1
    bars of warmup).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    n = len(values)
    e1 = _ema(values, period)
    e2 = _ema_of_optional(e1, period)
    e3 = _ema_of_optional(e2, period)
    out: list[Optional[float]] = [None] * n
    for i in range(n):
        if e1[i] is not None and e2[i] is not None and e3[i] is not None:
            out[i] = 3.0 * e1[i] - 3.0 * e2[i] + e3[i]
    return out
