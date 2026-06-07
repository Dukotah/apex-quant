"""
apex.validation.rolling_sharpe
==============================
Rolling Sharpe ratio: the annualized Sharpe computed over a sliding window as it
moves across a return series. A single headline Sharpe (see
``apex.validation.metrics.sharpe_ratio``) hides *when* an edge was working. A
rolling series exposes regime shifts and alpha decay — a strategy whose Sharpe
was 2.0 in 2021 and -0.5 since is a very different animal from one that holds up.

Deliberately dependency-light (stdlib ``math`` + ``statistics``) so it runs
anywhere, including the free GitHub Actions runner, with no heavy installs. This
is statistical/metric code, so it follows the float convention of
``apex.validation.metrics`` rather than Decimal.

All functions are pure and deterministic given their inputs, and degrade
gracefully on short windows (returning ``None`` for a window with no defined
Sharpe rather than garbage). Tested in tests/test_rolling_sharpe.py against
hand-computed values.
"""
from __future__ import annotations

import math
import statistics
from typing import Optional, Sequence

TRADING_DAYS_PER_YEAR = 252


def window_sharpe(
    window: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> Optional[float]:
    """
    Annualized Sharpe ratio for a single window of returns.

    Mirrors ``apex.validation.metrics.sharpe_ratio`` but returns ``None`` (rather
    than 0.0) when the Sharpe is undefined, so callers can distinguish "no edge"
    from "not enough data / no variance". Use ``sharpe_ratio`` directly if you
    want the 0.0-on-undefined behavior.

    Returns None if there are fewer than 2 points or the returns have zero
    variance (can't divide by zero).
    """
    if len(window) < 2:
        return None
    per_period_rf = risk_free_rate / periods_per_year
    excess = [r - per_period_rf for r in window]
    mean = statistics.fmean(excess)
    sd = statistics.pstdev(excess)
    if sd == 0:
        return None
    return (mean / sd) * math.sqrt(periods_per_year)


def rolling_sharpe(
    returns: Sequence[float],
    window: int,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> list[Optional[float]]:
    """
    Rolling annualized Sharpe over a sliding window of ``window`` returns.

    The result has one entry per fully-formed window: for an input of length N
    with a window of W, the output has ``N - W + 1`` entries (empty if N < W).
    The i-th entry is the Sharpe of ``returns[i : i + window]``. Windows whose
    Sharpe is undefined (zero variance) yield ``None`` rather than a fabricated
    number, so downstream code fails closed instead of trusting garbage.

    Args:
        returns: per-period returns as fractions (0.01 = +1%).
        window: number of returns per window (must be >= 2).
        risk_free_rate: annual risk-free rate, de-annualized internally.
        periods_per_year: annualization factor (252 trading days by default).

    Raises:
        ValueError: if ``window`` < 2 (a Sharpe needs at least two points).
    """
    if window < 2:
        raise ValueError("window must be >= 2 to define a Sharpe ratio")
    n = len(returns)
    if n < window:
        return []
    out: list[Optional[float]] = []
    for start in range(0, n - window + 1):
        chunk = returns[start : start + window]
        out.append(window_sharpe(chunk, risk_free_rate, periods_per_year))
    return out


def latest_rolling_sharpe(
    returns: Sequence[float],
    window: int,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> Optional[float]:
    """
    Convenience: the most recent window's Sharpe (the last entry of
    ``rolling_sharpe``), or ``None`` if there isn't a full window of data or the
    final window's Sharpe is undefined.
    """
    if window < 2 or len(returns) < window:
        return None
    return window_sharpe(
        returns[-window:], risk_free_rate, periods_per_year
    )


def rolling_sharpe_stats(
    returns: Sequence[float],
    window: int,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> dict[str, Optional[float]]:
    """
    Summarize the rolling-Sharpe series into a small, useful dict.

    Only the *defined* windows (non-None) feed the summary; this is what tells
    you whether an edge is stable or whether it is concentrated in a few good
    windows. Returns a dict with keys:

        ``count``   number of defined windows (int as float; 0 if none)
        ``mean``    mean rolling Sharpe across defined windows (None if none)
        ``min``     worst window (None if none)
        ``max``     best window (None if none)
        ``last``    most recent defined window's Sharpe, in chronological
                    position — i.e. the final element of the series if defined,
                    else None (None if the series is empty)
        ``positive_fraction`` fraction of defined windows with Sharpe > 0
                    (None if none)
    """
    series = rolling_sharpe(returns, window, risk_free_rate, periods_per_year)
    defined = [s for s in series if s is not None]
    last: Optional[float] = series[-1] if series else None
    if not defined:
        return {
            "count": 0.0,
            "mean": None,
            "min": None,
            "max": None,
            "last": last,
            "positive_fraction": None,
        }
    positives = sum(1 for s in defined if s > 0)
    return {
        "count": float(len(defined)),
        "mean": statistics.fmean(defined),
        "min": min(defined),
        "max": max(defined),
        "last": last,
        "positive_fraction": positives / len(defined),
    }
