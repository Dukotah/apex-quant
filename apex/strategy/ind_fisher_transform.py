"""
apex.strategy.ind_fisher_transform
===================================
The Fisher Transform of normalized price (John Ehlers). A stateless, pure
indicator that converts price into a near-Gaussian-distributed oscillator,
sharpening turning points: most price action clusters in the middle of its
recent range, but the Fisher Transform stretches the extremes so reversals
stand out as sharp, decisive peaks instead of gentle rolls.

CONTRACT (mirrors apex.strategy.indicators):
  - Input: a sequence of values (floats or Decimals). We work in float
    internally for speed, since this is a comparative oscillator, not
    accounting; money math stays Decimal elsewhere.
  - Output: a list the SAME LENGTH as the input, with None for positions
    where there isn't enough data yet (the warmup period). NEVER returns
    garbage for insufficient data — None means "not enough history."
  - Deterministic: same input → same output, always. No I/O, no clock,
    no randomness.

THE ALGORITHM (Ehlers' canonical formulation):
  For each bar i, over a trailing window of `period` values:
    1. Find the window's max (mx) and min (mn).
    2. Normalize the latest value to roughly [-1, 1]:
           raw = 2 * ((price - mn) / (mx - mn)) - 1     (0.5 if flat window)
    3. Smooth the normalized value and clamp to (-0.999, 0.999) so the log
       never blows up:
           value = 0.33 * 2 * raw + 0.67 * prev_value         [Ehlers' 0.33/0.67]
           value = clamp(value, -0.999, 0.999)
    4. Apply the Fisher transform with one bar of recursion:
           fisher = 0.5 * ln((1 + value) / (1 - value)) + 0.5 * prev_fisher

The recursion is seeded with zeros (value and fisher both 0 before the first
output), which is the standard convention. Output begins at index period-1,
the first bar with a full window.

Tested in tests/test_ind_fisher_transform.py against hand-computed values.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


# Ehlers' canonical smoothing/clamp constants.
_ALPHA = 0.33          # weight on the new normalized value (applied to 2*raw)
_CLAMP = 0.999         # keep |value| < 1 so ln((1+v)/(1-v)) stays finite


def fisher_transform(data: Sequence, period: int = 10) -> list[Optional[float]]:
    """
    Fisher Transform of normalized price.

    `period` is the lookback window used to normalize each value into its
    recent range. Default 10 is Ehlers' original. None until `period` values
    are available; thereafter a value for every bar.

    Returns a list the same length as `data`.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period:
        return out

    prev_value = 0.0    # smoothed normalized value, seeded at 0
    prev_fisher = 0.0   # Fisher output, seeded at 0
    for i in range(period - 1, n):
        window = values[i - period + 1: i + 1]
        mx = max(window)
        mn = min(window)
        spread = mx - mn
        if spread == 0:
            # Flat window: price sits dead-center of a zero-width range.
            raw = 0.0
        else:
            raw = 2.0 * ((values[i] - mn) / spread) - 1.0

        value = _ALPHA * 2.0 * raw + (1.0 - _ALPHA) * prev_value
        # Clamp before the log so (1 - value) and (1 + value) stay positive.
        if value > _CLAMP:
            value = _CLAMP
        elif value < -_CLAMP:
            value = -_CLAMP

        fisher = 0.5 * math.log((1.0 + value) / (1.0 - value)) + 0.5 * prev_fisher
        out[i] = fisher

        prev_value = value
        prev_fisher = fisher
    return out


def fisher_signal(
    data: Sequence, period: int = 10
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """
    Convenience pair: (fisher, trigger), where `trigger` is the Fisher line
    delayed by one bar. A common entry rule is fisher crossing its trigger.

    `trigger[i]` is `fisher[i-1]`; it is None wherever fisher is None or where
    no prior fisher value exists. Both lists are the same length as `data`.
    """
    fisher = fisher_transform(data, period)
    n = len(fisher)
    trigger: list[Optional[float]] = [None] * n
    for i in range(1, n):
        if fisher[i] is not None and fisher[i - 1] is not None:
            trigger[i] = fisher[i - 1]
    return fisher, trigger
