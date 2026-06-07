"""
apex.validation.t_test_returns
==============================
One-sample t-test for whether a return series has a mean greater than zero.

A backtest can show a positive average return that is really just noise. This
module answers the narrow statistical question: "Is the mean return reliably
above zero, or is it indistinguishable from a coin flip?" — via a one-sample,
one-sided Student's t-test (H0: mean <= 0, H1: mean > 0).

We deliberately avoid scipy: the p-value is computed from the t-distribution's
CDF using a stdlib-only regularized incomplete beta function. This keeps the
module runnable on the free CI runner with no extra installs, at the cost of a
tiny approximation error (well under typical decision thresholds).

All functions are pure and deterministic given their inputs. Statistical code,
so floats (matching apex/validation/metrics.py), never datetime.now() or RNG.
Tested in tests/test_t_test_returns.py against hand-computed values.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class TTestResult:
    """Outcome of the one-sample, one-sided t-test (H1: mean return > 0)."""

    n: int  # number of observations used
    mean: float  # sample mean return
    std: float  # sample standard deviation (Bessel-corrected)
    t_statistic: float  # the t-statistic
    df: int  # degrees of freedom (n - 1)
    p_value: float  # one-sided P(T >= t | H0: mean <= 0)
    significant: bool  # p_value < significance threshold

    def summary(self) -> str:
        verdict = "SIGNIFICANT" if self.significant else "not significant"
        return (
            f"t-test [{verdict}]: t={self.t_statistic:.4f}, "
            f"df={self.df}, p={self.p_value:.4f}, "
            f"mean={self.mean:.6f} over n={self.n}"
        )


def _betacf(a: float, b: float, x: float) -> float:
    """
    Continued-fraction expansion for the incomplete beta function (Lentz's
    method). Adapted from Numerical Recipes; pure stdlib float math.
    """
    fpmin = 1e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3.0e-12:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b), pure stdlib."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(ln_beta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_sf(t: float, df: int) -> float:
    """
    Survival function P(T >= t) for a Student's t-distribution with `df`
    degrees of freedom, computed via the regularized incomplete beta function.

    Returns a value in [0, 1]. df must be >= 1.
    """
    if df < 1:
        return float("nan")
    x = df / (df + t * t)
    # Two-tailed tail mass beyond |t| is I_x(df/2, 1/2); halve and place by sign.
    tail = 0.5 * _betai(df / 2.0, 0.5, x)
    if t > 0.0:
        return tail
    return 1.0 - tail


def t_test_mean_gt_zero(
    returns: Sequence[float],
    significance: float = 0.05,
) -> TTestResult | None:
    """
    One-sample, one-sided t-test of H0: mean(returns) <= 0 vs H1: mean > 0.

    Args:
        returns: a series of returns (or any measurements). Needs >= 2 points
            for the variance to be defined.
        significance: p-value threshold for `significant` (default 0.05).

    Returns:
        A TTestResult, or None if there is insufficient data (< 2 points).

    Behavior at the degenerate edges (fail closed):
      * Zero sample variance with a positive mean -> t = +inf, p = 0.0
        (a perfectly consistent positive sample is significant).
      * Zero sample variance with a mean <= 0 -> t = 0/-inf, p = 1.0/0... but
        we treat mean <= 0 as never significant for the "> 0" hypothesis:
        a non-positive mean cannot be evidence the mean exceeds zero.
    """
    n = len(returns)
    if n < 2:
        return None

    mean = statistics.fmean(returns)
    std = statistics.stdev(returns)  # Bessel-corrected (n - 1) sample std
    df = n - 1

    if std == 0.0:
        # No spread. The sample is a constant equal to its mean.
        if mean > 0.0:
            t_stat = float("inf")
            p_value = 0.0
        else:
            # Constant mean <= 0: no evidence mean > 0.
            t_stat = float("-inf") if mean < 0.0 else 0.0
            p_value = 1.0
        return TTestResult(
            n=n,
            mean=mean,
            std=std,
            t_statistic=t_stat,
            df=df,
            p_value=p_value,
            significant=p_value < significance,
        )

    standard_error = std / math.sqrt(n)
    t_stat = mean / standard_error
    p_value = student_t_sf(t_stat, df)

    return TTestResult(
        n=n,
        mean=mean,
        std=std,
        t_statistic=t_stat,
        df=df,
        p_value=p_value,
        significant=p_value < significance,
    )
