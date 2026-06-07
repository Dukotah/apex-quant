"""
tests/test_block_bootstrap.py
=============================
Tests for the circular/stationary block bootstrap.

Covers:
  * determinism — same seed → identical resampled-metric arrays;
  * different seeds generally diverge;
  * block_size=1 reduces to the ordinary i.i.d. bootstrap;
  * the output distribution length == n_resamples;
  * circular wrap-around behavior and block contiguity;
  * graceful degradation on insufficient data;
  * p-value bounds and percentile sanity.

Math is checked against hand-reasoned properties of the resampler, not just
"runs without error".
"""
from __future__ import annotations

import random

import pytest

from apex.validation import metrics
from apex.validation.block_bootstrap import (
    BlockBootstrapResult,
    block_bootstrap,
    circular_block_resample,
)

# A deterministic, mildly autocorrelated-looking series. 40 points so the
# n >= 30 style thresholds elsewhere don't matter and resamples are stable.
SERIES = [
    0.01, -0.02, 0.015, 0.005, -0.01, 0.02, -0.005, 0.012,
    -0.018, 0.008, 0.003, -0.007, 0.011, -0.013, 0.006, 0.009,
    -0.004, 0.014, -0.011, 0.002, 0.017, -0.009, 0.004, -0.016,
    0.013, 0.001, -0.006, 0.019, -0.003, 0.007, -0.012, 0.010,
    0.000, -0.008, 0.016, -0.014, 0.005, 0.011, -0.002, 0.018,
]


def test_determinism_same_seed_identical_samples() -> None:
    r1 = block_bootstrap(SERIES, block_size=5, n_resamples=500, seed=123)
    r2 = block_bootstrap(SERIES, block_size=5, n_resamples=500, seed=123)
    assert r1.samples == r2.samples
    assert r1.observed == r2.observed
    assert r1.mean == r2.mean
    assert r1.std == r2.std
    assert r1.p_value == r2.p_value
    assert r1.percentiles == r2.percentiles


def test_different_seeds_diverge() -> None:
    r1 = block_bootstrap(SERIES, block_size=5, n_resamples=500, seed=1)
    r2 = block_bootstrap(SERIES, block_size=5, n_resamples=500, seed=2)
    # Astronomically unlikely to be identical with different seeds.
    assert r1.samples != r2.samples


def test_output_length_equals_n_resamples() -> None:
    for n in (10, 100, 777):
        res = block_bootstrap(SERIES, block_size=4, n_resamples=n, seed=7)
        assert len(res.samples) == n
        assert res.n_resamples == n


def test_observed_metric_is_real_metric() -> None:
    res = block_bootstrap(SERIES, block_size=3, n_resamples=100, seed=7)
    assert res.observed == metrics.sharpe_ratio(SERIES)


def test_block_size_one_is_iid_bootstrap() -> None:
    """
    With block_size=1 the circular block bootstrap is identical to drawing n
    single observations uniformly with replacement. We reproduce that exact
    draw sequence with the same seeded RNG and assert equality.
    """
    seed = 999
    res = block_bootstrap(SERIES, block_size=1, n_resamples=50, seed=seed)

    # Reproduce the i.i.d. bootstrap by hand with the same RNG contract.
    rng = random.Random(seed)
    n = len(SERIES)
    expected_metrics = []
    for _ in range(50):
        sample = [SERIES[rng.randrange(n)] for _ in range(n)]
        expected_metrics.append(metrics.sharpe_ratio(sample))

    assert list(res.samples) == expected_metrics
    assert res.block_size == 1


def test_circular_resample_length_and_membership() -> None:
    rng = random.Random(0)
    out = circular_block_resample(SERIES, block_size=7, rng=rng)
    assert len(out) == len(SERIES)
    # Every value must come from the original series (it's a resample).
    allowed = set(SERIES)
    assert all(v in allowed for v in out)


def test_circular_wrap_preserves_contiguity() -> None:
    """A block that starts near the end must wrap to the front of the series."""
    series = [0.0, 1.0, 2.0, 3.0, 4.0]

    class FixedStartRNG(random.Random):
        # Force every block to start at index 3.
        def randrange(self, *args, **kwargs):  # type: ignore[override]
            return 3

    out = circular_block_resample(series, block_size=4, rng=FixedStartRNG())
    # Start at 3, length 4, wrapping: 3,4,0,1 then again 3 -> 3 (truncated to n=5).
    assert out == [3.0, 4.0, 0.0, 1.0, 3.0]


def test_block_size_capped_at_series_length() -> None:
    res = block_bootstrap(SERIES, block_size=10_000, n_resamples=20, seed=3)
    assert res.block_size == len(SERIES)
    # A single huge block of length n drawn circularly is just a rotation of the
    # series, so Sharpe (order-invariant for std/mean) equals the observed value.
    for m in res.samples:
        assert m == pytest.approx(res.observed, rel=1e-9, abs=1e-12)


def test_percentiles_present_and_ordered() -> None:
    res = block_bootstrap(SERIES, block_size=5, n_resamples=1000, seed=11)
    assert set(res.percentiles.keys()) == {5.0, 25.0, 50.0, 75.0, 95.0}
    vals = [res.percentiles[p] for p in (5.0, 25.0, 50.0, 75.0, 95.0)]
    assert vals == sorted(vals)


def test_pvalue_in_unit_interval() -> None:
    res = block_bootstrap(SERIES, block_size=5, n_resamples=500, seed=11)
    assert 0.0 <= res.p_value <= 1.0


def test_custom_metric_used() -> None:
    res = block_bootstrap(
        SERIES, block_size=3, n_resamples=100, seed=5, metric=metrics.sortino_ratio
    )
    assert res.observed == metrics.sortino_ratio(SERIES)


def test_insufficient_data_degrades_gracefully() -> None:
    empty = block_bootstrap([], n_resamples=100, seed=1)
    assert isinstance(empty, BlockBootstrapResult)
    assert empty.n_resamples == 0
    assert empty.samples == ()
    assert empty.p_value == 1.0

    one = block_bootstrap([0.05], n_resamples=100, seed=1)
    assert one.n_resamples == 0
    assert one.samples == ()


def test_resample_empty_series_returns_empty() -> None:
    assert circular_block_resample([], block_size=3, rng=random.Random(0)) == []


def test_mean_and_std_match_samples() -> None:
    import statistics as _stats

    res = block_bootstrap(SERIES, block_size=5, n_resamples=300, seed=42)
    assert res.mean == pytest.approx(_stats.fmean(res.samples))
    assert res.std == pytest.approx(_stats.pstdev(res.samples))
