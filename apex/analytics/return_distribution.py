"""
apex.analytics.return_distribution
==================================
Histogram buckets and distribution statistics (mean, std, skew, kurtosis,
percentiles) for a return series — the shape of the return distribution behind
a headline number.

Sharpe collapses a strategy to one ratio; this module exposes the *shape*: is
the distribution fat-tailed, is it left-skewed (lots of small wins, the
occasional catastrophic loss — the classic blow-up profile), where do the worst
days actually sit? A histogram plus moments and percentiles answers the
questions a single mean-and-std can hide.

This is statistical/reporting code, so it follows the float convention of
``apex.validation.metrics`` rather than Decimal: the inputs are already-computed
fractional returns (0.01 = +1%), not money. Deliberately dependency-light
(stdlib ``math`` + ``statistics``) so it runs anywhere, including the free
GitHub Actions runner.

All functions are pure and deterministic given their inputs. There is no I/O and
no wall-clock access. Insufficient-data windows are handled gracefully: moments
that are undefined for too-few points return ``None`` (not garbage), and an
empty series yields an empty histogram. Tested in
tests/test_return_distribution.py against hand-computed values.
"""
from __future__ import annotations

import math
import statistics
from typing import List, NamedTuple, Optional, Sequence


class HistogramBucket(NamedTuple):
    """
    One bar of a return histogram.

    Attributes:
        lower: inclusive lower edge of the bucket (a fractional return).
        upper: exclusive upper edge of the bucket, except for the final bucket
            whose upper edge is inclusive so the maximum observation lands in it.
        count: number of observations that fell into ``[lower, upper)``.
        frequency: ``count`` divided by the total number of observations
            (0.0-1.0); the frequencies across all buckets sum to 1.0.
    """

    lower: float
    upper: float
    count: int
    frequency: float


class DistributionStats(NamedTuple):
    """
    Summary moments and dispersion of a return series.

    Every field is ``Optional`` so that windows too small to define a given
    statistic report ``None`` rather than a misleading number:
      - ``count`` is always present (0 for an empty series).
      - ``mean``/``minimum``/``maximum`` need >= 1 observation.
      - ``std``/``variance`` (sample) need >= 2 observations.
      - ``skew`` needs >= 3 observations and non-zero dispersion.
      - ``kurtosis`` (excess) needs >= 4 observations and non-zero dispersion.
    """

    count: int
    mean: Optional[float]
    median: Optional[float]
    std: Optional[float]
    variance: Optional[float]
    skew: Optional[float]
    kurtosis: Optional[float]
    minimum: Optional[float]
    maximum: Optional[float]


def mean(returns: Sequence[float]) -> Optional[float]:
    """Arithmetic mean of the return series, or ``None`` if it is empty."""
    if not returns:
        return None
    return statistics.fmean(returns)


def variance(returns: Sequence[float]) -> Optional[float]:
    """
    Sample variance (Bessel-corrected, divides by n-1). Returns ``None`` for
    fewer than two observations, where sample variance is undefined.
    """
    if len(returns) < 2:
        return None
    return statistics.variance(returns)


def std(returns: Sequence[float]) -> Optional[float]:
    """
    Sample standard deviation (divides by n-1). Returns ``None`` for fewer than
    two observations, where it is undefined.
    """
    var = variance(returns)
    if var is None:
        return None
    return math.sqrt(var)


def skewness(returns: Sequence[float]) -> Optional[float]:
    """
    Population skewness (Fisher-Pearson, the moment coefficient): the third
    standardized moment using the *population* standard deviation (divides by n).

    Negative skew = a long left tail (small frequent gains, rare large losses —
    the dangerous blow-up profile). Positive skew = a long right tail.

    Returns ``None`` for fewer than three observations or when dispersion is zero
    (skew is undefined for a constant series), failing closed rather than
    returning garbage.
    """
    n = len(returns)
    if n < 3:
        return None
    mu = statistics.fmean(returns)
    sd = statistics.pstdev(returns)  # population stdev (divides by n)
    if sd == 0:
        return None
    m3 = statistics.fmean([(r - mu) ** 3 for r in returns])
    return m3 / (sd ** 3)


def kurtosis(returns: Sequence[float], *, excess: bool = True) -> Optional[float]:
    """
    Population kurtosis: the fourth standardized moment using the *population*
    standard deviation (divides by n).

    With ``excess=True`` (the default) returns *excess* kurtosis (the raw value
    minus 3), so a normal distribution scores ~0 and a positive number signals
    fat tails — the regime where risk models that assume normality understate
    tail risk. With ``excess=False`` returns the raw (Pearson) kurtosis.

    Returns ``None`` for fewer than four observations or when dispersion is zero,
    failing closed rather than returning garbage.
    """
    n = len(returns)
    if n < 4:
        return None
    mu = statistics.fmean(returns)
    sd = statistics.pstdev(returns)  # population stdev (divides by n)
    if sd == 0:
        return None
    m4 = statistics.fmean([(r - mu) ** 4 for r in returns])
    raw = m4 / (sd ** 4)
    return raw - 3.0 if excess else raw


def percentile(returns: Sequence[float], q: float) -> Optional[float]:
    """
    The ``q``-th percentile of the return series using linear interpolation
    between the two nearest ranks (the common "type 7" / NumPy default method).

    Args:
        q: percentile in [0, 100]. 50 is the median; 5 is the 5th percentile
            (a Value-at-Risk style left-tail point). Values outside [0, 100]
            raise ``ValueError``.

    Returns ``None`` for an empty series. For a single observation every
    percentile is that observation.
    """
    if not 0.0 <= q <= 100.0:
        raise ValueError("q must be in [0, 100]")
    n = len(returns)
    if n == 0:
        return None
    ordered = sorted(returns)
    if n == 1:
        return ordered[0]
    # Linear interpolation between closest ranks (NumPy's default 'linear').
    rank = (q / 100.0) * (n - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def percentiles(
    returns: Sequence[float],
    qs: Sequence[float] = (5.0, 25.0, 50.0, 75.0, 95.0),
) -> List[Optional[float]]:
    """
    Convenience wrapper computing several percentiles at once, preserving the
    order of ``qs``. Each element is ``None`` if the series is empty.
    """
    return [percentile(returns, q) for q in qs]


def distribution_stats(returns: Sequence[float]) -> DistributionStats:
    """
    Compute the full :class:`DistributionStats` summary for a return series.

    Bundles count, mean, median, sample std/variance, population skew/kurtosis,
    and the observed min/max. Each field follows the per-statistic minimum-data
    rules documented on :class:`DistributionStats`; nothing throws on a short or
    empty series.
    """
    n = len(returns)
    if n == 0:
        return DistributionStats(
            count=0,
            mean=None,
            median=None,
            std=None,
            variance=None,
            skew=None,
            kurtosis=None,
            minimum=None,
            maximum=None,
        )
    return DistributionStats(
        count=n,
        mean=statistics.fmean(returns),
        median=statistics.median(returns),
        std=std(returns),
        variance=variance(returns),
        skew=skewness(returns),
        kurtosis=kurtosis(returns),
        minimum=min(returns),
        maximum=max(returns),
    )


def histogram(
    returns: Sequence[float],
    bins: int = 10,
) -> List[HistogramBucket]:
    """
    Bucket a return series into a fixed-width histogram.

    The range ``[min, max]`` is split into ``bins`` equal-width buckets. Each
    observation is placed in the bucket whose half-open interval ``[lower, upper)``
    contains it, with the final bucket made inclusive on the upper edge so the
    maximum observation is counted. Bucket frequencies sum to 1.0.

    Args:
        bins: number of equal-width buckets (must be >= 1).

    Returns:
        A list of ``bins`` :class:`HistogramBucket` in ascending order. Two edge
        cases collapse gracefully:
          - empty series -> ``[]`` (no range to bucket over).
          - a series where every value is identical (zero width) -> a single
            degenerate bucket ``[v, v]`` holding all observations, regardless of
            the requested ``bins`` (an equal-width split is undefined for a
            zero-width range).
    """
    if bins < 1:
        raise ValueError("bins must be >= 1")
    n = len(returns)
    if n == 0:
        return []

    lo = min(returns)
    hi = max(returns)

    # Degenerate: no spread to divide. Report one bucket holding everything
    # rather than fabricating empty equal-width bins over a zero-width range.
    if hi == lo:
        return [HistogramBucket(lower=lo, upper=hi, count=n, frequency=1.0)]

    width = (hi - lo) / bins
    counts = [0] * bins
    for r in returns:
        # Index into the bins; clamp so the maximum lands in the last bucket
        # (its upper edge is inclusive) and float error can't overflow the array.
        idx = int((r - lo) / width)
        if idx >= bins:
            idx = bins - 1
        elif idx < 0:
            idx = 0
        counts[idx] += 1

    out: List[HistogramBucket] = []
    for i in range(bins):
        lower = lo + i * width
        upper = lo + (i + 1) * width
        if i == bins - 1:
            upper = hi  # pin the final edge exactly to max (inclusive)
        out.append(
            HistogramBucket(
                lower=lower,
                upper=upper,
                count=counts[i],
                frequency=counts[i] / n,
            )
        )
    return out
