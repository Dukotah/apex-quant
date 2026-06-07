"""
apex.validation.regime_split_metrics
=====================================
Split a return series by an externally-provided regime label and compute the
key performance metrics SEPARATELY within each regime.

The whole point: a strategy's headline Sharpe can hide that it makes all its
money in one market regime (e.g. a calm bull) and bleeds in another (e.g. a
high-volatility crash). By aligning per-period regime labels to returns and
re-running the metrics within each bucket, we expose where the edge actually
lives — and whether it survives the regime you fear most.

Regime labels are supplied by the caller (whatever taxonomy they use:
"bull"/"bear", "low_vol"/"high_vol", integer states, etc.). This module is
agnostic about how regimes are detected; it only slices and scores.

Deliberately dependency-light: stdlib + the existing metrics module, so it runs
on the free CI runner. All functions are pure and deterministic given inputs.
Tested in tests/test_regime_split_metrics.py against hand-computed values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Sequence

from apex.validation import metrics


@dataclass(frozen=True)
class RegimeMetrics:
    """Performance metrics computed within a single regime bucket."""

    regime: Hashable
    n_periods: int  # number of return observations in this regime
    fraction: float  # share of all aligned periods this regime covers
    total_return: float  # compounded return of this regime's periods only
    annualized_return: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float  # on the regime-only compounded equity curve
    win_rate: float  # fraction of positive periods
    profit_factor: float  # gross gains / gross losses across periods
    mean_return: float  # arithmetic mean per-period return

    def summary(self) -> str:
        return (
            f"Regime {self.regime!r}: n={self.n_periods} ({self.fraction:.1%}), "
            f"Sharpe={self.sharpe_ratio:.2f}, total={self.total_return:.1%}, "
            f"maxDD={self.max_drawdown:.1%}, win={self.win_rate:.1%}"
        )


def _equity_from_returns(returns: Sequence[float], start: float = 1.0) -> list[float]:
    """Compound a sequence of per-period returns into an equity curve."""
    equity = [start]
    for r in returns:
        equity.append(equity[-1] * (1.0 + r))
    return equity


def split_returns_by_regime(
    returns: Sequence[float],
    regimes: Sequence[Hashable],
) -> dict[Hashable, list[float]]:
    """
    Group returns by their aligned regime label, preserving order within each
    bucket.

    Args:
        returns: per-period returns as fractions (0.01 = +1%).
        regimes: regime label for each corresponding return; must be the same
            length as ``returns`` (element i labels return i).

    Returns a dict mapping each distinct regime label to the ordered list of
    returns observed in that regime. Returns an empty dict if the inputs are
    empty or length-mismatched (fail closed — never silently misalign).
    """
    if len(returns) != len(regimes) or len(returns) == 0:
        return {}
    buckets: dict[Hashable, list[float]] = {}
    for r, label in zip(returns, regimes):
        buckets.setdefault(label, []).append(r)
    return buckets


def metrics_for_returns(
    regime: Hashable,
    regime_returns: Sequence[float],
    total_periods: int,
    periods_per_year: int = metrics.TRADING_DAYS_PER_YEAR,
    risk_free_rate: float = 0.0,
) -> RegimeMetrics:
    """
    Compute the full metric bundle for one regime's returns.

    ``total_periods`` is the count across ALL regimes, used only to compute the
    ``fraction`` field; it does not affect any other metric. Handles the
    insufficient-data case gracefully: with zero returns every metric is its
    neutral value (0.0), never garbage.
    """
    n = len(regime_returns)
    fraction = (n / total_periods) if total_periods > 0 else 0.0
    if n == 0:
        return RegimeMetrics(
            regime=regime,
            n_periods=0,
            fraction=fraction,
            total_return=0.0,
            annualized_return=0.0,
            sharpe_ratio=0.0,
            sortino_ratio=0.0,
            max_drawdown=0.0,
            win_rate=0.0,
            profit_factor=0.0,
            mean_return=0.0,
        )
    equity = _equity_from_returns(regime_returns)
    mean_return = sum(regime_returns) / n
    return RegimeMetrics(
        regime=regime,
        n_periods=n,
        fraction=fraction,
        total_return=metrics.total_return(equity),
        annualized_return=metrics.annualized_return(equity, periods_per_year),
        sharpe_ratio=metrics.sharpe_ratio(regime_returns, risk_free_rate, periods_per_year),
        sortino_ratio=metrics.sortino_ratio(regime_returns, risk_free_rate, periods_per_year),
        max_drawdown=metrics.max_drawdown(equity),
        win_rate=metrics.win_rate(regime_returns),
        profit_factor=metrics.profit_factor(regime_returns),
        mean_return=mean_return,
    )


def regime_split_metrics(
    returns: Sequence[float],
    regimes: Sequence[Hashable],
    periods_per_year: int = metrics.TRADING_DAYS_PER_YEAR,
    risk_free_rate: float = 0.0,
) -> dict[Hashable, RegimeMetrics]:
    """
    Compute the key performance metrics separately for each regime.

    Args:
        returns: per-period returns as fractions, aligned 1:1 with ``regimes``.
        regimes: externally-provided regime label per period (same length).
        periods_per_year: annualization factor (252 trading days by default).
        risk_free_rate: annual risk-free rate passed through to Sharpe/Sortino.

    Returns a dict mapping each distinct regime label to its ``RegimeMetrics``.
    Returns an empty dict on empty or length-mismatched inputs (fail closed).

    Each regime is scored on a fresh equity curve starting at 1.0 using only the
    returns observed in that regime, in their original order. This intentionally
    measures the strategy's character WITHIN a regime, not the cross-regime
    compounding path.
    """
    buckets = split_returns_by_regime(returns, regimes)
    if not buckets:
        return {}
    total_periods = sum(len(v) for v in buckets.values())
    out: dict[Hashable, RegimeMetrics] = {}
    for regime, regime_returns in buckets.items():
        out[regime] = metrics_for_returns(
            regime,
            regime_returns,
            total_periods,
            periods_per_year=periods_per_year,
            risk_free_rate=risk_free_rate,
        )
    return out
