"""
apex.validation.bootstrap_ci
============================
Seeded bootstrap confidence interval for an arbitrary metric of a return series.

A single backtest gives you ONE number for any metric (Sharpe, total return,
profit factor, whatever). But that number is a point estimate from one finite
sample — it has sampling uncertainty. This module quantifies that uncertainty:
resample the returns WITH replacement many times, recompute the metric on each
resample, and read off a confidence interval from the resulting distribution.

The percentile bootstrap is used: sort the resampled statistics and take the
empirical (alpha/2, 1-alpha/2) quantiles. Simple, distribution-free, and honest
about how wobbly a metric is when you only have N observations.

Uses a SEEDED RNG so results are reproducible (determinism is sacred here).
Pure stdlib (random + statistics) — runs on the free CI runner. The metric is
injected as a callable, so this works with anything in apex.validation.metrics
or any user-supplied function of a return series.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from typing import Callable, Sequence

# A metric is any pure function from a return series to a single number.
Metric = Callable[[Sequence[float]], float]


@dataclass(frozen=True)
class BootstrapCI:
    """A bootstrap confidence interval for a metric of a return series."""
    point_estimate: float   # the metric computed on the original (unresampled) data
    lower: float            # lower confidence bound (alpha/2 percentile)
    upper: float            # upper confidence bound (1 - alpha/2 percentile)
    median: float           # median of the bootstrap distribution
    mean: float             # mean of the bootstrap distribution
    std_error: float        # standard error = stdev of the bootstrap distribution
    confidence: float       # confidence level used (e.g. 0.95)
    iterations: int         # number of bootstrap resamples actually run
    n: int                  # size of the input return series

    def contains(self, value: float) -> bool:
        """True if `value` falls inside the (inclusive) confidence interval."""
        return self.lower <= value <= self.upper

    def excludes_zero(self) -> bool:
        """
        True if the whole interval is on one side of zero — i.e. the metric is
        significantly non-zero at this confidence level. Useful for asking
        'is this edge distinguishable from nothing?'.
        """
        return self.lower > 0.0 or self.upper < 0.0

    def summary(self) -> str:
        pct = int(round(self.confidence * 100))
        return (
            f"Bootstrap CI [{pct}%]: estimate={self.point_estimate:.4f}, "
            f"CI=[{self.lower:.4f}, {self.upper:.4f}], "
            f"SE={self.std_error:.4f} ({self.iterations} resamples)"
        )


def _percentile(sorted_values: list[float], q: float) -> float:
    """
    Linear-interpolated percentile of an ascending-sorted list.

    `q` is a fraction in [0, 1]. Mirrors the standard 'linear' / numpy-default
    method so hand-computed test values are easy to verify. Assumes the input is
    already sorted and non-empty.
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def bootstrap_metric_ci(
    returns: Sequence[float],
    metric: Metric,
    iterations: int = 2000,
    confidence: float = 0.95,
    seed: int = 42,
) -> BootstrapCI | None:
    """
    Seeded percentile-bootstrap confidence interval for `metric(returns)`.

    Args:
        returns: the return series (fractions; 0.02 = +2%). Order is irrelevant
            to the bootstrap, which resamples with replacement.
        metric: a pure function mapping a return series to a single float
            (e.g. metrics.sharpe_ratio, metrics.total_return, or any callable).
        iterations: number of bootstrap resamples (>= 1000 recommended).
        confidence: confidence level in (0, 1), e.g. 0.95 for a 95% CI.
        seed: RNG seed for reproducibility.

    Returns:
        A BootstrapCI, or None if there is insufficient data (fewer than 2
        observations) or the parameters are out of range — we fail closed and
        return nothing rather than a garbage interval.
    """
    n = len(returns)
    if n < 2:
        return None
    if iterations < 1:
        return None
    if not (0.0 < confidence < 1.0):
        return None

    data = list(returns)
    point = float(metric(data))

    rng = random.Random(seed)
    stats: list[float] = []
    for _ in range(iterations):
        sample = [data[rng.randrange(n)] for _ in range(n)]
        stats.append(float(metric(sample)))

    stats.sort()
    alpha = 1.0 - confidence
    lower = _percentile(stats, alpha / 2.0)
    upper = _percentile(stats, 1.0 - alpha / 2.0)
    median = _percentile(stats, 0.5)
    mean = statistics.fmean(stats)
    std_error = statistics.pstdev(stats) if len(stats) >= 2 else 0.0

    return BootstrapCI(
        point_estimate=point,
        lower=lower,
        upper=upper,
        median=median,
        mean=mean,
        std_error=std_error,
        confidence=confidence,
        iterations=iterations,
        n=n,
    )
