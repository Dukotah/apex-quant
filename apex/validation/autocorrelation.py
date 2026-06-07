"""
apex.validation.autocorrelation
===============================
Serial-dependence diagnostics for a return series.

A real edge that compounds is fine, but *predictable* serial dependence in
returns is a red flag: it usually means a backtest is exploiting structure that
won't survive contact with a live market (autocorrelated noise, bid/ask bounce,
overlapping windows, look-ahead leakage), or it signals that the i.i.d.
assumption baked into our Sharpe/Monte-Carlo math is violated.

This module provides:
  1. ``autocorrelation`` — the lag-k sample autocorrelation of a series.
  2. ``autocorrelation_function`` — the ACF for lags 1..max_lag.
  3. ``ljung_box`` — a simple Ljung-Box-style portmanteau statistic that pools
     the first ``lags`` autocorrelations into one number testing the joint null
     "no serial correlation up to lag k."

Deliberately dependency-light (stdlib math + statistics) so it runs anywhere,
including the free GitHub Actions runner, with no heavy installs. All functions
are pure and deterministic given their inputs, and fail closed (return None /
empty) on insufficient data rather than emitting garbage.

Statistical/metric code here uses float to match apex/validation/metrics.py.
Tested in tests/test_autocorrelation.py against hand-computed values.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Sequence


def autocorrelation(series: Sequence[float], lag: int) -> float | None:
    """
    Sample autocorrelation of ``series`` at the given ``lag``.

    Uses the standard biased estimator (the one Ljung-Box assumes), dividing both
    the lag-k autocovariance and the variance by N (the full series length):

        r_k = sum_{t=k+1..N} (x_t - mean)(x_{t-k} - mean)
              / sum_{t=1..N}   (x_t - mean)^2

    Args:
        series: the return (or value) series.
        lag: the lag k. lag 0 is trivially 1.0.

    Returns:
        The autocorrelation in [-1, 1], or None when it is undefined:
        lag < 0, fewer than lag+2 points, or zero variance (a constant series).
    """
    if lag < 0:
        return None
    n = len(series)
    if lag == 0:
        # Defined as 1.0 whenever variance exists; None for a constant/empty series.
        if n < 2:
            return None
        mean = statistics.fmean(series)
        denom = sum((x - mean) ** 2 for x in series)
        return None if denom == 0 else 1.0
    # Need at least one (x_t, x_{t-lag}) pair AND >= 2 points for a variance.
    if n < lag + 2:
        return None
    mean = statistics.fmean(series)
    denom = sum((x - mean) ** 2 for x in series)
    if denom == 0:
        return None
    numer = sum((series[t] - mean) * (series[t - lag] - mean) for t in range(lag, n))
    return numer / denom


def autocorrelation_function(series: Sequence[float], max_lag: int) -> list[float]:
    """
    The autocorrelation function (ACF) for lags 1..max_lag inclusive.

    Returns a list of length max_lag where element i is the lag-(i+1)
    autocorrelation. Lags that are undefined (insufficient data, zero variance)
    are reported as 0.0 so the output length is always predictable. Returns an
    empty list when max_lag < 1.
    """
    if max_lag < 1:
        return []
    out: list[float] = []
    for k in range(1, max_lag + 1):
        ac = autocorrelation(series, k)
        out.append(0.0 if ac is None else ac)
    return out


@dataclass(frozen=True)
class LjungBoxResult:
    """Outcome of a Ljung-Box-style portmanteau test."""

    statistic: float  # the Q statistic
    lags: int  # number of lags pooled
    dof: int  # degrees of freedom (== lags here)
    autocorrelations: tuple[float, ...]  # r_1 .. r_lags actually used
    significant: bool  # True => serial correlation detected (reject null)

    def summary(self) -> str:
        verdict = "SERIAL CORRELATION" if self.significant else "no serial correlation"
        return f"Ljung-Box [{verdict}]: Q={self.statistic:.4f} over {self.lags} lag(s)"


# Upper-tail chi-squared critical values at the 5% level, indexed by degrees of
# freedom (dof -> critical value). Lets us flag significance without scipy. Only
# the first handful of lags are typically tested; we cover dof 1..20.
_CHI2_95: dict[int, float] = {
    1: 3.841,
    2: 5.991,
    3: 7.815,
    4: 9.488,
    5: 11.070,
    6: 12.592,
    7: 14.067,
    8: 15.507,
    9: 16.919,
    10: 18.307,
    11: 19.675,
    12: 21.026,
    13: 22.362,
    14: 23.685,
    15: 24.996,
    16: 26.296,
    17: 27.587,
    18: 28.869,
    19: 30.144,
    20: 31.410,
}


def ljung_box(series: Sequence[float], lags: int = 10) -> LjungBoxResult | None:
    """
    A simple Ljung-Box portmanteau statistic for serial correlation.

    Q = N (N + 2) * sum_{k=1..lags} r_k^2 / (N - k)

    where N is the series length and r_k is the lag-k sample autocorrelation.
    Under the null of no serial correlation, Q is approximately chi-squared with
    ``lags`` degrees of freedom. A large Q means the returns are NOT independent
    — a warning that the strategy's edge (and our i.i.d.-based stats) may be
    unreliable.

    Args:
        series: the return series.
        lags: number of autocorrelation lags to pool (default 10).

    Returns:
        A LjungBoxResult, or None when the test cannot be computed:
        lags < 1, fewer than lags+2 points, or zero variance. ``significant`` is
        set from a built-in 5% chi-squared table when dof <= 20; for larger dof
        (untabulated) it falls back to False (fail closed: don't cry wolf).
    """
    if lags < 1:
        return None
    n = len(series)
    if n < lags + 2:
        return None

    acs: list[float] = []
    q = 0.0
    for k in range(1, lags + 1):
        r_k = autocorrelation(series, k)
        if r_k is None:
            # Undefined (e.g. zero variance) => can't form the statistic.
            return None
        acs.append(r_k)
        q += (r_k * r_k) / (n - k)
    q *= n * (n + 2)

    crit = _CHI2_95.get(lags)
    significant = bool(crit is not None and q > crit)

    return LjungBoxResult(
        statistic=q,
        lags=lags,
        dof=lags,
        autocorrelations=tuple(acs),
        significant=significant,
    )
