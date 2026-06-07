"""
apex.validation.hurst_exponent
==============================
Hurst exponent via rescaled-range (R/S) analysis.

The Hurst exponent H characterizes the long-term memory of a time series — it
tells you whether a price/return series trends, mean-reverts, or wanders like a
coin flip. That distinction is the difference between which KIND of strategy can
ever work on an instrument, so it's a useful pre-filter before you waste a
backtest on the wrong approach.

Interpretation:
  * H ~ 0.5  → geometric random walk (no exploitable memory). Efficient market.
  * H  > 0.5 → persistent / trending. Up tends to follow up. Momentum can work.
  * H  < 0.5 → anti-persistent / mean-reverting. Reversals dominate. Fade extremes.

How R/S analysis works (the classic Hurst estimator):
  1. Split the series into chunks of size n (for several values of n).
  2. For each chunk, compute the rescaled range R/S:
        - mean-adjust the chunk,
        - take the cumulative sum (the running deviation profile),
        - R = max(profile) - min(profile)   (the range of the profile),
        - S = sample std of the chunk,
        - R/S = R / S.
  3. Average R/S across chunks of the same size → (R/S)_n.
  4. By Hurst's law, E[(R/S)_n] ~ c * n^H, so a line fit of
        log((R/S)_n)  vs  log(n)
     has slope H. That slope is the Hurst exponent.

Statistical/estimator code, so this layer uses float (matching
apex/validation/metrics.py), not Decimal. All functions are pure and
deterministic given their inputs. Insufficient-data windows return None rather
than garbage (fail closed). Pure stdlib (math + statistics) — runs anywhere,
including the free CI runner, with no heavy installs.

Tested in tests/test_hurst_exponent.py against hand-computed values.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class HurstResult:
    """Outcome of a rescaled-range Hurst estimation."""
    hurst: float                    # the estimated exponent H
    intercept: float                # intercept c of the log-log fit (log(R/S)=H*log(n)+c)
    log_sizes: tuple[float, ...]    # log(n) values that went into the fit
    log_rs: tuple[float, ...]       # log((R/S)_n) values that went into the fit
    r_squared: float                # goodness of the log-log line fit (0..1)
    num_points: int                 # number of (n, R/S) points used in the fit

    @property
    def regime(self) -> str:
        """Human-readable classification (see module docstring thresholds)."""
        return classify_hurst(self.hurst)

    def summary(self) -> str:
        return (
            f"Hurst H={self.hurst:.4f} ({self.regime}), "
            f"R^2={self.r_squared:.3f} over {self.num_points} scales"
        )


def classify_hurst(hurst: float, tolerance: float = 0.05) -> str:
    """
    Classify a Hurst exponent into a regime label.

    Within `tolerance` of 0.5 we call it a random walk (the honest default —
    most markets are close to efficient). Above → trending, below → mean-reverting.
    """
    if hurst > 0.5 + tolerance:
        return "trending"
    if hurst < 0.5 - tolerance:
        return "mean-reverting"
    return "random-walk"


def _rescaled_range(chunk: Sequence[float]) -> float | None:
    """
    Rescaled range R/S of a single chunk.

    Returns None when the chunk is too short (< 2 points) or has zero dispersion
    (constant series → S == 0, undefined ratio). Fail closed; never divide by zero.
    """
    n = len(chunk)
    if n < 2:
        return None
    mean = statistics.fmean(chunk)
    # Cumulative deviation profile (running sum of mean-adjusted values).
    cumulative = 0.0
    profile_max = -math.inf
    profile_min = math.inf
    for x in chunk:
        cumulative += x - mean
        if cumulative > profile_max:
            profile_max = cumulative
        if cumulative < profile_min:
            profile_min = cumulative
    rng = profile_max - profile_min
    sd = statistics.stdev(chunk)  # sample std (n-1)
    if sd == 0:
        return None
    return rng / sd


def _mean_rescaled_range(series: Sequence[float], size: int) -> float | None:
    """
    Average R/S over all non-overlapping chunks of length `size`.

    Only whole chunks are used; a trailing remainder shorter than `size` is
    dropped (it would bias the estimate). Returns None if no usable chunk yields
    a defined R/S.
    """
    if size < 2 or size > len(series):
        return None
    num_chunks = len(series) // size
    if num_chunks < 1:
        return None
    values: list[float] = []
    for i in range(num_chunks):
        chunk = series[i * size : (i + 1) * size]
        rs = _rescaled_range(chunk)
        if rs is not None and rs > 0:
            values.append(rs)
    if not values:
        return None
    return statistics.fmean(values)


def _chunk_sizes(n: int, min_chunk: int = 8, max_divisor: int = 2) -> list[int]:
    """
    Geometric ladder of chunk sizes from `min_chunk` up to n // max_divisor.

    Doubling each step keeps the log(n) axis evenly spaced (good for the fit) and
    cheap to compute. Requires at least `min_chunk` per chunk so each R/S is
    statistically meaningful, and caps size so every size has >= max_divisor
    chunks to average over.
    """
    upper = n // max_divisor
    if upper < min_chunk:
        return []
    sizes: list[int] = []
    size = min_chunk
    while size <= upper:
        sizes.append(size)
        size *= 2
    # Always include the largest allowed size so the long-memory scale is covered.
    if sizes and sizes[-1] != upper:
        sizes.append(upper)
    return sizes


def _linear_fit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float] | None:
    """
    Ordinary least-squares fit y = slope*x + intercept.

    Returns (slope, intercept, r_squared), or None if undefined (too few points
    or zero variance in x). r_squared is clamped to [0, 1].
    """
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x == 0:
        return None
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = cov_xy / var_x
    intercept = mean_y - slope * mean_x
    # Coefficient of determination.
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot == 0:
        r_squared = 1.0
    else:
        ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
        r_squared = 1.0 - ss_res / ss_tot
    r_squared = max(0.0, min(1.0, r_squared))
    return slope, intercept, r_squared


def hurst_exponent(
    series: Sequence[float],
    min_chunk: int = 8,
) -> HurstResult | None:
    """
    Estimate the Hurst exponent of a series via rescaled-range (R/S) analysis.

    Args:
        series: the time series (e.g. a price level series, or a cumulative log
            return series). At least ~2 * min_chunk points are needed to form the
            log-log fit; more is better. Order matters — this measures memory.
        min_chunk: smallest chunk size used in the R/S ladder. Smaller values
            squeeze more scales out of short series but make each R/S noisier.

    Returns:
        A HurstResult, or None if there is too little data / too little structure
        to form a meaningful fit (fail closed — never return a garbage exponent).
    """
    n = len(series)
    if n < 2 * min_chunk or min_chunk < 2:
        return None

    sizes = _chunk_sizes(n, min_chunk=min_chunk)
    log_sizes: list[float] = []
    log_rs: list[float] = []
    for size in sizes:
        rs = _mean_rescaled_range(series, size)
        if rs is None or rs <= 0:
            continue
        log_sizes.append(math.log(size))
        log_rs.append(math.log(rs))

    if len(log_sizes) < 2:
        return None

    fit = _linear_fit(log_sizes, log_rs)
    if fit is None:
        return None
    slope, intercept, r_squared = fit

    return HurstResult(
        hurst=slope,
        intercept=intercept,
        log_sizes=tuple(log_sizes),
        log_rs=tuple(log_rs),
        r_squared=r_squared,
        num_points=len(log_sizes),
    )
