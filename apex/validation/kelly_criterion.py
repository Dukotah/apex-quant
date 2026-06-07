"""
apex.validation.kelly_criterion
===============================
Position-sizing math: the Kelly criterion.

Given an edge, Kelly tells you the fraction of capital that maximizes the
long-run geometric growth rate. It is the theoretically optimal bet size — and
also a faster route to ruin than most people expect, because real edges are
estimated with error and Kelly assumes you know them exactly. So in practice you
size at a FRACTION of full Kelly (half-Kelly is the common compromise: ~75% of
the growth for ~half the volatility).

Two ways to derive it here:
  1. From discrete win/loss statistics (win rate + payoff ratio) — the classic
     "f* = p - q/b" form, right for trade-level stats.
  2. From a series of returns — the continuous "mean / variance" form, right when
     you have a return stream rather than clean win/loss buckets.

Statistical sizing math, so this layer uses float to match
apex/validation/metrics.py. All functions are pure and deterministic. Insufficient
or degenerate inputs return None (or a result flagged not-actionable) rather than
garbage — fail closed, never hand back a number you can't trust.

Tested in tests/test_kelly_criterion.py against hand-computed values.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional, Sequence

# Default fraction-of-Kelly to actually deploy. Half-Kelly is the standard,
# conservative choice: it captures most of the growth at far less volatility.
DEFAULT_KELLY_FRACTION = 0.5


@dataclass(frozen=True)
class KellyResult:
    """
    Outcome of a Kelly sizing calculation.

    full_kelly is the raw optimal fraction; fractional_kelly is what you'd
    actually size at after scaling by `kelly_fraction`. Both are CLAMPED to
    [0, 1] for the recommendation — a negative Kelly means "no edge, don't bet,"
    and we never recommend leverage (> 1) from this layer.
    """

    full_kelly: float  # raw f* (can be negative if no edge; unclamped)
    kelly_fraction: float  # the multiplier applied (e.g. 0.5 = half-Kelly)
    fractional_kelly: float  # recommended fraction to deploy, clamped [0, 1]
    edge: bool  # True if full_kelly > 0 (a real positive edge)

    def summary(self) -> str:
        verdict = "EDGE" if self.edge else "NO EDGE"
        return (
            f"Kelly [{verdict}]: full f*={self.full_kelly:.4f}, "
            f"{self.kelly_fraction:.2f}x -> deploy {self.fractional_kelly:.2%}"
        )


def _clamp_unit(x: float) -> float:
    """Clamp to [0, 1]: no shorting-via-negative, no leverage-via-Kelly here."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def full_kelly_fraction(win_rate: float, payoff_ratio: float) -> Optional[float]:
    """
    Raw Kelly fraction from a win rate and a payoff ratio.

        f* = p - q / b

    where p = win rate, q = 1 - p (loss rate), b = payoff ratio
    (average win size / average loss size, expressed as a positive multiple:
    b = 2.0 means winners are twice the size of losers).

    Args:
        win_rate: probability of a winning trade, in [0, 1].
        payoff_ratio: avg win / avg loss, must be > 0.

    Returns the (unclamped) optimal fraction f*. This can be negative, meaning
    the bet has negative expectancy and you should not take it at all.

    Returns None for nonsensical inputs (win_rate outside [0, 1], non-positive
    payoff ratio, non-finite values) — fail closed rather than emit garbage.
    """
    if not _is_finite(win_rate) or not _is_finite(payoff_ratio):
        return None
    if win_rate < 0.0 or win_rate > 1.0:
        return None
    if payoff_ratio <= 0.0:
        return None
    loss_rate = 1.0 - win_rate
    return win_rate - loss_rate / payoff_ratio


def kelly_from_win_rate(
    win_rate: float,
    payoff_ratio: float,
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
) -> Optional[KellyResult]:
    """
    Full + fractional Kelly sizing from win/loss statistics.

    Args:
        win_rate: probability of a win, in [0, 1].
        payoff_ratio: avg win / avg loss (positive multiple).
        kelly_fraction: how much of full Kelly to deploy (0..1, default 0.5).
            Values are clamped to [0, 1]; >1 would mean over-betting Kelly, which
            we refuse from this sizing layer.

    Returns a KellyResult, or None if the inputs can't yield a trustworthy edge
    estimate (see full_kelly_fraction for the rejection rules).
    """
    f_star = full_kelly_fraction(win_rate, payoff_ratio)
    if f_star is None:
        return None
    return _build_result(f_star, kelly_fraction)


def kelly_from_returns(
    returns: Sequence[float],
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
) -> Optional[KellyResult]:
    """
    Continuous Kelly sizing from a series of per-period returns.

    For a return stream the growth-optimal leverage is approximately:

        f* = mean(returns) / variance(returns)

    (the continuous analogue of p - q/b; valid for small returns where the
    log-growth is well approximated by mean - f*var/2). Use this when you have a
    return series rather than clean win/loss buckets.

    Args:
        returns: per-period returns as fractions (0.01 = +1%).
        kelly_fraction: fraction of full Kelly to deploy (clamped to [0, 1]).

    Returns a KellyResult, or None if there are too few points (< 2) or zero
    variance (can't divide) — insufficient-data windows fail closed.
    """
    if len(returns) < 2:
        return None
    if not all(_is_finite(r) for r in returns):
        return None
    mean = statistics.fmean(returns)
    variance = statistics.pvariance(returns)
    if variance <= 0.0:
        return None
    f_star = mean / variance
    return _build_result(f_star, kelly_fraction)


def _build_result(f_star: float, kelly_fraction: float) -> KellyResult:
    """Assemble a KellyResult from a raw f* and a fraction multiplier."""
    frac = _clamp_unit(kelly_fraction)
    # Scale first, then clamp the deployed fraction to [0, 1].
    deployed = _clamp_unit(f_star * frac)
    return KellyResult(
        full_kelly=f_star,
        kelly_fraction=frac,
        fractional_kelly=deployed,
        edge=f_star > 0.0,
    )


def _is_finite(x: float) -> bool:
    """True for a real, finite number (rejects nan/inf)."""
    return x == x and x not in (float("inf"), float("-inf"))
