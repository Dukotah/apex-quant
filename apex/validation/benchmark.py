"""
apex.validation.benchmark
=========================
Benchmark-relative performance metrics. Where metrics.py judges a strategy in
isolation, this module judges it *against a yardstick* (SPY, a 60/40 blend, an
equal-weight basket — whatever the benchmark return series represents).

These answer the question every allocator actually asks: "Did this strategy
earn its keep relative to just buying the benchmark?" Beta tells you how much
market exposure you took, alpha tells you what you added on top, and the
capture ratios tell you whether you participated in up moves while ducking
down moves.

Same design as metrics.py: pure, deterministic, dependency-light (stdlib math
+ statistics only). Statistical code, so float is the correct numeric type here
(mirrors metrics.py — Decimal is for money/prices/quantities, not stats).

All series are period-over-period RETURNS (not equity curves). Convert with
metrics.returns_from_equity first if you have curves.

Length handling: the two series MUST be the same length. A mismatch is almost
always a silent alignment bug (different date ranges, a missing bar), so these
functions RAISE ValueError rather than guess how to align them. Align upstream.

Tested in tests/test_benchmark.py against hand-computed values.
"""
from __future__ import annotations

import math
import statistics
from typing import Sequence

TRADING_DAYS_PER_YEAR = 252

# A flat benchmark has zero variance, but float drift in fmean can leave a
# vanishingly small residual variance instead of an exact 0. Treat anything at
# or below this as "no variance" so the documented zero-variance behaviour
# actually holds for float inputs.
_VARIANCE_EPSILON = 1e-30


def _require_aligned(strategy: Sequence[float], benchmark: Sequence[float]) -> None:
    """
    Guard: the two return series must be the same, non-trivial length.

    We RAISE on mismatch rather than truncate. Different-length return series
    almost always mean the dates don't line up, and silently aligning to the
    shorter one would produce a confidently-wrong beta/alpha. Fail loud.
    """
    if len(strategy) != len(benchmark):
        raise ValueError(
            f"strategy and benchmark must be the same length "
            f"(got {len(strategy)} and {len(benchmark)}); align them upstream"
        )


def beta(strategy: Sequence[float], benchmark: Sequence[float]) -> float:
    """
    Beta = Cov(strategy, benchmark) / Var(benchmark).

    Sensitivity of the strategy's returns to the benchmark's. 1.0 = moves
    one-for-one with the market; 2.0 = twice as volatile (a 2x-levered clone
    of the benchmark has beta 2.0); 0.0 = market-neutral.

    Uses population covariance/variance (consistent denominators, so they
    cancel — sample vs population makes no difference to the ratio).

    Returns 0.0 if the benchmark has zero (or float-negligible) variance (flat
    benchmark → beta is undefined; 0.0 is the safe, documented choice) or there
    are < 2 points.
    """
    _require_aligned(strategy, benchmark)
    if len(benchmark) < 2:
        return 0.0
    mean_s = statistics.fmean(strategy)
    mean_b = statistics.fmean(benchmark)
    cov = sum((s - mean_s) * (b - mean_b) for s, b in zip(strategy, benchmark))
    var_b = sum((b - mean_b) ** 2 for b in benchmark)
    if var_b <= _VARIANCE_EPSILON:
        return 0.0
    return cov / var_b


def alpha(
    strategy: Sequence[float],
    benchmark: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    Annualized Jensen's alpha — the return earned beyond what beta-exposure to
    the benchmark would predict (CAPM):

        per-period alpha = mean(strategy) - [rf + beta * (mean(benchmark) - rf)]
        annualized alpha = per-period alpha * periods_per_year

    `risk_free_rate` is an ANNUAL rate; it's de-annualized internally to a
    per-period rate. A strategy that is exactly a scaled copy of the benchmark
    has alpha ~0 (all its return is explained by beta).

    Returns 0.0 for < 2 points. Note: if the benchmark is flat, beta is 0, so
    alpha collapses to (mean strategy return − rf) annualized, which is correct.
    """
    _require_aligned(strategy, benchmark)
    if len(strategy) < 2:
        return 0.0
    per_period_rf = risk_free_rate / periods_per_year
    b = beta(strategy, benchmark)
    mean_s = statistics.fmean(strategy)
    mean_b = statistics.fmean(benchmark)
    per_period_alpha = mean_s - (per_period_rf + b * (mean_b - per_period_rf))
    return per_period_alpha * periods_per_year


def tracking_error(
    strategy: Sequence[float],
    benchmark: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    Annualized tracking error: the standard deviation of the active return
    (strategy − benchmark), scaled by sqrt(periods_per_year).

    How much the strategy's path diverges from the benchmark. A strategy
    identical to the benchmark has tracking error 0.

    Returns 0.0 for < 2 points. Uses population stdev (pstdev) to match the
    Sharpe/Sortino convention in metrics.py.
    """
    _require_aligned(strategy, benchmark)
    if len(strategy) < 2:
        return 0.0
    active = [s - b for s, b in zip(strategy, benchmark)]
    return statistics.pstdev(active) * math.sqrt(periods_per_year)


def information_ratio(
    strategy: Sequence[float],
    benchmark: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    Information ratio = annualized active return / tracking error.

        annualized active return = mean(strategy − benchmark) * periods_per_year
        IR = annualized active return / annualized tracking error

    The Sharpe ratio of the active bet: excess-over-benchmark return per unit of
    benchmark-relative risk taken to get it. A strategy identical to the
    benchmark has zero tracking error AND zero active return — IR is 0.0 here
    (undefined 0/0; 0.0 is the safe documented choice).

    Returns 0.0 for < 2 points or zero tracking error.
    """
    _require_aligned(strategy, benchmark)
    if len(strategy) < 2:
        return 0.0
    te = tracking_error(strategy, benchmark, periods_per_year)
    if te == 0:
        return 0.0
    active = [s - b for s, b in zip(strategy, benchmark)]
    annualized_active = statistics.fmean(active) * periods_per_year
    return annualized_active / te


def up_capture(strategy: Sequence[float], benchmark: Sequence[float]) -> float:
    """
    Up-capture ratio: mean strategy return / mean benchmark return, computed
    over only the periods where the BENCHMARK was up (> 0).

    1.0 = captured 100% of up moves; > 1.0 = beat the benchmark on up days;
    < 1.0 = lagged. A 2x-scaled clone has up-capture 2.0.

    Returns 0.0 if there are no up periods, or if the mean benchmark up-return
    is 0 (degenerate). Not annualized — it's a unitless ratio of averages.
    """
    _require_aligned(strategy, benchmark)
    up_s = [s for s, b in zip(strategy, benchmark) if b > 0]
    up_b = [b for b in benchmark if b > 0]
    if not up_b:
        return 0.0
    mean_b = statistics.fmean(up_b)
    if mean_b == 0:
        return 0.0
    return statistics.fmean(up_s) / mean_b


def down_capture(strategy: Sequence[float], benchmark: Sequence[float]) -> float:
    """
    Down-capture ratio: mean strategy return / mean benchmark return, computed
    over only the periods where the BENCHMARK was down (< 0).

    LOWER is better here: 1.0 = took the full loss with the market; < 1.0 =
    cushioned the downside (the goal); > 1.0 = lost more than the market.
    A 2x-scaled clone has down-capture 2.0.

    Returns 0.0 if there are no down periods, or if the mean benchmark
    down-return is 0 (degenerate). Not annualized — a unitless ratio of averages.
    """
    _require_aligned(strategy, benchmark)
    down_s = [s for s, b in zip(strategy, benchmark) if b < 0]
    down_b = [b for b in benchmark if b < 0]
    if not down_b:
        return 0.0
    mean_b = statistics.fmean(down_b)
    if mean_b == 0:
        return 0.0
    return statistics.fmean(down_s) / mean_b
