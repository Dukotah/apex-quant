"""
apex.validation.omega_ratio
===========================
The Omega ratio of a return series at a chosen threshold.

Sharpe and Sortino each collapse a return distribution down to one or two
moments (mean, variance / downside-variance). The Omega ratio throws nothing
away: it weighs the ENTIRE probability of gains above a threshold against the
entire probability of losses below it. That makes it sensitive to skew and fat
tails that mean/variance metrics miss.

Definition (for a threshold ``tau``):

    Omega(tau) = sum( max(r - tau, 0) )  /  sum( max(tau - r, 0) )

i.e. total upside over the threshold divided by total downside under it. With
``tau = 0`` it answers "per unit of money lost below break-even, how much is
gained above it?". Omega > 1 means the gains above the threshold outweigh the
losses below it; the higher the better.

Pure, deterministic, stdlib-only (matches metrics.py) so it runs anywhere,
including the free CI runner. Tested in tests/test_omega_ratio.py against
hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence


def omega_ratio(returns: Sequence[float], threshold: float = 0.0) -> Optional[float]:
    """
    Omega ratio of a return series at ``threshold``.

    Omega(tau) = sum(max(r - tau, 0)) / sum(max(tau - r, 0))

    Args:
        returns: per-period returns as fractions (0.02 = +2%). May be the raw
            return series or excess returns over a benchmark — the threshold is
            applied directly to whatever you pass in.
        threshold: the minimum acceptable return ``tau`` (default 0.0, i.e.
            break-even). Returns above it count as gains, below it as losses.

    Returns:
        The Omega ratio as a float, or ``None`` when it is undefined:
          - empty input (no data to judge), or
          - no downside below the threshold (denominator is zero). We return
            ``None`` rather than ``math.inf`` so callers must consciously decide
            how to treat "never lost" — usually it just means too little data.

        Returns 0.0 when there is downside but no upside (a genuinely losing
        series at this threshold).
    """
    if not returns:
        return None

    gains = 0.0
    losses = 0.0
    for r in returns:
        diff = r - threshold
        if diff > 0.0:
            gains += diff
        elif diff < 0.0:
            losses -= diff  # accumulate the magnitude of the shortfall

    if losses == 0.0:
        # No return fell below the threshold: Omega is undefined (division by
        # zero). Fail closed — let the caller decide, don't fabricate infinity.
        return None
    return gains / losses


def omega_ratios(
    returns: Sequence[float],
    thresholds: Sequence[float],
) -> list[Optional[float]]:
    """
    Evaluate the Omega ratio at several thresholds (an "Omega curve").

    The full Omega curve over a range of thresholds is the richest summary of a
    return distribution: where it crosses 1.0 tells you the highest threshold the
    series still clears on balance.

    Returns one result per threshold, each following :func:`omega_ratio`
    semantics (``None`` where undefined).
    """
    return [omega_ratio(returns, tau) for tau in thresholds]


def omega_threshold_crossing(
    returns: Sequence[float],
    low: float,
    high: float,
    steps: int = 100,
) -> Optional[float]:
    """
    Approximate the threshold at which the Omega ratio crosses 1.0.

    The Omega ratio is monotonically non-increasing in the threshold, so there is
    at most one crossing of 1.0. Scanning ``steps`` evenly spaced thresholds in
    ``[low, high]``, this returns the highest threshold at which Omega is still
    >= 1.0 — a single-number proxy for the distribution's "fair" return.

    Args:
        low: lower end of the threshold scan (inclusive).
        high: upper end of the threshold scan (inclusive). Must be >= ``low``.
        steps: number of intervals to scan (>= 1). More steps = finer resolution.

    Returns:
        The highest scanned threshold with Omega >= 1.0, or ``None`` if no
        scanned threshold clears 1.0 (or the inputs are degenerate).
    """
    if not returns or steps < 1 or high < low:
        return None

    best: Optional[float] = None
    for i in range(steps + 1):
        tau = low + (high - low) * (i / steps)
        ratio = omega_ratio(returns, tau)
        if ratio is None:
            # Undefined here means no downside at this threshold: Omega is
            # effectively >= 1.0, so this threshold clears.
            best = tau
            continue
        if ratio >= 1.0:
            best = tau
    return best
