"""
apex.analytics.benchmark_comparison
===================================
Side-by-side comparison of a strategy against a benchmark (e.g. SPY). Answers
the question a human asks first: "did this actually beat just holding the
index, and how much of that was real edge versus just market exposure?"

It pairs each side's headline performance digest (total/annualized return,
Sharpe, drawdown, ...) with the relational metrics that only make sense across
two series:

  - beta:          sensitivity of strategy returns to benchmark returns
                   (cov / var). 1.0 ≈ moves with the market, <1 defensive.
  - alpha:         annualized CAPM excess return — the return left over after
                   stripping out the part explained by beta * market.
  - up_capture:    how much of the benchmark's UP moves the strategy captured
                   (1.0 = matched, >1 = amplified, <1 = lagged on the way up).
  - down_capture:  how much of the benchmark's DOWN moves the strategy ate
                   (<1 = cushioned losses — what you want).
  - correlation:   Pearson correlation of the two return series.
  - tracking_error: annualized stdev of the active (strategy - benchmark)
                   return series.

This is a thin, deterministic layer over apex.validation.metrics and
apex.analytics.performance_summary — it does no money math. Following the
convention of the metrics layer it operates on float, not Decimal (these are
reporting statistics, not P&L). Pure, no I/O, deterministic given its inputs.
Tested in tests/test_benchmark_comparison.py against hand-computed values.
"""
from __future__ import annotations

import math
import statistics
from typing import Dict, Sequence

from apex.analytics.performance_summary import (
    equity_curve_from_returns,
    performance_summary,
)
from apex.validation.metrics import (
    TRADING_DAYS_PER_YEAR,
    annualized_return,
    correlation,
)

# The ordered set of relational keys the comparison carries (the cross-series
# numbers, on top of each side's full performance_summary). Exposed so callers
# can build stable tables without guessing the schema.
RELATIVE_KEYS: tuple[str, ...] = (
    "beta",
    "alpha",
    "correlation",
    "up_capture",
    "down_capture",
    "tracking_error",
    "excess_return",
    "num_periods",
)


def beta(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> float:
    """
    Beta of the strategy versus the benchmark: cov(s, b) / var(b).

    Beta measures how much the strategy moves for a unit move in the benchmark.
    1.0 ≈ tracks the market, <1 defensive, >1 leveraged exposure, ~0 market
    neutral. Series are truncated to their common length.

    Returns 0.0 if there are fewer than two paired observations or the
    benchmark has zero variance (beta undefined — fail closed to 0).
    """
    n = min(len(strategy_returns), len(benchmark_returns))
    if n < 2:
        return 0.0
    s = strategy_returns[:n]
    b = benchmark_returns[:n]
    mean_s = statistics.fmean(s)
    mean_b = statistics.fmean(b)
    cov = sum((x - mean_s) * (y - mean_b) for x, y in zip(s, b))
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_b == 0:
        return 0.0
    return cov / var_b


def alpha(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    Annualized CAPM alpha: the strategy's return in excess of what its beta
    exposure to the benchmark would explain.

        alpha = R_strategy - [R_f + beta * (R_benchmark - R_f)]

    where the R's are annualized (compounded) returns. Positive alpha is the
    genuine edge; a strategy that just leverages the index has beta but ~0
    alpha. Series are truncated to their common length.

    Returns 0.0 if there are fewer than two paired observations.
    """
    n = min(len(strategy_returns), len(benchmark_returns))
    if n < 2:
        return 0.0
    s = strategy_returns[:n]
    b = benchmark_returns[:n]
    r_s = annualized_return(equity_curve_from_returns(s), periods_per_year)
    r_b = annualized_return(equity_curve_from_returns(b), periods_per_year)
    bta = beta(s, b)
    return r_s - (risk_free_rate + bta * (r_b - risk_free_rate))


def _capture(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    upside: bool,
) -> float:
    """
    Capture ratio over the benchmark periods that were up (upside=True) or down
    (upside=False). Defined as the compounded strategy return over those
    periods divided by the compounded benchmark return over the same periods.

    Returns 0.0 if there are no qualifying periods or the benchmark's
    compounded move over them is exactly zero (undefined — fail closed).
    """
    n = min(len(strategy_returns), len(benchmark_returns))
    if n < 1:
        return 0.0
    s_growth = 1.0
    b_growth = 1.0
    count = 0
    for x, y in zip(strategy_returns[:n], benchmark_returns[:n]):
        if (y > 0.0) if upside else (y < 0.0):
            s_growth *= 1.0 + x
            b_growth *= 1.0 + y
            count += 1
    if count == 0:
        return 0.0
    bench_move = b_growth - 1.0
    if bench_move == 0.0:
        return 0.0
    return (s_growth - 1.0) / bench_move


def up_capture(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> float:
    """
    Up-capture ratio: compounded strategy return over the benchmark's UP
    periods / compounded benchmark return over those same periods.

    1.0 = matched the market's gains, >1 = amplified them, <1 = lagged on the
    way up. Returns 0.0 if there are no up periods.
    """
    return _capture(strategy_returns, benchmark_returns, upside=True)


def down_capture(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> float:
    """
    Down-capture ratio: compounded strategy return over the benchmark's DOWN
    periods / compounded benchmark return over those same periods.

    A value < 1 means the strategy cushioned the market's losses (what you
    want); > 1 means it lost more than the index on down days. Note the sign:
    a positive down-capture with a falling benchmark means the strategy also
    fell. Returns 0.0 if there are no down periods.
    """
    return _capture(strategy_returns, benchmark_returns, upside=False)


def tracking_error(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    Annualized tracking error: the standard deviation of the active-return
    series (strategy - benchmark), scaled to a year. How far the strategy
    wanders from the benchmark. Series are truncated to their common length.

    Returns 0.0 if there are fewer than two paired observations.
    """
    n = min(len(strategy_returns), len(benchmark_returns))
    if n < 2:
        return 0.0
    active = [x - y for x, y in zip(strategy_returns[:n], benchmark_returns[:n])]
    return statistics.pstdev(active) * math.sqrt(periods_per_year)


def benchmark_comparison(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> Dict[str, object]:
    """
    Build a side-by-side comparison of a strategy versus its benchmark.

    Args:
        strategy_returns: Period-over-period strategy returns (0.01 = +1%).
        benchmark_returns: Period returns of the benchmark (e.g. SPY), same
            sampling frequency. Series are truncated to their common length so
            each pair lines up.
        risk_free_rate: Annual risk-free rate for Sharpe/Sortino and CAPM alpha.
        periods_per_year: Periods per year for annualization (default 252).

    Returns:
        A dict with three top-level keys:
          - "strategy":  full performance_summary dict for the strategy
          - "benchmark": full performance_summary dict for the benchmark
          - "relative":  the cross-series metrics (the RELATIVE_KEYS):
                beta, alpha, correlation, up_capture, down_capture,
                tracking_error, excess_return (strategy total return minus
                benchmark total return), and num_periods (paired observations).

    Both per-side summaries are computed on the COMMON-LENGTH (paired) slice so
    the side-by-side is apples-to-apples. Insufficient data is handled
    gracefully: empty/short series yield zeroed metrics rather than raising,
    because every underlying function fails closed to 0.0 when undefined.
    """
    n = min(len(strategy_returns), len(benchmark_returns))
    s = list(strategy_returns[:n])
    b = list(benchmark_returns[:n])

    strat_summary = performance_summary(s, risk_free_rate, periods_per_year)
    bench_summary = performance_summary(b, risk_free_rate, periods_per_year)

    relative: Dict[str, float] = {
        "beta": beta(s, b),
        "alpha": alpha(s, b, risk_free_rate, periods_per_year),
        "correlation": correlation(s, b),
        "up_capture": up_capture(s, b),
        "down_capture": down_capture(s, b),
        "tracking_error": tracking_error(s, b, periods_per_year),
        "excess_return": strat_summary["total_return"] - bench_summary["total_return"],
        "num_periods": float(n),
    }

    return {
        "strategy": strat_summary,
        "benchmark": bench_summary,
        "relative": relative,
    }
