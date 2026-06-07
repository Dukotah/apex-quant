"""Tests for apex.analytics.return_distribution.

Hand-computed known values plus edge cases (empty, single-point, constant
series, insufficient-data windows). Pure and fast.
"""
from __future__ import annotations

import math

import pytest

from apex.analytics.return_distribution import (
    DistributionStats,
    HistogramBucket,
    distribution_stats,
    histogram,
    kurtosis,
    mean,
    percentile,
    percentiles,
    skewness,
    std,
    variance,
)

# --------------------------------------------------------------------------
# mean / variance / std
# --------------------------------------------------------------------------


def test_mean_known():
    assert mean([0.1, 0.2, 0.3]) == pytest.approx(0.2)


def test_mean_empty_is_none():
    assert mean([]) is None


def test_variance_and_std_known():
    # Sample variance of [1,2,3,4,5]: mean=3, sum sq dev=10, /(n-1=4)=2.5
    data = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert variance(data) == pytest.approx(2.5)
    assert std(data) == pytest.approx(math.sqrt(2.5))


def test_variance_needs_two_points():
    assert variance([0.05]) is None
    assert std([0.05]) is None
    assert variance([]) is None


# --------------------------------------------------------------------------
# skewness
# --------------------------------------------------------------------------


def test_skew_symmetric_is_zero():
    # A symmetric set has zero skew.
    assert skewness([-2.0, -1.0, 0.0, 1.0, 2.0]) == pytest.approx(0.0, abs=1e-12)


def test_skew_known_value():
    # data [0, 0, 0, 1]: mean=0.25, pop var = (0.0625*3 + 0.5625)/4 = 0.1875
    # pop sd = sqrt(0.1875). m3 = mean of cubes of deviations:
    # deviations: -0.25 (x3) -> -0.015625 each; 0.75 -> 0.421875
    # m3 = (3*-0.015625 + 0.421875)/4 = (-0.046875 + 0.421875)/4 = 0.09375
    # skew = m3 / sd^3
    data = [0.0, 0.0, 0.0, 1.0]
    sd = math.sqrt(0.1875)
    expected = 0.09375 / (sd ** 3)
    assert skewness(data) == pytest.approx(expected)
    # right-tailed -> positive skew
    assert skewness(data) > 0


def test_skew_needs_three_points_and_variance():
    assert skewness([0.1, 0.2]) is None
    assert skewness([0.5, 0.5, 0.5]) is None  # zero dispersion


# --------------------------------------------------------------------------
# kurtosis
# --------------------------------------------------------------------------


def test_kurtosis_known_value():
    # data [-1, 0, 0, 1]: mean=0, pop var = (1+0+0+1)/4 = 0.5, sd=sqrt(0.5)
    # m4 = (1 + 0 + 0 + 1)/4 = 0.5 ; raw = 0.5 / 0.5^2 = 0.5/0.25 = 2.0
    # excess = 2.0 - 3.0 = -1.0
    data = [-1.0, 0.0, 0.0, 1.0]
    assert kurtosis(data, excess=False) == pytest.approx(2.0)
    assert kurtosis(data, excess=True) == pytest.approx(-1.0)


def test_kurtosis_needs_four_points_and_variance():
    assert kurtosis([0.1, 0.2, 0.3]) is None
    assert kurtosis([0.5, 0.5, 0.5, 0.5]) is None  # zero dispersion


# --------------------------------------------------------------------------
# percentile / percentiles
# --------------------------------------------------------------------------


def test_percentile_median_odd():
    assert percentile([3.0, 1.0, 2.0], 50.0) == pytest.approx(2.0)


def test_percentile_interpolation():
    # data [0,1,2,3,4], q=25 -> rank = 0.25*4 = 1.0 -> exactly index 1 -> 1.0
    data = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert percentile(data, 25.0) == pytest.approx(1.0)
    # q=10 -> rank=0.4 -> between idx0(0) and idx1(1): 0 + 1*0.4 = 0.4
    assert percentile(data, 10.0) == pytest.approx(0.4)


def test_percentile_extremes():
    data = [5.0, 1.0, 3.0]
    assert percentile(data, 0.0) == pytest.approx(1.0)
    assert percentile(data, 100.0) == pytest.approx(5.0)


def test_percentile_single_and_empty():
    assert percentile([0.07], 5.0) == pytest.approx(0.07)
    assert percentile([0.07], 99.0) == pytest.approx(0.07)
    assert percentile([], 50.0) is None


def test_percentile_out_of_range_raises():
    with pytest.raises(ValueError):
        percentile([1.0, 2.0], -1.0)
    with pytest.raises(ValueError):
        percentile([1.0, 2.0], 101.0)


def test_percentiles_default_order_preserved():
    data = list(range(101))  # 0..100
    res = percentiles([float(x) for x in data])
    # 5,25,50,75,95 of 0..100 -> exact integer ranks
    assert res == pytest.approx([5.0, 25.0, 50.0, 75.0, 95.0])


def test_percentiles_empty():
    assert percentiles([]) == [None, None, None, None, None]


# --------------------------------------------------------------------------
# distribution_stats
# --------------------------------------------------------------------------


def test_distribution_stats_empty():
    s = distribution_stats([])
    assert isinstance(s, DistributionStats)
    assert s.count == 0
    assert s.mean is None
    assert s.std is None
    assert s.skew is None
    assert s.kurtosis is None
    assert s.minimum is None
    assert s.maximum is None


def test_distribution_stats_full():
    data = [-1.0, 0.0, 0.0, 1.0]
    s = distribution_stats(data)
    assert s.count == 4
    assert s.mean == pytest.approx(0.0)
    assert s.median == pytest.approx(0.0)
    assert s.minimum == pytest.approx(-1.0)
    assert s.maximum == pytest.approx(1.0)
    # sample variance: sumsq=2, /(n-1=3) = 0.6667
    assert s.variance == pytest.approx(2.0 / 3.0)
    assert s.std == pytest.approx(math.sqrt(2.0 / 3.0))
    assert s.kurtosis == pytest.approx(-1.0)
    assert s.skew == pytest.approx(0.0, abs=1e-12)


def test_distribution_stats_short_window_partial():
    s = distribution_stats([0.05])
    assert s.count == 1
    assert s.mean == pytest.approx(0.05)
    assert s.median == pytest.approx(0.05)
    assert s.minimum == pytest.approx(0.05)
    assert s.maximum == pytest.approx(0.05)
    assert s.std is None
    assert s.variance is None
    assert s.skew is None
    assert s.kurtosis is None


# --------------------------------------------------------------------------
# histogram
# --------------------------------------------------------------------------


def test_histogram_empty():
    assert histogram([]) == []


def test_histogram_constant_series():
    h = histogram([0.02, 0.02, 0.02], bins=5)
    assert len(h) == 1
    b = h[0]
    assert isinstance(b, HistogramBucket)
    assert b.lower == pytest.approx(0.02)
    assert b.upper == pytest.approx(0.02)
    assert b.count == 3
    assert b.frequency == pytest.approx(1.0)


def test_histogram_known_buckets():
    # data 0..9 (10 points), 5 bins over [0,9], width=1.8
    # edges: [0,1.8),[1.8,3.6),[3.6,5.4),[5.4,7.2),[7.2,9]
    # 0,1 -> b0 (2); 2,3 -> b1 (2); 4,5 -> b2 (2); 6,7 -> b3 (2); 8,9 -> b4 (2)
    data = [float(x) for x in range(10)]
    h = histogram(data, bins=5)
    assert len(h) == 5
    counts = [b.count for b in h]
    assert counts == [2, 2, 2, 2, 2]
    freqs = [b.frequency for b in h]
    assert sum(freqs) == pytest.approx(1.0)
    assert all(f == pytest.approx(0.2) for f in freqs)
    # last bucket upper pinned to max exactly
    assert h[-1].upper == pytest.approx(9.0)
    assert h[0].lower == pytest.approx(0.0)


def test_histogram_max_in_last_bucket():
    # max must be counted (inclusive last edge)
    data = [0.0, 0.5, 1.0]
    h = histogram(data, bins=2)
    assert sum(b.count for b in h) == 3
    assert h[-1].count >= 1  # the 1.0 lands in the last bucket


def test_histogram_frequencies_sum_to_one():
    data = [0.01, -0.02, 0.03, 0.0, -0.01, 0.05, -0.03, 0.02]
    h = histogram(data, bins=4)
    assert sum(b.count for b in h) == len(data)
    assert sum(b.frequency for b in h) == pytest.approx(1.0)


def test_histogram_invalid_bins_raises():
    with pytest.raises(ValueError):
        histogram([0.1, 0.2], bins=0)
