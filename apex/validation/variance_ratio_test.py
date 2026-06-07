"""
apex.validation.variance_ratio_test
===================================
The Lo-MacKinlay (1988) variance ratio test for the random-walk hypothesis.

If price log-returns are a random walk, the variance of a q-period return should
be exactly q times the variance of a one-period return. The variance ratio

    VR(q) = Var(q-period return) / (q * Var(one-period return))

therefore equals 1.0 under the null. A VR(q) > 1 indicates positive serial
correlation / momentum (returns trend); VR(q) < 1 indicates mean reversion
(returns reverse). Lo & MacKinlay give an asymptotically normal z-statistic so
we can attach a p-value and actually reject (or fail to reject) the random walk.

Why a momentum/mean-reversion engine cares: a strategy's premise IS a claim
about the auto-correlation structure of returns. This test tells you whether the
series you are trading actually exhibits the structure your strategy assumes,
*before* you fool yourself with an overfit backtest. It is also a clean way to
spot a price series that is suspiciously well-behaved (a sign of bad data).

Two variance estimators are provided:
  1. Homoskedastic z (z1): assumes constant variance. Tighter, but wrong if
     volatility clusters (it always does in markets).
  2. Heteroskedasticity-robust z (z2): the Lo-MacKinlay correction. Use this one
     for real return series; it does not over-reject when volatility clusters.

This module follows the convention of apex.validation.metrics: it is a pure,
deterministic, dependency-light statistical layer, so it uses float (not Decimal)
to match the surrounding indicator/metric code. No I/O, no wall-clock, no RNG.
Insufficient data returns None rather than garbage (fail closed).

Tested in tests/test_variance_ratio_test.py against hand-computed values.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class VarianceRatioResult:
    """Outcome of a single Lo-MacKinlay variance ratio test at horizon q."""
    q: int                       # the aggregation horizon tested
    n: int                       # number of one-period returns used
    variance_ratio: float        # VR(q); 1.0 under the random-walk null
    z_homoskedastic: float       # z1: assumes constant variance
    z_heteroskedastic: float     # z2: Lo-MacKinlay robust z (use this one)
    p_value: float               # two-sided p-value from the robust z
    rejects_random_walk: bool    # p_value < significance

    def summary(self) -> str:
        if self.variance_ratio > 1.0:
            tilt = "momentum (positive autocorrelation)"
        elif self.variance_ratio < 1.0:
            tilt = "mean reversion (negative autocorrelation)"
        else:
            tilt = "pure random walk"
        verdict = "REJECT random walk" if self.rejects_random_walk else "cannot reject random walk"
        return (
            f"VR(q={self.q})={self.variance_ratio:.4f} [{tilt}], "
            f"z={self.z_heteroskedastic:.3f}, p={self.p_value:.4f} -> {verdict}"
        )


def log_returns(prices: Sequence[float]) -> list[float]:
    """
    Convert a price series into one-period log returns.

    Log returns are the right input for the variance ratio test: they aggregate
    additively (the q-period log return is the sum of q one-period log returns),
    which is exactly the structure the test exploits.

    Non-positive prices have no defined log return; returns an empty list if any
    price is <= 0 (fail closed rather than emit nan/inf garbage).
    """
    if len(prices) < 2:
        return []
    if any(p <= 0 for p in prices):
        return []
    return [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via the error function (stdlib, deterministic)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def two_sided_p_value(z: float) -> float:
    """Two-sided p-value for a standard-normal test statistic."""
    return 2.0 * (1.0 - _normal_cdf(abs(z)))


def variance_ratio(returns: Sequence[float], q: int) -> float | None:
    """
    The raw Lo-MacKinlay variance ratio VR(q) for a series of one-period returns.

    VR(q) = sigma_q^2 / sigma_1^2

    where sigma_1^2 is the (unbiased) variance of one-period returns and
    sigma_q^2 is the overlapping per-period variance estimator built from
    q-period returns. The bias correction m = q*(n-q+1)*(1 - q/n) already
    divides out the extra factor of q, so sigma_q^2 is on a per-period basis and
    VR(q) is its ratio to sigma_1^2 (no further /q). Under the random-walk null
    VR(q) -> 1.

    Returns None if there are too few returns to form the estimator (need at
    least q+1 one-period returns and q >= 2), or if one-period variance is zero
    (ratio undefined). Never returns garbage.
    """
    if q < 2:
        return None
    n = len(returns)
    if n < q + 1:
        return None

    mu = sum(returns) / n

    # Unbiased one-period variance estimator (denominator n - 1).
    var_1 = sum((r - mu) ** 2 for r in returns) / (n - 1)
    if var_1 <= 0.0:
        return None

    # Overlapping q-period variance estimator with the Lo-MacKinlay bias
    # correction m = q * (n - q + 1) * (1 - q / n).
    m = q * (n - q + 1) * (1.0 - q / n)
    if m <= 0.0:
        return None

    sum_sq = 0.0
    for t in range(q - 1, n):
        # q-period return centered on q*mu.
        q_ret = sum(returns[t - q + 1 : t + 1]) - q * mu
        sum_sq += q_ret * q_ret
    # m carries the factor of q, so var_q is already a per-period variance.
    var_q = sum_sq / m

    return var_q / var_1


def lo_mackinlay_test(
    returns: Sequence[float],
    q: int,
    significance: float = 0.05,
) -> VarianceRatioResult | None:
    """
    Full Lo-MacKinlay variance ratio test at horizon q on one-period returns.

    Computes VR(q), the homoskedastic z-statistic (z1), the
    heteroskedasticity-robust z-statistic (z2), and a two-sided p-value from z2.

    Args:
        returns: one-period returns (use log_returns() on a price series).
        q: aggregation horizon (>= 2). VR(q) compares q-period to 1-period var.
        significance: two-sided alpha for the reject decision (default 0.05).

    Returns a VarianceRatioResult, or None if there is insufficient data to form
    a meaningful estimator (fail closed — never approve on garbage).

    The statistics (asymptotic, from Lo & MacKinlay 1988):

      z1 = (VR(q) - 1) / sqrt(phi1),
        phi1 = 2 * (2q - 1) * (q - 1) / (3 * q * n)

      z2 = (VR(q) - 1) / sqrt(phi2),
        phi2 = sum_{j=1}^{q-1} ( 2(q-j)/q )^2 * delta_j
        delta_j = [ sum_t (e_t - mu)^2 (e_{t-j} - mu)^2 ]
                  / [ sum_t (e_t - mu)^2 ]^2
    Both are asymptotically N(0, 1) under the random-walk null.
    """
    if q < 2:
        return None
    n = len(returns)
    if n < q + 1:
        return None

    vr = variance_ratio(returns, q)
    if vr is None:
        return None

    mu = sum(returns) / n
    dev = [r - mu for r in returns]
    sum_dev_sq = sum(d * d for d in dev)
    if sum_dev_sq <= 0.0:
        return None

    # --- Homoskedastic z (z1) ---
    phi1 = 2.0 * (2 * q - 1) * (q - 1) / (3.0 * q * n)
    if phi1 <= 0.0:
        return None
    z1 = (vr - 1.0) / math.sqrt(phi1)

    # --- Heteroskedasticity-robust z (z2) ---
    phi2 = 0.0
    denom = sum_dev_sq * sum_dev_sq
    for j in range(1, q):
        # delta_j: ratio of lag-j squared-deviation cross product to denom.
        cross = 0.0
        for t in range(j, n):
            cross += (dev[t] * dev[t]) * (dev[t - j] * dev[t - j])
        delta_j = cross / denom
        weight = (2.0 * (q - j) / q) ** 2
        phi2 += weight * delta_j
    if phi2 <= 0.0:
        return None
    z2 = (vr - 1.0) / math.sqrt(phi2)

    p = two_sided_p_value(z2)

    return VarianceRatioResult(
        q=q,
        n=n,
        variance_ratio=vr,
        z_homoskedastic=z1,
        z_heteroskedastic=z2,
        p_value=p,
        rejects_random_walk=p < significance,
    )


def variance_ratio_test_from_prices(
    prices: Sequence[float],
    q: int,
    significance: float = 0.05,
) -> VarianceRatioResult | None:
    """
    Convenience wrapper: run the Lo-MacKinlay test directly on a price series.

    Converts prices to log returns then delegates to lo_mackinlay_test. Returns
    None if the price series is too short or contains non-positive prices.
    """
    rets = log_returns(prices)
    if not rets:
        return None
    return lo_mackinlay_test(rets, q, significance)
