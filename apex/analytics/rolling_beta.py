"""
apex.analytics.rolling_beta
===========================
Rolling beta of a strategy's returns versus a benchmark's returns.

Beta measures how much a strategy moves with the market: it is the slope of a
regression of strategy returns on benchmark returns, equivalently

    beta = Cov(strategy, benchmark) / Var(benchmark)

A beta of 1.0 means the strategy moves one-for-one with the benchmark; 0.0 means
market-neutral; negative means it tends to move against the market. Tracking how
beta drifts over time is a fast way to spot a strategy quietly turning into a
closet index-tracker (or flipping its market exposure).

Like apex.validation.metrics this is statistical/indicator code: it works in
float (matching that layer's convention), is dependency-light (stdlib only), and
every function is pure and deterministic given its inputs. Windows with
insufficient or degenerate data return None rather than garbage.

Tested in tests/test_rolling_beta.py against hand-computed values.
"""

from __future__ import annotations

import statistics
from typing import List, Optional, Sequence


def beta(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> Optional[float]:
    """
    Beta of a strategy versus a benchmark over the full supplied series.

    beta = Cov(strategy, benchmark) / Var(benchmark)

    Both series are aligned to the shorter length (paired observations). Returns
    None when there are fewer than two paired points or when the benchmark has
    zero variance (beta is undefined — no market movement to regress against).
    """
    n = min(len(strategy_returns), len(benchmark_returns))
    if n < 2:
        return None

    s = strategy_returns[:n]
    b = benchmark_returns[:n]

    mean_s = statistics.fmean(s)
    mean_b = statistics.fmean(b)

    cov = sum((x - mean_s) * (y - mean_b) for x, y in zip(s, b))
    var_b = sum((y - mean_b) ** 2 for y in b)

    if var_b == 0:
        return None
    return cov / var_b


def rolling_beta(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    window: int,
) -> List[Optional[float]]:
    """
    Beta computed over a trailing window that slides one observation at a time.

    The two series are first aligned to the shorter length. For each position i
    where a full `window` of paired observations is available (i.e. i >= window-1)
    the result holds the beta of returns[i-window+1 .. i]; earlier positions hold
    None. The output list therefore has the same length as the aligned series, so
    result[i] lines up with the i-th return.

    A window whose benchmark slice has zero variance yields None at that position
    (beta undefined), never a bogus number.

    Args:
        strategy_returns: Per-period strategy returns.
        benchmark_returns: Per-period benchmark returns (same periods).
        window: Number of observations per beta estimate. Must be >= 2.

    Returns:
        List of Optional[float], one entry per aligned observation. Returns an
        empty list if window < 2 or there are fewer than `window` paired points.
    """
    if window < 2:
        return []

    n = min(len(strategy_returns), len(benchmark_returns))
    if n < window:
        return []

    s = strategy_returns[:n]
    b = benchmark_returns[:n]

    out: List[Optional[float]] = [None] * n
    for end in range(window - 1, n):
        start = end - window + 1
        out[end] = beta(s[start : end + 1], b[start : end + 1])
    return out


def latest_beta(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    window: int,
) -> Optional[float]:
    """
    Beta over only the most recent `window` paired observations.

    Convenience for callers that just want the current market exposure and don't
    need the whole history. Returns None if there isn't a full window of paired
    data or the benchmark slice has zero variance.
    """
    if window < 2:
        return None

    n = min(len(strategy_returns), len(benchmark_returns))
    if n < window:
        return None

    s = strategy_returns[n - window : n]
    b = benchmark_returns[n - window : n]
    return beta(s, b)
