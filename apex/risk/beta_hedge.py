"""
apex.risk.beta_hedge
====================
Beta-hedging math: estimate a portfolio's beta to a benchmark, then derive the
hedge notional that neutralizes that beta.

The classic systematic-risk overlay. A long book carries market exposure: when
the benchmark moves 1%, a book with beta 1.3 tends to move ~1.3%. Shorting an
amount of the benchmark equal to (beta * portfolio_value) cancels that first-order
market move, leaving (ideally) only the idiosyncratic alpha you were paid for.

This module is the STATISTICS layer (beta is an OLS regression slope estimated
from return series), so — like apex/validation/metrics.py — it works in float
for the indicator/metric math. It is pure, deterministic, dependency-light
(stdlib statistics only), and degrades gracefully: insufficient or degenerate
data returns None rather than garbage (fail closed — no hedge claim you can't
justify from the data).

All functions are pure and deterministic given their inputs. Tested in
tests/test_beta_hedge.py against hand-computed values.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional, Sequence


def beta(
    asset_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> Optional[float]:
    """
    OLS beta of `asset_returns` against `benchmark_returns`:

        beta = cov(asset, benchmark) / var(benchmark)

    This is the slope of the regression of the asset's returns on the
    benchmark's returns — how much the asset moves per unit of benchmark move.

    Series are aligned to their common (shorter) length, oldest-first. Returns
    None when there is too little data (< 2 paired observations) or when the
    benchmark has zero variance (a flat benchmark gives no information about
    sensitivity — slope is undefined, so we fail closed).
    """
    n = min(len(asset_returns), len(benchmark_returns))
    if n < 2:
        return None
    a = asset_returns[:n]
    b = benchmark_returns[:n]

    mean_a = statistics.fmean(a)
    mean_b = statistics.fmean(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_b == 0:
        return None
    return cov / var_b


@dataclass(frozen=True)
class BetaHedge:
    """
    The result of a beta-hedge computation.

    Attributes:
        beta:           Estimated portfolio beta to the benchmark.
        hedge_ratio:    Fraction of portfolio value to short in the benchmark to
                        reach the target beta. Positive => short the benchmark
                        (the usual case for a net-long book); negative => buy more
                        benchmark exposure (an under-hedged / net-short book).
        hedge_notional: Signed dollar amount of benchmark to trade. Positive =>
                        SHORT this much benchmark notional; negative => LONG this
                        much. Magnitude is |hedge_ratio| * portfolio_value.
        hedge_units:    Whole/fractional benchmark units implied by the notional
                        at `benchmark_price`, or None if no price was supplied.
                        Sign matches hedge_notional (positive => short units).
    """

    beta: float
    hedge_ratio: float
    hedge_notional: float
    hedge_units: Optional[float]


def hedge_ratio(portfolio_beta: float, target_beta: float = 0.0) -> float:
    """
    Fraction of portfolio value to SHORT in the benchmark to move the book from
    `portfolio_beta` to `target_beta`:

        hedge_ratio = portfolio_beta - target_beta

    For full neutralization (target_beta = 0) this is just the portfolio beta:
    a beta-1.3 book shorts 1.3x its value in the benchmark. A positive result
    means short the benchmark; a negative result means add benchmark exposure.
    """
    return portfolio_beta - target_beta


def beta_hedge(
    asset_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    portfolio_value: float,
    *,
    target_beta: float = 0.0,
    benchmark_price: Optional[float] = None,
) -> Optional[BetaHedge]:
    """
    Estimate the portfolio's beta to the benchmark and size the hedge that drives
    the book to `target_beta` (0.0 = market-neutral by default).

    Args:
        asset_returns:     Portfolio (or asset) return series, oldest-first.
        benchmark_returns: Benchmark return series, oldest-first.
        portfolio_value:   Current market value of the book being hedged. Must be
                           non-negative; a zero book needs no hedge.
        target_beta:       Desired residual beta after hedging (default 0.0).
        benchmark_price:   Optional per-unit benchmark price; if given (> 0) the
                           result includes hedge_units (notional / price).

    Returns:
        A BetaHedge, or None when beta cannot be estimated (insufficient or
        degenerate data) or when portfolio_value is negative (fail closed).

    Sign convention (hedge_notional / hedge_units): positive => SHORT the
    benchmark, negative => LONG it.
    """
    if portfolio_value < 0:
        return None

    b = beta(asset_returns, benchmark_returns)
    if b is None:
        return None

    ratio = hedge_ratio(b, target_beta)
    notional = ratio * portfolio_value

    units: Optional[float] = None
    if benchmark_price is not None and benchmark_price > 0:
        units = notional / benchmark_price

    return BetaHedge(
        beta=b,
        hedge_ratio=ratio,
        hedge_notional=notional,
        hedge_units=units,
    )
