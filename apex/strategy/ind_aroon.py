"""
apex.strategy.ind_aroon
=======================
The Aroon indicator — Aroon Up, Aroon Down, and the Aroon Oscillator. Measures
the time elapsed since the most recent high/low within a lookback window, which
makes it a trend-strength / trend-change detector rather than a price-level one.

This is a stateless, pure-function indicator in the same family as
`apex.strategy.indicators`. It follows the SAME CONTRACT:
  - Input: a sequence of values (floats or Decimals — we work in float internally
    for speed; money math stays Decimal elsewhere).
  - Output: a list the SAME LENGTH as the input, with None for positions where
    there isn't enough data yet (the "warmup" period). NEVER returns garbage for
    insufficient data — None means "not enough history, don't trade on this."
  - Deterministic: same input → same output, always.

Definitions (Chande's original, the standard convention):
  Aroon Up   = ((period - periods_since_highest_high) / period) * 100
  Aroon Down = ((period - periods_since_lowest_low)  / period) * 100
  Oscillator = Aroon Up - Aroon Down   (ranges -100..+100)

The lookback window for index ``i`` is the ``period + 1`` bars
``values[i - period .. i]`` (today plus the ``period`` prior bars), so each
output is None until ``period + 1`` values exist. "Periods since" counts how many
bars ago the extreme occurred: 0 means it is the current bar (Aroon = 100), and
``period`` means it sat at the oldest bar in the window (Aroon = 0). When the
extreme value repeats inside the window, the MOST RECENT occurrence wins (the
smallest "periods since"), the standard tie-breaking rule.

All functions tested in tests/test_ind_aroon.py against hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def _periods_since_highest(window: list[float]) -> int:
    """
    Number of bars ago the highest value occurred, where the LAST element of
    `window` is the current bar (0 periods ago). Ties resolve to the most recent
    (smallest count). `window` has length period + 1.
    """
    last = len(window) - 1
    best_idx = last
    best_val = window[last]
    # Walk backwards so the most recent occurrence is found first; only replace
    # on a strictly greater value, preserving the recency tie-break.
    for idx in range(last - 1, -1, -1):
        if window[idx] > best_val:
            best_val = window[idx]
            best_idx = idx
    return last - best_idx


def _periods_since_lowest(window: list[float]) -> int:
    """Mirror of `_periods_since_highest` for the lowest value."""
    last = len(window) - 1
    best_idx = last
    best_val = window[last]
    for idx in range(last - 1, -1, -1):
        if window[idx] < best_val:
            best_val = window[idx]
            best_idx = idx
    return last - best_idx


def aroon_up(high: Sequence, period: int = 25) -> list[Optional[float]]:
    """
    Aroon Up (0-100). High when a new high is recent (strong/young uptrend).
    None until `period`+1 highs exist.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    highs = _to_floats(high)
    n = len(highs)
    out: list[Optional[float]] = [None] * n
    for i in range(period, n):
        window = highs[i - period : i + 1]
        since = _periods_since_highest(window)
        out[i] = (period - since) / period * 100.0
    return out


def aroon_down(low: Sequence, period: int = 25) -> list[Optional[float]]:
    """
    Aroon Down (0-100). High when a new low is recent (strong/young downtrend).
    None until `period`+1 lows exist.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    lows = _to_floats(low)
    n = len(lows)
    out: list[Optional[float]] = [None] * n
    for i in range(period, n):
        window = lows[i - period : i + 1]
        since = _periods_since_lowest(window)
        out[i] = (period - since) / period * 100.0
    return out


def aroon(
    high: Sequence, low: Sequence, period: int = 25
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """
    Full Aroon. Returns (up, down, oscillator), each the same length as input.
    oscillator = up - down, ranging -100..+100. None where either side is None.

    `high` and `low` must be the same length (typically a bar's high/low series).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    highs, lows = _to_floats(high), _to_floats(low)
    if len(highs) != len(lows):
        raise ValueError("high and low must be the same length")
    up = aroon_up(highs, period)
    down = aroon_down(lows, period)
    n = len(highs)
    osc: list[Optional[float]] = [None] * n
    for i in range(n):
        if up[i] is not None and down[i] is not None:
            osc[i] = up[i] - down[i]
    return up, down, osc
