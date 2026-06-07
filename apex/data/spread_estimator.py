"""
apex.data.spread_estimator
==========================
Estimate the effective bid-ask spread from OHLC data (Corwin-Schultz, 2012).

Backtests on daily bars usually have no quote data — only OHLCV. Yet a realistic
fill needs *some* estimate of the cost of crossing the spread, and a liquidity
filter needs *some* read on how wide that spread is. The Corwin-Schultz estimator
recovers a proportional bid-ask spread from nothing more than consecutive daily
high/low ranges, exploiting a clean piece of structure:

  - The daily **high-low range** reflects both true price volatility *and* the
    bid-ask bounce, and it scales with the *square root of time* (one day).
  - The **two-day range** (the high and low taken over a 2-day window) reflects
    the same volatility over twice the horizon, but the *same* one-spread bounce.

By comparing one-day vs two-day ranges across an adjacent pair of bars, the two
unknowns — volatility and spread — can be separated algebraically. The result is
a proportional (fraction-of-price) spread estimate per overlapping day-pair.

This is **statistical/estimator** code, so — matching the convention of
``apex.strategy.indicators`` and ``apex.validation.metrics`` — it works in
``float`` internally (the inputs may be ``Decimal`` ``Bar`` prices; they are
coerced once). The output is an *estimate*, never accounting truth, so it never
feeds P&L math directly; a caller that needs a Decimal cost converts at its own
boundary.

CONTRACT (mirrors the indicator library):
  - Per-pair functions return a list the SAME LENGTH as the input, with ``None``
    at index 0 (no preceding bar) and at any position where the estimate cannot
    be formed (non-positive prices, high < low). NEVER returns garbage for
    insufficient/degenerate data — ``None`` means "no usable estimate here."
  - Negative raw estimates are floored to ``0.0`` (the standard Corwin-Schultz
    treatment: a negative spread is an artifact of noise, read as "≈ zero").
  - Pure and deterministic: same input → same output, no I/O, no clock.

Reference: Corwin & Schultz, "A Simple Way to Estimate Bid-Ask Spreads from
Daily High and Low Prices," Journal of Finance 67(2), 2012.

Tested in tests/test_spread_estimator.py against hand-computed values.
"""
from __future__ import annotations

import math
import statistics
from typing import Optional, Sequence

# 3 - 2*sqrt(2): the constant that appears in the Corwin-Schultz alpha formula.
_THREE_MINUS_2RT2 = 3.0 - 2.0 * math.sqrt(2.0)


def _to_floats(data: Sequence) -> list[float]:
    """Coerce a sequence of numbers (Decimal/float/int/str) to floats."""
    return [float(x) for x in data]


def _pair_alpha(h0: float, l0: float, h1: float, l1: float) -> Optional[float]:
    """
    Corwin-Schultz ``alpha`` for one adjacent day-pair (day0 then day1).

    Returns ``None`` if any input is non-positive or a day's high < low — the
    log ranges would be undefined or negative, which means no usable estimate.

        beta  = ln(H0/L0)^2 + ln(H1/L1)^2
        gamma = ln( max(H0,H1) / min(L0,L1) )^2
        alpha = (sqrt(2*beta) - sqrt(beta)) / (3 - 2*sqrt(2))
                - sqrt( gamma / (3 - 2*sqrt(2)) )
    """
    if h0 <= 0.0 or l0 <= 0.0 or h1 <= 0.0 or l1 <= 0.0:
        return None
    if h0 < l0 or h1 < l1:
        return None

    r0 = math.log(h0 / l0)
    r1 = math.log(h1 / l1)
    beta = r0 * r0 + r1 * r1

    two_day_high = max(h0, h1)
    two_day_low = min(l0, l1)
    rg = math.log(two_day_high / two_day_low)
    gamma = rg * rg

    alpha = (math.sqrt(2.0 * beta) - math.sqrt(beta)) / _THREE_MINUS_2RT2
    alpha -= math.sqrt(gamma / _THREE_MINUS_2RT2)
    return alpha


def alpha_to_spread(alpha: float) -> float:
    """
    Convert a Corwin-Schultz ``alpha`` to a proportional spread estimate.

        S = 2 * (e^alpha - 1) / (1 + e^alpha)

    Negative results are floored to ``0.0`` (a negative spread is a noise
    artifact; the estimator reads it as "approximately zero").
    """
    e_alpha = math.exp(alpha)
    spread = 2.0 * (e_alpha - 1.0) / (1.0 + e_alpha)
    return spread if spread > 0.0 else 0.0


def corwin_schultz_spreads(
    highs: Sequence,
    lows: Sequence,
) -> list[Optional[float]]:
    """
    Proportional bid-ask spread estimate for every overlapping day-pair.

    Given ``highs`` and ``lows`` aligned bar-for-bar (oldest → newest), returns a
    list the SAME LENGTH as the input where element ``i`` is the spread estimated
    from the pair ``(i-1, i)``. Element 0 is always ``None`` (no preceding bar),
    as is any position whose pair is degenerate (non-positive price, high < low).

    The estimate is a *fraction of price* (e.g. ``0.012`` ≈ a 1.2% round-trip
    spread). Use ``mean_spread`` / ``median_spread`` to collapse it to a single
    liquidity read.

    Raises ``ValueError`` if ``highs`` and ``lows`` differ in length — that is a
    caller bug, not a data-quality issue, so it fails loud rather than guessing.
    """
    h = _to_floats(highs)
    lo = _to_floats(lows)
    if len(h) != len(lo):
        raise ValueError(
            f"highs and lows must be the same length ({len(h)} != {len(lo)})"
        )

    out: list[Optional[float]] = [None] * len(h)
    for i in range(1, len(h)):
        alpha = _pair_alpha(h[i - 1], lo[i - 1], h[i], lo[i])
        out[i] = None if alpha is None else alpha_to_spread(alpha)
    return out


def _finite_estimates(spreads: Sequence[Optional[float]]) -> list[float]:
    """The non-None spread estimates, preserving order."""
    return [s for s in spreads if s is not None]


def mean_spread(
    highs: Sequence,
    lows: Sequence,
) -> Optional[float]:
    """
    Mean proportional spread across all usable day-pairs, or ``None`` when no
    pair yields an estimate (fewer than 2 bars, or every pair degenerate).
    """
    estimates = _finite_estimates(corwin_schultz_spreads(highs, lows))
    if not estimates:
        return None
    return statistics.fmean(estimates)


def median_spread(
    highs: Sequence,
    lows: Sequence,
) -> Optional[float]:
    """
    Median proportional spread across all usable day-pairs, or ``None`` when no
    pair yields an estimate. The median is the more robust single-number read —
    Corwin-Schultz per-pair estimates are noisy, and the median shrugs off the
    occasional outlier pair.
    """
    estimates = _finite_estimates(corwin_schultz_spreads(highs, lows))
    if not estimates:
        return None
    return statistics.median(estimates)


def spreads_from_bars(bars: Sequence) -> list[Optional[float]]:
    """
    Convenience adapter: estimate spreads directly from a sequence of ``Bar``-like
    objects (anything exposing ``.high`` and ``.low``), oldest → newest.

    Returns the same per-pair list as :func:`corwin_schultz_spreads`. Pure: it
    only reads attributes, never mutates the bars or does any I/O.
    """
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    return corwin_schultz_spreads(highs, lows)
