"""
apex.strategy.ind_zlema
=======================
Zero-Lag Exponential Moving Average (ZLEMA), an Ehlers/Mulloy-style indicator.

A standard EMA lags price because it is, by construction, an average of past
values. ZLEMA tries to cancel that lag by feeding the EMA an "error-corrected"
series instead of raw price: at each bar it adds back the momentum the EMA is
about to smooth away. Concretely, with lag = (period - 1) // 2, it forms

    de_lagged[i] = price[i] + (price[i] - price[i - lag])
                 = 2 * price[i] - price[i - lag]

and then runs an ordinary EMA (alpha = 2 / (period + 1)) over that series. The
extra term is an estimate of the trend's slope, so the average tracks turns more
quickly than a plain EMA at the cost of slightly more noise sensitivity.

CONTRACT (same as apex.strategy.indicators):
  - Input: a sequence of values (floats or Decimals — we work in float
    internally for speed; money math stays Decimal elsewhere).
  - Output: a list the SAME LENGTH as the input, with None for the warmup
    positions where there isn't enough data yet. NEVER returns garbage for
    insufficient data — None means "not enough history, don't trade on this."
  - Deterministic: same input → same output, always.

Tested in tests/test_ind_zlema.py against hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def zlema(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Zero-Lag Exponential Moving Average.

    `lag = (period - 1) // 2` is the de-lagging offset. The de-lagged series
    `2*price[i] - price[i - lag]` only exists from index `lag` onward, and the
    EMA over it needs `period` of those values before it can be seeded, so the
    first non-None output is at index `lag + period - 1`. The EMA seed is the
    SMA of the first `period` de-lagged values (standard convention, matching
    apex.strategy.indicators.ema), then smoothed with alpha = 2/(period+1).

    Returns a list the same length as `data`; warmup positions are None.
    Raises ValueError if `period <= 0`.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    n = len(values)
    out: list[Optional[float]] = [None] * n

    lag = (period - 1) // 2

    # The de-lagged series is defined for indices i >= lag. We need `period` of
    # those values to seed the EMA, hence the first usable output index.
    first_idx = lag + period - 1
    if n <= first_idx:
        return out

    # Build the de-lagged ("error-corrected") series over valid indices only.
    de_lagged = [2.0 * values[i] - values[i - lag] for i in range(lag, n)]

    alpha = 2.0 / (period + 1)
    seed = sum(de_lagged[:period]) / period
    out[first_idx] = seed
    prev = seed
    for j in range(period, len(de_lagged)):
        prev = (de_lagged[j] - prev) * alpha + prev
        out[lag + j] = prev
    return out
