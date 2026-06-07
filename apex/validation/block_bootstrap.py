"""
apex.validation.block_bootstrap
===============================
Stationary (circular) block bootstrap for autocorrelated return series.

The plain Monte Carlo bootstrap (see ``monte_carlo.py``) resamples returns one at
a time, with replacement. That is fine for INDEPENDENT trade returns, but it
DESTROYS autocorrelation — and real return series (especially daily/intraday
equity-curve returns, momentum, mean reversion) are autocorrelated. Shuffling
them point-by-point produces a null that is too optimistic about how clean the
edge is.

The block bootstrap fixes this by resampling CONTIGUOUS BLOCKS of returns rather
than single points. A block of length L keeps L consecutive observations
together, so short-range dependence (volatility clustering, serial correlation)
survives the resample. We use the CIRCULAR variant: blocks wrap around the end of
the series back to the start, so every observation has equal probability of being
sampled (no edge-of-series bias). This is the Politis-Romano circular block
bootstrap; with a geometric block length it becomes the stationary bootstrap.

From the resampled series we:
  1. Recompute an arbitrary metric (default: annualized Sharpe via
     ``metrics.sharpe_ratio``) on each resample.
  2. Summarize the resulting distribution (mean, std, percentiles).
  3. Report a p-value for the OBSERVED metric: the fraction of resamples whose
     metric is >= the observed one (i.e. how ordinary the observed value looks
     under block-resampling of its own history).

Uses a SEEDED RNG (``random.Random(seed)``) so the same seed → identical output.
Pure stdlib (random + statistics + the metrics module) — runs on the free CI
runner with no heavy installs. Statistical code here uses ``float`` to match
``apex/validation/metrics.py`` (the layer this lives in is float-based by design).
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from typing import Callable, Sequence

from apex.validation import metrics

# A metric function maps a return series to a single scalar score.
MetricFn = Callable[[Sequence[float]], float]


@dataclass(frozen=True)
class BlockBootstrapResult:
    """Summary of a block-bootstrap distribution for one metric."""

    observed: float  # metric on the real (unresampled) series
    mean: float  # mean metric across resamples
    std: float  # sample std of the metric across resamples
    p_value: float  # P(resample metric >= observed)
    percentiles: dict[float, float]  # e.g. {5: ..., 25: ..., 50: ..., 75: ..., 95: ...}
    n_resamples: int
    block_size: int
    samples: tuple[float, ...] = field(repr=False)  # the raw resampled metrics

    def summary(self) -> str:
        p50 = self.percentiles.get(50.0, float("nan"))
        p05 = self.percentiles.get(5.0, float("nan"))
        p95 = self.percentiles.get(95.0, float("nan"))
        return (
            f"BlockBootstrap: observed={self.observed:.4f}, "
            f"p={self.p_value:.4f}, "
            f"dist mean={self.mean:.4f} std={self.std:.4f}, "
            f"[5/50/95]=[{p05:.4f}/{p50:.4f}/{p95:.4f}] "
            f"({self.n_resamples} resamples, block_size={self.block_size})"
        )


def _percentile_sorted(sorted_values: list[float], pct: float) -> float:
    """
    Linear-interpolated percentile of an ALREADY-SORTED list.

    ``pct`` is in [0, 100]. Mirrors the common "linear" / numpy-default method so
    results are interpretable, but stays pure-stdlib for the free runner.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return sorted_values[low] * (1.0 - frac) + sorted_values[high] * frac


def circular_block_resample(
    series: Sequence[float],
    block_size: int,
    rng: random.Random,
) -> list[float]:
    """
    Draw ONE circular-block-bootstrap resample of ``series``.

    Picks random block START indices uniformly in [0, n) and copies
    ``block_size`` consecutive observations starting there, wrapping around the
    end of the series (circular), until the resample has the same length as the
    input. With ``block_size == 1`` this reduces to the ordinary i.i.d. bootstrap
    (each draw is a single uniformly-chosen observation).

    Returns a list of the same length as ``series``. Empty input → empty list.
    """
    n = len(series)
    if n == 0:
        return []
    bsize = max(1, block_size)

    out: list[float] = []
    while len(out) < n:
        start = rng.randrange(n)
        for offset in range(bsize):
            if len(out) >= n:
                break
            out.append(series[(start + offset) % n])
    return out


def block_bootstrap(
    series: Sequence[float],
    block_size: int = 5,
    n_resamples: int = 2000,
    seed: int = 42,
    metric: MetricFn = metrics.sharpe_ratio,
    percentiles: Sequence[float] = (5.0, 25.0, 50.0, 75.0, 95.0),
) -> BlockBootstrapResult:
    """
    Build a block-bootstrap distribution of ``metric`` over ``series``.

    Args:
        series: the return series to resample (per-period returns as fractions).
        block_size: length of each contiguous block. 1 == ordinary i.i.d.
            bootstrap; larger preserves more autocorrelation. Capped at len(series).
        n_resamples: number of resampled series to generate (>= 1000 recommended).
            The output ``samples`` always has exactly this length (for n>0 input).
        seed: RNG seed for reproducibility. Same seed → identical output.
        metric: function mapping a return series to a scalar (default:
            annualized Sharpe). Any pure metrics-module function works.
        percentiles: which percentiles (0-100) to report from the distribution.

    Returns:
        A BlockBootstrapResult with the observed metric, distribution summary,
        requested percentiles, and a p-value = fraction of resamples whose metric
        is >= the observed metric.

    Degrades gracefully: an empty or single-point series cannot be meaningfully
    resampled, so we return a zeroed result with ``n_resamples == 0`` rather than
    producing garbage.
    """
    rng = random.Random(seed)
    n = len(series)

    # Insufficient data to resample meaningfully — fail closed, no garbage.
    if n < 2:
        observed = metric(series) if n else 0.0
        return BlockBootstrapResult(
            observed=observed,
            mean=observed,
            std=0.0,
            p_value=1.0,
            percentiles={float(p): observed for p in percentiles},
            n_resamples=0,
            block_size=max(1, min(block_size, max(1, n))),
            samples=(),
        )

    effective_block = max(1, min(block_size, n))
    observed = metric(series)

    sample_metrics: list[float] = []
    for _ in range(n_resamples):
        resampled = circular_block_resample(series, effective_block, rng)
        sample_metrics.append(metric(resampled))

    mean = statistics.fmean(sample_metrics)
    std = statistics.pstdev(sample_metrics) if len(sample_metrics) > 1 else 0.0

    ordered = sorted(sample_metrics)
    pct_map = {float(p): _percentile_sorted(ordered, float(p)) for p in percentiles}

    # p-value: how many resamples meet-or-beat the observed metric.
    beats = sum(1 for m in sample_metrics if m >= observed)
    p_value = beats / len(sample_metrics) if sample_metrics else 1.0

    return BlockBootstrapResult(
        observed=observed,
        mean=mean,
        std=std,
        p_value=p_value,
        percentiles=pct_map,
        n_resamples=len(sample_metrics),
        block_size=effective_block,
        samples=tuple(sample_metrics),
    )
