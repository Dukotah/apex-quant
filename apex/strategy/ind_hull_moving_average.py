"""
apex.strategy.ind_hull_moving_average
=====================================
Hull Moving Average (HMA) — a weighted-MA construction designed to be both
smooth and low-lag, addressing the classic trade-off where reducing lag (e.g.
a fast EMA) reintroduces noise.

The HMA is built from three weighted moving averages (Alan Hull, 2005):

    HMA(n) = WMA( 2 * WMA(n/2) - WMA(n),  floor(sqrt(n)) )

where WMA is the linearly weighted moving average (weights 1, 2, ..., period;
most recent value weighted highest). The inner term ``2*WMA(n/2) - WMA(n)`` is
a lag-reduced signal; smoothing it with a WMA of length ``sqrt(n)`` restores
smoothness without re-adding the lag.

Same conventions as ``apex.strategy.indicators`` (this is an indicator, not
accounting): we work in float internally; output is a list the SAME LENGTH as
the input with ``None`` during the warmup period — never garbage for
insufficient data. Deterministic: same input → same output, always.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def wma(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Linearly Weighted Moving Average. Weights are 1, 2, ..., period applied to
    the oldest..newest value in the window (most recent value weighted highest),
    normalized by the triangular number period*(period+1)/2.

    None until `period` values are available.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period:
        return out
    denom = period * (period + 1) / 2.0
    for i in range(period - 1, n):
        window = values[i - period + 1: i + 1]
        weighted = 0.0
        for w, x in enumerate(window, start=1):
            weighted += w * x
        out[i] = weighted / denom
    return out


def hull_moving_average(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Hull Moving Average over `period` bars.

    HMA(n) = WMA( 2*WMA(n/2) - WMA(n), floor(sqrt(n)) ).

    The half period is ``round(period/2)`` and the smoothing period is
    ``floor(sqrt(period))`` (both standard). With those, the first non-None
    value appears once ``period + floor(sqrt(period)) - 1`` values are
    available; positions before that are None.

    A period of 1 degenerates to the raw series (WMA(1) is the identity and
    sqrt(1) == 1), which is the mathematically correct limit.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    n = len(values)
    out: list[Optional[float]] = [None] * n

    half_period = max(1, round(period / 2))
    sqrt_period = max(1, int(math.isqrt(period)))

    wma_half = wma(values, half_period)
    wma_full = wma(values, period)

    # Lag-reduced intermediate series: 2*WMA(n/2) - WMA(n), only where both exist.
    raw: list[Optional[float]] = [None] * n
    for i in range(n):
        h = wma_half[i]
        f = wma_full[i]
        if h is not None and f is not None:
            raw[i] = 2.0 * h - f

    # Smooth the intermediate series with a WMA(sqrt(n)) over its valid portion,
    # preserving original index alignment.
    valid = [(i, v) for i, v in enumerate(raw) if v is not None]
    if len(valid) >= sqrt_period:
        raw_vals = [v for _, v in valid]
        smoothed = wma(raw_vals, sqrt_period)
        for (orig_i, _), s in zip(valid, smoothed):
            out[orig_i] = s
    return out
