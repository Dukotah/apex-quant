"""
apex.analytics.performance_summary
==================================
One-call performance digest. Turns a single period-return series into the
handful of numbers a human actually reads first: total and annualized return,
Sharpe, Sortino, max drawdown, Calmar, and win rate.

This is a thin, deterministic convenience layer over apex.validation.metrics —
it does no statistics of its own beyond reconstructing an equity curve from the
returns so the equity-curve-based metrics (drawdown, CAGR, Calmar) can be
reused unchanged. Following the convention of the metrics layer it operates on
float, not Decimal (these are reporting statistics, not money math).

Pure and deterministic given its inputs. No I/O. Tested in
tests/test_performance_summary.py against hand-computed values.
"""
from __future__ import annotations

from typing import Dict, Sequence

from apex.validation.metrics import (
    TRADING_DAYS_PER_YEAR,
    annualized_return,
    calmar_ratio,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    total_return,
    win_rate,
)

# The ordered set of keys every summary dict carries. Exposed so callers can
# build stable tables/headers without guessing the schema.
SUMMARY_KEYS: tuple[str, ...] = (
    "total_return",
    "annualized_return",
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown",
    "calmar_ratio",
    "win_rate",
    "num_periods",
)


def equity_curve_from_returns(
    returns: Sequence[float],
    starting_equity: float = 1.0,
) -> list[float]:
    """
    Reconstruct an equity curve from a period-return series.

    The curve always begins at ``starting_equity`` and then compounds each
    return: equity[i+1] = equity[i] * (1 + returns[i]). An empty return series
    yields a single-point curve [starting_equity] (no growth, no drawdown).
    """
    curve: list[float] = [starting_equity]
    for r in returns:
        curve.append(curve[-1] * (1.0 + r))
    return curve


def performance_summary(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> Dict[str, float]:
    """
    Compute the headline performance metrics from a period-return series.

    Args:
        returns: Period-over-period returns as fractions (0.01 = +1%). For a
            daily strategy these are daily returns; ``periods_per_year`` should
            match the sampling frequency.
        risk_free_rate: Annual risk-free rate used by Sharpe/Sortino (default 0).
        periods_per_year: Periods per year for annualization (default 252).

    Returns:
        A dict with these keys (all float):
          - total_return:       cumulative compounded return (fraction)
          - annualized_return:  CAGR implied by the curve (fraction)
          - sharpe_ratio:       annualized Sharpe
          - sortino_ratio:      annualized Sortino (downside-only)
          - max_drawdown:       worst peak-to-trough decline (positive fraction)
          - calmar_ratio:       annualized_return / max_drawdown
          - win_rate:           fraction of periods with a positive return
          - num_periods:        number of return observations (as float)

    Insufficient data is handled gracefully: an empty or single-period series
    returns zeros across the board rather than raising or producing garbage.
    The underlying metrics functions each fail closed to 0.0 when undefined
    (e.g. zero variance, zero drawdown), so this never divides by zero.
    """
    curve = equity_curve_from_returns(returns)
    return {
        "total_return": total_return(curve),
        "annualized_return": annualized_return(curve, periods_per_year),
        "sharpe_ratio": sharpe_ratio(returns, risk_free_rate, periods_per_year),
        "sortino_ratio": sortino_ratio(returns, risk_free_rate, periods_per_year),
        "max_drawdown": max_drawdown(curve),
        "calmar_ratio": calmar_ratio(curve, periods_per_year),
        "win_rate": win_rate(returns),
        "num_periods": float(len(returns)),
    }
