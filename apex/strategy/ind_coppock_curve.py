"""
apex.strategy.ind_coppock_curve
===============================
The Coppock Curve — a long-term momentum oscillator originally designed by
Edwin Coppock to spot generational buying opportunities in equity indices.

It is a weighted moving average of the SUM of two rate-of-change values:

    Coppock = WMA(wma_period) of ( ROC(long_roc) + ROC(short_roc) )

where ROC(n) is the percentage rate of change over `n` bars (in percent, the
classic convention) and WMA is the linearly-weighted moving average (most
recent bar weighted heaviest: weights 1, 2, ..., wma_period).

Classic monthly parameters are long_roc=14, short_roc=11, wma_period=10.
A bullish signal is traditionally the curve turning UP from below zero.

This module follows the same CONTRACT as apex.strategy.indicators:
  - Input: a sequence of values (floats or Decimals — float internally for
    speed, since indicators are comparative, not accounting).
  - Output: a list the SAME LENGTH as the input, with None for warmup
    positions where there isn't enough data yet. NEVER garbage for
    insufficient data — None means "not enough history, don't trade on this."
  - Deterministic: same input → same output, always.

Tested in tests/test_ind_coppock_curve.py against hand-computed values.
"""
from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def roc(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Rate of Change over `period` bars, expressed as a PERCENT (the classic
    Coppock convention): roc[i] = (value[i] / value[i-period] - 1) * 100.

    None until `period`+1 values exist, and None where the reference value is
    zero (division undefined — fail closed, don't emit garbage).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    n = len(values)
    out: list[Optional[float]] = [None] * n
    for i in range(period, n):
        past = values[i - period]
        if past != 0:
            out[i] = (values[i] / past - 1.0) * 100.0
    return out


def wma(data: Sequence[Optional[float]], period: int) -> list[Optional[float]]:
    """
    Linearly-Weighted Moving Average. The most recent value in each window is
    weighted `period`, the next `period-1`, down to 1 for the oldest, then
    divided by the triangular weight sum (period*(period+1)/2).

    Operates over a series that may contain leading None warmup values: each
    output index is None unless the whole window of `period` values is present
    (i.e. all non-None). None until that condition is first met.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(data)
    out: list[Optional[float]] = [None] * n
    weight_sum = period * (period + 1) / 2.0
    for i in range(period - 1, n):
        window = data[i - period + 1: i + 1]
        if any(v is None for v in window):
            continue
        weighted = 0.0
        for k, v in enumerate(window):
            # k = 0 is the oldest (weight 1); k = period-1 is newest (weight period)
            weighted += float(v) * (k + 1)
        out[i] = weighted / weight_sum
    return out


def coppock_curve(
    data: Sequence,
    long_roc: int = 14,
    short_roc: int = 11,
    wma_period: int = 10,
) -> list[Optional[float]]:
    """
    The Coppock Curve. Returns a list the same length as `data`.

    Coppock = WMA(wma_period) of ( ROC(long_roc) + ROC(short_roc) ).

    The summed-ROC series is None until both ROC components exist (i.e. after
    max(long_roc, short_roc) bars); the WMA then needs a further wma_period-1
    bars of that combined series before producing its first value. Positions
    before that — or any position where a ROC reference value was zero — are
    None. NEVER returns garbage for insufficient data.
    """
    if long_roc <= 0 or short_roc <= 0:
        raise ValueError("ROC periods must be positive")
    if wma_period <= 0:
        raise ValueError("wma_period must be positive")
    values = _to_floats(data)
    n = len(values)

    long_series = roc(values, long_roc)
    short_series = roc(values, short_roc)

    combined: list[Optional[float]] = [None] * n
    for i in range(n):
        a = long_series[i]
        b = short_series[i]
        if a is not None and b is not None:
            combined[i] = a + b

    return wma(combined, wma_period)
