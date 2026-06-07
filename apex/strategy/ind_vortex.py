"""
apex.strategy.ind_vortex
========================
The Vortex Indicator (VI), an add-on to the core indicator library. Stateless,
pure functions in the same style as apex.strategy.indicators — one tested source
of truth, no inline recomputation in strategies.

The Vortex Indicator (Botes & Siepman, 2010) captures the relationship between
upward and downward price movement over a window:

  VM+ (positive vortex movement) = |high[i] - low[i-1]|
  VM- (negative vortex movement) = |low[i]  - high[i-1]|
  TR  (true range)               = max(high-low,
                                       |high - close[i-1]|,
                                       |low  - close[i-1]|)

  VI+ = sum(VM+ over `period`) / sum(TR over `period`)
  VI- = sum(VM- over `period`) / sum(TR over `period`)

VI+ crossing above VI- is a bullish signal; VI- crossing above VI+ is bearish.

CONTRACT (mirrors apex.strategy.indicators):
  - Input: high/low/close sequences (floats or Decimals — float internally for
    speed; this is comparative indicator math, not accounting).
  - Output: two lists the SAME LENGTH as the input, with None for warmup
    positions where there isn't enough data yet. NEVER garbage for insufficient
    data — None means "not enough history, don't trade on this."
  - Deterministic: same input → same output, always.

Tested in tests/test_ind_vortex.py against hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def vortex(
    high: Sequence, low: Sequence, close: Sequence, period: int = 14
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """
    Vortex Indicator. Returns (vi_plus, vi_minus), each the same length as the
    input. None until `period`+1 bars exist (VM/TR for index i need the prior
    bar, and we sum `period` of them, so the first valid output is at index
    `period`).

    Raises ValueError on non-positive period or mismatched input lengths.
    Returns all-None lists when there is insufficient data — never garbage.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")
    vi_plus: list[Optional[float]] = [None] * n
    vi_minus: list[Optional[float]] = [None] * n
    if n < period + 1:
        return vi_plus, vi_minus

    # Per-bar vortex movements and true range. Index 0 has no prior bar, so its
    # components are 0.0 and are never included in any window (windows start at 1).
    vm_plus: list[float] = [0.0] * n
    vm_minus: list[float] = [0.0] * n
    true_ranges: list[float] = [0.0] * n
    for i in range(1, n):
        vm_plus[i] = abs(highs[i] - lows[i - 1])
        vm_minus[i] = abs(lows[i] - highs[i - 1])
        true_ranges[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    # First window covers indices 1..period (inclusive); output lands at index `period`.
    sum_vm_plus = sum(vm_plus[1 : period + 1])
    sum_vm_minus = sum(vm_minus[1 : period + 1])
    sum_tr = sum(true_ranges[1 : period + 1])
    vi_plus[period] = _ratio(sum_vm_plus, sum_tr)
    vi_minus[period] = _ratio(sum_vm_minus, sum_tr)

    # Slide the window one bar at a time.
    for i in range(period + 1, n):
        drop = i - period
        sum_vm_plus += vm_plus[i] - vm_plus[drop]
        sum_vm_minus += vm_minus[i] - vm_minus[drop]
        sum_tr += true_ranges[i] - true_ranges[drop]
        vi_plus[i] = _ratio(sum_vm_plus, sum_tr)
        vi_minus[i] = _ratio(sum_vm_minus, sum_tr)

    return vi_plus, vi_minus


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    """Vortex line value. None on a zero true-range window (flat market) —
    fail closed rather than divide by zero or emit garbage."""
    if denominator == 0:
        return None
    return numerator / denominator
