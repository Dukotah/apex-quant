"""
apex.strategy.ind_ultimate_oscillator
======================================
Larry Williams' Ultimate Oscillator (UO). A momentum oscillator that blends
buying pressure across THREE timeframes (short / medium / long, classically
7 / 14 / 28 bars) to dampen the false divergences that plague single-period
oscillators. Bounded 0-100; readings below ~30 are oversold, above ~70
overbought.

Same contract as the rest of apex.strategy.indicators:
  - Input: high/low/close sequences (floats or Decimals; we work in float
    internally since this is comparative, not accounting — money math stays
    Decimal elsewhere).
  - Output: a list the SAME LENGTH as the input, with None for warmup
    positions where there isn't enough history yet. NEVER garbage.
  - Deterministic: same input → same output, always.

Math (Wikipedia / Williams' original):
    Buying Pressure (BP) = close - min(low, prior_close)
    True Range      (TR) = max(high, prior_close) - min(low, prior_close)
    AvgN = sum(BP over last N) / sum(TR over last N)
    UO   = 100 * (4*Avg_short + 2*Avg_medium + Avg_long) / (4 + 2 + 1)

BP/TR need the prior close, so the first computable index is `long_period`
(N=long_period BP/TR values require indices 1..long_period, i.e. the bar at
index `long_period`).

Tested in tests/test_ind_ultimate_oscillator.py against hand-computed values.
"""
from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def ultimate_oscillator(
    high: Sequence,
    low: Sequence,
    close: Sequence,
    short_period: int = 7,
    medium_period: int = 14,
    long_period: int = 28,
) -> list[Optional[float]]:
    """
    Ultimate Oscillator over three periods. Returns 0-100, same length as input.

    None until `long_period` BP/TR values exist (the first computable index is
    `long_period`, since BP/TR require the prior close). Also None at any index
    where the long-window true-range sum is zero (flat market → undefined ratio):
    we fail closed rather than emit garbage.

    `short_period < medium_period < long_period` is required and the periods
    must be positive; the classic blend is 7 / 14 / 28.
    """
    if short_period <= 0 or medium_period <= 0 or long_period <= 0:
        raise ValueError("periods must be positive")
    if not (short_period < medium_period < long_period):
        raise ValueError("require short_period < medium_period < long_period")

    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")

    out: list[Optional[float]] = [None] * n
    if n < long_period + 1:
        return out

    # Buying pressure and true range. Index 0 has no prior close, so leave it 0
    # and never include it in any window (windows start at index 1 at minimum).
    bp: list[float] = [0.0] * n
    tr: list[float] = [0.0] * n
    for i in range(1, n):
        prior_close = closes[i - 1]
        true_low = min(lows[i], prior_close)
        true_high = max(highs[i], prior_close)
        bp[i] = closes[i] - true_low
        tr[i] = true_high - true_low

    weight_total = 4.0 + 2.0 + 1.0
    for i in range(long_period, n):
        avg_short = _windowed_avg(bp, tr, i, short_period)
        avg_medium = _windowed_avg(bp, tr, i, medium_period)
        avg_long = _windowed_avg(bp, tr, i, long_period)
        if avg_short is None or avg_medium is None or avg_long is None:
            continue  # flat window → undefined; fail closed (leave None)
        out[i] = 100.0 * (4.0 * avg_short + 2.0 * avg_medium + avg_long) / weight_total
    return out


def _windowed_avg(
    bp: list[float], tr: list[float], end: int, period: int
) -> Optional[float]:
    """
    sum(BP) / sum(TR) over the `period` bars ending at index `end` (inclusive).
    Returns None if the true-range sum is zero (a perfectly flat window).
    """
    start = end - period + 1
    tr_sum = sum(tr[start: end + 1])
    if tr_sum == 0:
        return None
    bp_sum = sum(bp[start: end + 1])
    return bp_sum / tr_sum
