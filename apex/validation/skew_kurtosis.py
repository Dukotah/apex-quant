"""
apex.validation.skew_kurtosis
=============================
Distribution-shape diagnostics for a return series: sample **skewness**,
**excess kurtosis**, and the **Jarque-Bera** normality test statistic.

Why this matters for trading validation: Sharpe and most risk metrics implicitly
assume returns are roughly normal. Real strategy returns rarely are — negative
skew (occasional large losses) and fat tails (excess kurtosis > 0) mean the
"realistic worst case" is worse than a normal-based estimate suggests. These
diagnostics let a Gauntlet gate flag a return distribution whose tails make the
headline metrics untrustworthy.

Definitions used (sample / bias-corrected forms, matching scipy defaults):
  - skewness:  Fisher-Pearson standardized moment coefficient, bias-corrected.
  - kurtosis:  EXCESS kurtosis (normal -> 0), bias-corrected (Fisher).
  - Jarque-Bera: JB = (n/6) * (S^2 + (K^2)/4), where S is sample skewness and K
    is excess kurtosis. Under normality JB ~ chi-squared with 2 d.o.f. We report
    the statistic and an approximate p-value from that distribution.

This is statistical/metric code, so — like apex.validation.metrics — it uses
float, not Decimal. Pure stdlib (math + statistics); deterministic; no I/O.
Insufficient-data windows return None rather than garbage (fail closed).

Tested in tests/test_skew_kurtosis.py against hand-computed values.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class ShapeStats:
    """Bundle of distribution-shape diagnostics for a return series."""
    n: int
    mean: float
    std: float                 # sample standard deviation (ddof=1)
    skewness: float            # bias-corrected sample skewness
    excess_kurtosis: float     # bias-corrected EXCESS kurtosis (normal -> 0)
    jarque_bera: float         # JB test statistic (>= 0)
    jb_p_value: float          # approx p-value under chi-sq(2); small => non-normal

    def is_normal(self, significance: float = 0.05) -> bool:
        """True if we CANNOT reject normality at the given level (p >= alpha)."""
        return self.jb_p_value >= significance

    def summary(self) -> str:
        tail = "fat tails" if self.excess_kurtosis > 0 else "thin tails"
        lean = (
            "left-skewed" if self.skewness < 0
            else "right-skewed" if self.skewness > 0
            else "symmetric"
        )
        verdict = "normal-ish" if self.is_normal() else "NON-normal"
        return (
            f"Shape [{verdict}]: skew={self.skewness:+.3f} ({lean}), "
            f"excess kurt={self.excess_kurtosis:+.3f} ({tail}), "
            f"JB={self.jarque_bera:.3f} (p={self.jb_p_value:.4f})"
        )


def _central_moment(values: Sequence[float], mean: float, order: int) -> float:
    """Population central moment of the given order: mean of (x - mean)**order."""
    n = len(values)
    return sum((x - mean) ** order for x in values) / n


def skewness(returns: Sequence[float]) -> Optional[float]:
    """
    Bias-corrected sample skewness (Fisher-Pearson, matching scipy bias=False).

    g1 = m3 / m2**1.5   (the biased estimator)
    G1 = g1 * sqrt(n*(n-1)) / (n-2)   (bias correction)

    Negative => longer/heavier left tail (the dangerous kind for a long book).
    Returns None if there are fewer than 3 points or the series has zero variance
    (skewness undefined), never a garbage value.
    """
    n = len(returns)
    if n < 3:
        return None
    mean = statistics.fmean(returns)
    m2 = _central_moment(returns, mean, 2)
    if m2 == 0:
        return None
    m3 = _central_moment(returns, mean, 3)
    g1 = m3 / (m2 ** 1.5)
    return g1 * math.sqrt(n * (n - 1)) / (n - 2)


def excess_kurtosis(returns: Sequence[float]) -> Optional[float]:
    """
    Bias-corrected sample EXCESS kurtosis (Fisher; a normal distribution -> 0),
    matching scipy.stats.kurtosis(..., fisher=True, bias=False).

    Positive => fatter tails than normal (more extreme moves than a Sharpe-style
    normal assumption expects). Returns None with fewer than 4 points or zero
    variance, never garbage.
    """
    n = len(returns)
    if n < 4:
        return None
    mean = statistics.fmean(returns)
    m2 = _central_moment(returns, mean, 2)
    if m2 == 0:
        return None
    m4 = _central_moment(returns, mean, 4)
    g2 = m4 / (m2 * m2) - 3.0  # biased excess kurtosis
    # Bias correction (same formula scipy uses):
    return ((n - 1) / ((n - 2) * (n - 3))) * ((n + 1) * g2 + 6.0)


def _chi2_2df_sf(x: float) -> float:
    """
    Survival function P(X > x) for a chi-squared distribution with 2 d.o.f.
    Chi-sq(2) is an exponential with mean 2, so the SF is exactly exp(-x/2).
    Clamped to [0, 1]; non-positive x => 1.0.
    """
    if x <= 0:
        return 1.0
    return math.exp(-x / 2.0)


def jarque_bera(returns: Sequence[float]) -> Optional[tuple[float, float]]:
    """
    Jarque-Bera normality test on a return series.

    JB = (n / 6) * (S**2 + (K**2) / 4)
        where S = sample skewness, K = excess kurtosis (BIASED forms are the
        textbook JB inputs; we use the biased moment coefficients here so the
        statistic matches the canonical definition).

    Under the null of normality JB is asymptotically chi-squared with 2 d.o.f.,
    so the p-value is exp(-JB/2). A small p-value (e.g. < 0.05) rejects normality.

    Returns (statistic, p_value), or None if there are fewer than 4 points or the
    series has zero variance (test undefined). Fails closed: never returns garbage.
    """
    n = len(returns)
    if n < 4:
        return None
    mean = statistics.fmean(returns)
    m2 = _central_moment(returns, mean, 2)
    if m2 == 0:
        return None
    m3 = _central_moment(returns, mean, 3)
    m4 = _central_moment(returns, mean, 4)
    s = m3 / (m2 ** 1.5)          # biased skewness
    k = m4 / (m2 * m2) - 3.0      # biased excess kurtosis
    jb = (n / 6.0) * (s * s + (k * k) / 4.0)
    return jb, _chi2_2df_sf(jb)


def shape_stats(returns: Sequence[float]) -> Optional[ShapeStats]:
    """
    Compute the full bundle of shape diagnostics for a return series.

    Uses bias-corrected skewness/excess-kurtosis (for reporting), and the
    canonical biased-moment Jarque-Bera statistic (for the normality test).

    Returns None if there are fewer than 4 points or the series has zero variance
    (all moments undefined). Otherwise a fully-populated ShapeStats.
    """
    n = len(returns)
    if n < 4:
        return None
    mean = statistics.fmean(returns)
    m2 = _central_moment(returns, mean, 2)
    if m2 == 0:
        return None

    sk = skewness(returns)
    ek = excess_kurtosis(returns)
    jb_result = jarque_bera(returns)
    if sk is None or ek is None or jb_result is None:
        return None
    jb, p = jb_result

    return ShapeStats(
        n=n,
        mean=mean,
        std=statistics.stdev(returns),
        skewness=sk,
        excess_kurtosis=ek,
        jarque_bera=jb,
        jb_p_value=p,
    )
