"""
apex.validation.tail_ratio
==========================
Tail ratio: a quick read on the asymmetry of a return distribution.

    tail_ratio = (95th percentile of returns) / abs(5th percentile of returns)

It compares the size of the typical *big winner* against the size of the
typical *big loser*. A ratio > 1 means the right tail (gains) is fatter than the
left tail (losses) — the kind of asymmetry you want. A ratio < 1 means the
losses bite harder than the wins reward, even if the strategy looks profitable
on average. It's a complement to Sharpe/Sortino: those summarize the middle of
the distribution; this one looks only at the extremes.

Statistical metric, not money — follows the float convention of
apex/validation/metrics.py. Pure and deterministic given its inputs; tested in
tests/test_tail_ratio.py against hand-computed values.

Dependency-light (stdlib only) so it runs anywhere, including the free CI runner.
"""
from __future__ import annotations

from typing import Sequence


def percentile(values: Sequence[float], q: float) -> float | None:
    """
    The q-th percentile (q in [0, 100]) using linear interpolation between the
    two closest ranks — the same definition NumPy uses by default.

    Returns None for an empty input (insufficient data → no garbage). A single
    value's percentile is that value at every q. q is clamped to [0, 100].
    """
    n = len(values)
    if n == 0:
        return None
    if q < 0.0:
        q = 0.0
    elif q > 100.0:
        q = 100.0

    ordered = sorted(values)
    if n == 1:
        return float(ordered[0])

    # Rank position on a 0..(n-1) scale, then interpolate between neighbours.
    rank = (q / 100.0) * (n - 1)
    lower = int(rank)
    upper = min(lower + 1, n - 1)
    frac = rank - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * frac)


def tail_ratio(
    returns: Sequence[float],
    upper_q: float = 95.0,
    lower_q: float = 5.0,
) -> float | None:
    """
    Ratio of the right-tail percentile to the magnitude of the left-tail
    percentile of a return series.

        tail_ratio = percentile(returns, upper_q) / abs(percentile(returns, lower_q))

    Args:
        returns: per-period (or per-trade) returns as fractions.
        upper_q: the right-tail percentile (default 95th).
        lower_q: the left-tail percentile (default 5th).

    Returns:
        The tail ratio as a float. A value > 1 means gains in the right tail
        outsize losses in the left tail; < 1 is the reverse.

        Returns None when it cannot be computed meaningfully and we fail closed:
          - fewer than 2 data points (no distribution to speak of), or
          - the lower-tail percentile is exactly 0 (division by zero — the left
            tail carries no magnitude, so the ratio is undefined rather than inf).
    """
    if len(returns) < 2:
        return None

    upper = percentile(returns, upper_q)
    lower = percentile(returns, lower_q)
    if upper is None or lower is None:
        return None

    denom = abs(lower)
    if denom == 0.0:
        return None

    return upper / denom
