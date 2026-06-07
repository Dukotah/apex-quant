"""
apex.strategy.ind_supertrend
============================
The Supertrend indicator. A trend-following overlay built from ATR-scaled bands
around the bar's median price (HL2). It produces a single line that sits below
price in an uptrend and above price in a downtrend, flipping when price closes
through the opposing band.

CONTRACT (same as apex.strategy.indicators):
  - Input: parallel high/low/close sequences (floats or Decimals — we work in
    float internally for speed, since this is a comparative indicator, not
    accounting; money math stays Decimal elsewhere).
  - Output: lists the SAME LENGTH as the input, with None for positions in the
    warmup window (where ATR has no value yet). NEVER returns garbage for
    insufficient data — None means "not enough history, don't trade on this."
  - Deterministic: same input → same output, always. No I/O, no wall-clock,
    no randomness.

The classic Supertrend recurrence (Olivier Seban):

    hl2          = (high + low) / 2
    basic_upper  = hl2 + multiplier * atr
    basic_lower  = hl2 - multiplier * atr

    final_upper[i] = basic_upper[i]
        if basic_upper[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]
        else final_upper[i-1]
    final_lower[i] = basic_lower[i]
        if basic_lower[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]
        else final_lower[i-1]

The trend flips to up when close crosses above the prior final upper band, and
to down when close crosses below the prior final lower band; otherwise it holds.
The Supertrend line is the lower band while up, the upper band while down.

Tested in tests/test_ind_supertrend.py against hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence

from apex.strategy.indicators import atr


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def supertrend(
    high: Sequence,
    low: Sequence,
    close: Sequence,
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[list[Optional[float]], list[Optional[int]]]:
    """
    Supertrend.

    Returns (line, direction), each the SAME LENGTH as the input:
      - line[i]:      the Supertrend value (the active band) or None in warmup.
      - direction[i]: +1 when the trend is up (line below price),
                      -1 when the trend is down (line above price),
                      None in warmup.

    The first value appears at the first index where ATR is available
    (index `period`). At that seed bar the direction is chosen by comparing the
    close to the basic lower band: close above it ⇒ up, otherwise down.

    `multiplier` scales the ATR band width (wider = fewer, slower flips).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if multiplier <= 0:
        raise ValueError("multiplier must be positive")

    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")

    line: list[Optional[float]] = [None] * n
    direction: list[Optional[int]] = [None] * n

    atr_vals = atr(highs, lows, closes, period)

    # The first index where ATR exists is the seed bar. If ATR never gets a
    # value (insufficient data), return the all-None outputs unchanged.
    start: Optional[int] = next((i for i, v in enumerate(atr_vals) if v is not None), None)
    if start is None:
        return line, direction

    final_upper = 0.0
    final_lower = 0.0
    prev_dir = 0

    for i in range(start, n):
        a = atr_vals[i]
        # ATR is Wilder-smoothed and, once seeded, is defined for every later
        # bar, so `a` is not None within this loop — but guard to fail closed.
        if a is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2.0
        basic_upper = hl2 + multiplier * a
        basic_lower = hl2 - multiplier * a

        if i == start:
            # Seed bar: no prior bands to carry, no prior trend.
            final_upper = basic_upper
            final_lower = basic_lower
            if closes[i] > basic_lower:
                prev_dir = 1
            else:
                prev_dir = -1
        else:
            prev_upper = final_upper
            prev_lower = final_lower
            prev_close = closes[i - 1]

            if basic_upper < prev_upper or prev_close > prev_upper:
                final_upper = basic_upper
            else:
                final_upper = prev_upper

            if basic_lower > prev_lower or prev_close < prev_lower:
                final_lower = basic_lower
            else:
                final_lower = prev_lower

            # Determine the new trend direction from a close-through.
            if prev_dir == 1 and closes[i] < final_lower:
                prev_dir = -1
            elif prev_dir == -1 and closes[i] > final_upper:
                prev_dir = 1
            # otherwise the trend holds.

        if prev_dir == 1:
            line[i] = final_lower
            direction[i] = 1
        else:
            line[i] = final_upper
            direction[i] = -1

    return line, direction


def supertrend_flips(direction: Sequence[Optional[int]]) -> list[int]:
    """
    Detect trend-direction flips from a Supertrend `direction` series.

    Returns a list the SAME LENGTH as `direction`:
      +1 where the trend flips from down (or warmup) to up,
      -1 where it flips from up (or warmup) to down,
       0 where the trend is unchanged or still in warmup.

    A flip is only emitted between two consecutive defined directions. The first
    defined direction is NOT a flip (there is no prior trend to flip from).
    """
    n = len(direction)
    out = [0] * n
    prev: Optional[int] = None
    for i in range(n):
        cur = direction[i]
        if cur is None:
            continue
        if prev is not None and cur != prev:
            out[i] = cur
        prev = cur
    return out
