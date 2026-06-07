"""
apex.strategy.ind_trix
======================
TRIX: the 1-period rate-of-change of a triple-smoothed EMA.

TRIX is a momentum oscillator that filters out price movements deemed
insignificant. The price series is run through an EMA three times in succession
(EMA of EMA of EMA), which heavily smooths out short-term noise. TRIX is then
the percent change of that triple-smoothed line from one bar to the next.

Because it oscillates around zero, TRIX is used for trend-direction and
zero-line crossovers: positive TRIX → triple-EMA rising (uptrend), negative →
falling (downtrend). A signal line (an EMA of TRIX) is commonly added for
crossover signals, mirroring MACD.

CONTRACT (same as apex.strategy.indicators):
  - Input: a sequence of values (floats or Decimals — float internally for
    speed, since this is comparative/indicator math, not accounting).
  - Output: a list the SAME LENGTH as the input, with None for warmup positions
    where there isn't enough data yet. NEVER garbage for insufficient data —
    None means "not enough history, don't trade on this."
  - Deterministic: same input → same output, always.

Tested in tests/test_ind_trix.py against hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence

from apex.strategy.indicators import ema


def _compact(series: Sequence[Optional[float]]) -> tuple[list[int], list[float]]:
    """Return (indices, values) for the contiguous non-None tail of a series.

    EMA seeds with None during warmup, then produces a contiguous run of
    floats. We strip the leading Nones so the next EMA pass operates on the
    valid values and we remember where they sat in the original series.
    """
    indices: list[int] = []
    values: list[float] = []
    for i, v in enumerate(series):
        if v is not None:
            indices.append(i)
            values.append(float(v))
    return indices, values


def triple_ema(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Triple-smoothed EMA: EMA(EMA(EMA(data))).

    Each EMA pass consumes only the valid (non-None) portion of the previous
    pass and is re-anchored back onto the original index positions, so the
    result is the same length as `data` with None during the combined warmup.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(data)
    out: list[Optional[float]] = [None] * n

    first = ema(data, period)
    idx1, vals1 = _compact(first)
    if not vals1:
        return out

    second = ema(vals1, period)
    rel2, vals2 = _compact(second)
    if not vals2:
        return out
    # Map the second pass back onto original indices via the first pass's indices.
    idx2 = [idx1[r] for r in rel2]

    third = ema(vals2, period)
    rel3, vals3 = _compact(third)
    if not vals3:
        return out
    idx3 = [idx2[r] for r in rel3]

    for orig_i, v in zip(idx3, vals3):
        out[orig_i] = v
    return out


def trix(data: Sequence, period: int = 15) -> list[Optional[float]]:
    """
    TRIX line: the 1-period rate of change of the triple-smoothed EMA, as a
    fraction (multiply by 100 for the conventional percentage display).

    TRIX[i] = triple_ema[i] / triple_ema[i-1] - 1

    None until two consecutive triple-EMA values exist, and None at any point
    where the prior triple-EMA value is zero (rate of change undefined).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    tema = triple_ema(data, period)
    n = len(tema)
    out: list[Optional[float]] = [None] * n
    for i in range(1, n):
        prev = tema[i - 1]
        cur = tema[i]
        if prev is None or cur is None:
            continue
        if prev == 0:
            continue
        out[i] = cur / prev - 1.0
    return out


def trix_signal(
    data: Sequence, period: int = 15, signal_period: int = 9
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """
    TRIX with a signal line. Returns (trix_line, signal_line), each the same
    length as the input.

    signal_line = EMA(trix_line, signal_period), computed over the trix_line's
    valid (non-None) portion and re-anchored onto the original indices. A
    bullish crossover is trix crossing above its signal line, mirroring MACD.
    """
    if signal_period <= 0:
        raise ValueError("signal_period must be positive")
    trix_line = trix(data, period)
    n = len(trix_line)
    signal_line: list[Optional[float]] = [None] * n

    idx, vals = _compact(trix_line)
    if len(vals) >= signal_period:
        sig = ema(vals, signal_period)
        for orig_i, s in zip(idx, sig):
            if s is not None:
                signal_line[orig_i] = s
    return trix_line, signal_line
