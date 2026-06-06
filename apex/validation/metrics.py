"""
apex.validation.metrics
=======================
The statistical foundation every Gauntlet gate builds on. Pure functions that
turn a series of returns or trade results into the risk-adjusted metrics that
actually matter.

Deliberately dependency-light (stdlib math + statistics) so it runs anywhere,
including the free GitHub Actions runner, with no heavy installs.

All functions are pure and deterministic given their inputs. Tested in
tests/test_metrics.py against hand-computed values.
"""

from __future__ import annotations

import math
import statistics
from typing import Sequence

TRADING_DAYS_PER_YEAR = 252


def total_return(equity_curve: Sequence[float]) -> float:
    """Cumulative return of an equity curve, as a fraction (0.20 = +20%)."""
    if len(equity_curve) < 2 or equity_curve[0] == 0:
        return 0.0
    return equity_curve[-1] / equity_curve[0] - 1.0


def returns_from_equity(equity_curve: Sequence[float]) -> list[float]:
    """Convert an equity curve into a list of period-over-period returns."""
    out: list[float] = []
    for prev, curr in zip(equity_curve, equity_curve[1:]):
        if prev == 0:
            out.append(0.0)
        else:
            out.append(curr / prev - 1.0)
    return out


def sharpe_ratio(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    Annualized Sharpe ratio. The headline risk-adjusted return metric.

    Sharpe = (mean excess return / std of returns) * sqrt(periods_per_year)

    Returns 0.0 if there's no variance (can't divide by zero) or too few points.
    A Sharpe < 1 is weak, 1-2 is decent, > 2 is excellent (and rare, and suspect).
    """
    if len(returns) < 2:
        return 0.0
    per_period_rf = risk_free_rate / periods_per_year
    excess = [r - per_period_rf for r in returns]
    mean = statistics.fmean(excess)
    sd = statistics.pstdev(excess)
    if sd == 0:
        return 0.0
    return (mean / sd) * math.sqrt(periods_per_year)


def sortino_ratio(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    Like Sharpe but only penalizes DOWNSIDE volatility. Upside swings shouldn't
    count as 'risk'. Often a fairer measure for asymmetric strategies.
    """
    if len(returns) < 2:
        return 0.0
    per_period_rf = risk_free_rate / periods_per_year
    excess = [r - per_period_rf for r in returns]
    downside = [min(0.0, r) for r in excess]
    downside_dev = math.sqrt(statistics.fmean([d * d for d in downside]))
    if downside_dev == 0:
        return 0.0
    return (statistics.fmean(excess) / downside_dev) * math.sqrt(periods_per_year)


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """
    Worst peak-to-trough decline, as a positive fraction (0.25 = -25% drawdown).
    The number that actually determines whether you can stomach a strategy and
    how much you can size into it.
    """
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for value in equity_curve:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak
            if dd > worst:
                worst = dd
    return worst


def profit_factor(trade_returns: Sequence[float]) -> float:
    """
    Gross profit / gross loss across trades. > 1 means profitable.
    > 1.3 is our minimum bar; > 2 is strong. inf if there are no losing trades
    (treat with suspicion — usually too few trades).
    """
    gross_profit = sum(r for r in trade_returns if r > 0)
    gross_loss = abs(sum(r for r in trade_returns if r < 0))
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def win_rate(trade_returns: Sequence[float]) -> float:
    """Fraction of trades that were profitable (0.0-1.0)."""
    if not trade_returns:
        return 0.0
    wins = sum(1 for r in trade_returns if r > 0)
    return wins / len(trade_returns)


def annualized_return(
    equity_curve: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Compound annual growth rate implied by the equity curve."""
    if len(equity_curve) < 2 or equity_curve[0] <= 0:
        return 0.0
    periods = len(equity_curve) - 1
    growth = equity_curve[-1] / equity_curve[0]
    if growth <= 0:
        return -1.0
    return growth ** (periods_per_year / periods) - 1.0


def calmar_ratio(
    equity_curve: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Annualized return / max drawdown. Reward per unit of worst-case pain."""
    mdd = max_drawdown(equity_curve)
    if mdd == 0:
        return 0.0
    return annualized_return(equity_curve, periods_per_year) / mdd


def correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """
    Pearson correlation between two return series. Used by Gate 7 to confirm a
    strategy diversifies (low correlation to SPY and to other approved strategies).
    Returns 0.0 if undefined.
    """
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a, b = a[:n], b[:n]
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    denom = math.sqrt(var_a * var_b)
    if denom == 0:
        return 0.0
    return cov / denom
