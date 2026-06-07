"""
apex.strategy.ind_stochastic
============================
The Stochastic Oscillator — a momentum indicator that locates the latest close
relative to the high/low range over a lookback window. Fast %K, then %D as a
simple moving average (SMA) of %K.

This mirrors apex.strategy.indicators: stateless, pure functions; float math
internally (indicators are comparative, not accounting — money math stays
Decimal elsewhere). Same input → same output, always.

CONTRACT:
  - Inputs: parallel sequences of highs, lows, closes (floats or Decimals).
  - Output: lists the SAME LENGTH as the input, with None where there isn't
    enough data yet (the warmup period). NEVER garbage for insufficient data —
    None means "not enough history, don't trade on this."
  - Deterministic.

Definitions (fast stochastic):
    %K[i] = 100 * (close[i] - lowest_low[i]) / (highest_high[i] - lowest_low[i])
  where lowest_low / highest_high are over the trailing `k_period` bars.
  When highest_high == lowest_low over the window (a flat range) the ratio is
  undefined; we report 50.0 (mid-range) rather than dividing by zero.

    %D[i] = SMA(%K, d_period)[i]

Tested in tests/test_ind_stochastic.py against hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def stochastic_k(
    high: Sequence, low: Sequence, close: Sequence, k_period: int = 14
) -> list[Optional[float]]:
    """
    Fast %K over a trailing `k_period` window. Returns 0-100.
    None until `k_period` bars are available.

    Flat-range guard: if the highest high equals the lowest low over the window
    (no range to measure), returns 50.0 (mid-range) instead of dividing by zero.
    """
    if k_period <= 0:
        raise ValueError("k_period must be positive")
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")
    out: list[Optional[float]] = [None] * n
    if n < k_period:
        return out

    for i in range(k_period - 1, n):
        window_start = i - k_period + 1
        highest_high = max(highs[window_start : i + 1])
        lowest_low = min(lows[window_start : i + 1])
        rng = highest_high - lowest_low
        if rng == 0:
            out[i] = 50.0
        else:
            out[i] = 100.0 * (closes[i] - lowest_low) / rng
    return out


def stochastic_d(k_values: Sequence[Optional[float]], d_period: int = 3) -> list[Optional[float]]:
    """
    %D = SMA of %K over `d_period`. Same length as `k_values`.

    Operates on a %K series that may contain leading Nones (warmup). %D is None
    until `d_period` consecutive non-None %K values are available, anchored on
    the first valid %K so the warmup composes cleanly with stochastic_k.
    """
    if d_period <= 0:
        raise ValueError("d_period must be positive")
    n = len(k_values)
    out: list[Optional[float]] = [None] * n
    for i in range(n):
        if i < d_period - 1:
            continue
        window = k_values[i - d_period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(window) / d_period  # type: ignore[misc]
    return out


def stochastic(
    high: Sequence,
    low: Sequence,
    close: Sequence,
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """
    Fast stochastic oscillator. Returns (%K, %D), each the same length as input.

    %K = fast stochastic over `k_period`; %D = SMA(%K, d_period). Both 0-100,
    with None during warmup. Convenience wrapper over stochastic_k/stochastic_d.
    """
    k = stochastic_k(high, low, close, k_period)
    d = stochastic_d(k, d_period)
    return k, d
