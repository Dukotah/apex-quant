"""Tests for apex.validation.hurst_exponent (rescaled-range Hurst estimation)."""
from __future__ import annotations

import math
import random

import pytest

from apex.validation.hurst_exponent import (
    HurstResult,
    _chunk_sizes,
    _linear_fit,
    _mean_rescaled_range,
    _rescaled_range,
    classify_hurst,
    hurst_exponent,
)


# --------------------------------------------------------------------------- #
# _rescaled_range: hand-computed known values
# --------------------------------------------------------------------------- #
def test_rescaled_range_hand_computed():
    # chunk = [1, 3, 2, 4]; mean = 2.5
    # deviations:        -1.5, 0.5, -0.5, 1.5
    # cumulative:        -1.5, -1.0, -1.5, 0.0
    # range R = max - min = 0.0 - (-1.5) = 1.5
    # sample std S of [1,3,2,4]: variance = ((1.5^2)+(0.5^2)+(0.5^2)+(1.5^2))/3
    #   = (2.25+0.25+0.25+2.25)/3 = 5.0/3 -> S = sqrt(5/3)
    chunk = [1.0, 3.0, 2.0, 4.0]
    expected_S = math.sqrt(5.0 / 3.0)
    expected = 1.5 / expected_S
    assert _rescaled_range(chunk) == pytest.approx(expected)


def test_rescaled_range_constant_is_none():
    # zero dispersion -> S == 0 -> undefined, fail closed
    assert _rescaled_range([5.0, 5.0, 5.0, 5.0]) is None


def test_rescaled_range_too_short_is_none():
    assert _rescaled_range([1.0]) is None
    assert _rescaled_range([]) is None


# --------------------------------------------------------------------------- #
# _mean_rescaled_range: averaging over whole chunks, remainder dropped
# --------------------------------------------------------------------------- #
def test_mean_rescaled_range_averages_chunks():
    # two chunks of size 2: [1,3] and [2,4], plus a remainder that is dropped
    # for [1,3]: mean 2 -> devs -1,1 -> cum -1,0 -> R=1 ; S=stdev([1,3])=sqrt(2)
    #   R/S = 1/sqrt(2)
    # for [2,4]: identical shape -> R/S = 1/sqrt(2)
    series = [1.0, 3.0, 2.0, 4.0, 9.0]  # trailing 9.0 dropped (odd remainder)
    expected = 1.0 / math.sqrt(2.0)
    assert _mean_rescaled_range(series, 2) == pytest.approx(expected)


def test_mean_rescaled_range_size_too_big_is_none():
    assert _mean_rescaled_range([1.0, 2.0, 3.0], 5) is None
    assert _mean_rescaled_range([1.0, 2.0, 3.0], 1) is None  # size < 2


def test_mean_rescaled_range_all_constant_is_none():
    assert _mean_rescaled_range([7.0, 7.0, 7.0, 7.0], 2) is None


# --------------------------------------------------------------------------- #
# _chunk_sizes: geometric ladder
# --------------------------------------------------------------------------- #
def test_chunk_sizes_geometric_ladder():
    # n=64, min_chunk=8, max_divisor=2 -> upper=32; doubling 8,16,32
    assert _chunk_sizes(64, min_chunk=8, max_divisor=2) == [8, 16, 32]


def test_chunk_sizes_appends_upper_when_not_power_of_two():
    # n=100 -> upper=50; doubling 8,16,32 then append 50
    assert _chunk_sizes(100, min_chunk=8, max_divisor=2) == [8, 16, 32, 50]


def test_chunk_sizes_empty_when_series_too_short():
    assert _chunk_sizes(10, min_chunk=8, max_divisor=2) == []


# --------------------------------------------------------------------------- #
# _linear_fit: OLS against a known perfect line
# --------------------------------------------------------------------------- #
def test_linear_fit_perfect_line():
    xs = [0.0, 1.0, 2.0, 3.0]
    ys = [1.0, 3.0, 5.0, 7.0]  # y = 2x + 1
    result = _linear_fit(xs, ys)
    assert result is not None
    slope, intercept, r2 = result
    assert slope == pytest.approx(2.0)
    assert intercept == pytest.approx(1.0)
    assert r2 == pytest.approx(1.0)


def test_linear_fit_zero_x_variance_is_none():
    assert _linear_fit([3.0, 3.0, 3.0], [1.0, 2.0, 3.0]) is None


def test_linear_fit_too_few_points_is_none():
    assert _linear_fit([1.0], [2.0]) is None


# --------------------------------------------------------------------------- #
# classify_hurst
# --------------------------------------------------------------------------- #
def test_classify_hurst_regimes():
    assert classify_hurst(0.5) == "random-walk"
    assert classify_hurst(0.52) == "random-walk"   # within tolerance
    assert classify_hurst(0.8) == "trending"
    assert classify_hurst(0.2) == "mean-reverting"


# --------------------------------------------------------------------------- #
# hurst_exponent: end-to-end behavior
# --------------------------------------------------------------------------- #
def test_hurst_insufficient_data_is_none():
    assert hurst_exponent([1.0, 2.0, 3.0]) is None
    assert hurst_exponent([float(i) for i in range(10)], min_chunk=8) is None


def test_hurst_min_chunk_floor():
    assert hurst_exponent([float(i) for i in range(100)], min_chunk=1) is None


def test_hurst_returns_result_type():
    rng = random.Random(7)
    series = [rng.gauss(0.0, 1.0) for _ in range(512)]
    # build a cumulative (random-walk) series from i.i.d. increments
    walk = []
    acc = 0.0
    for x in series:
        acc += x
        walk.append(acc)
    res = hurst_exponent(walk)
    assert isinstance(res, HurstResult)
    assert res.num_points >= 2
    assert len(res.log_sizes) == res.num_points
    assert len(res.log_rs) == res.num_points
    assert 0.0 <= res.r_squared <= 1.0
    assert res.regime in {"trending", "mean-reverting", "random-walk"}


def test_hurst_white_noise_near_half():
    # R/S applied to i.i.d. increments (white noise) -> H ~ 0.5. This is the
    # canonical random-walk-increments test: no memory in the increments.
    rng = random.Random(123)
    noise = [rng.gauss(0.0, 1.0) for _ in range(4096)]
    res = hurst_exponent(noise)
    assert res is not None
    # R/S is known to be slightly biased on finite samples, but white-noise
    # increments should land in a broad band around 0.5 with a good linear fit.
    assert 0.40 <= res.hurst <= 0.60
    assert res.r_squared > 0.9


def test_hurst_random_walk_levels_near_one():
    # The integrated series (price LEVELS of a random walk) is the integral of
    # white noise -> highly persistent -> H ~ 1.0. Confirms the estimator sees
    # the difference between a walk and its increments.
    rng = random.Random(123)
    walk = []
    acc = 0.0
    for _ in range(4096):
        acc += rng.gauss(0.0, 1.0)
        walk.append(acc)
    res = hurst_exponent(walk)
    assert res is not None
    assert res.hurst > 0.85
    assert res.r_squared > 0.9


def test_hurst_trending_series_above_half():
    # A strong deterministic trend with small noise is highly persistent -> H high.
    rng = random.Random(99)
    trend = [0.1 * i + rng.gauss(0.0, 0.01) for i in range(1024)]
    res = hurst_exponent(trend)
    assert res is not None
    assert res.hurst > 0.6
    assert res.regime == "trending"


def test_hurst_mean_reverting_below_half():
    # Strongly anti-persistent: alternating sign increments -> low H.
    series = []
    acc = 0.0
    for i in range(1024):
        acc += (1.0 if i % 2 == 0 else -1.0)
        series.append(acc)
    res = hurst_exponent(series)
    assert res is not None
    assert res.hurst < 0.5


def test_hurst_constant_series_is_none():
    # No dispersion at any scale -> no usable R/S -> fail closed.
    assert hurst_exponent([3.0] * 256) is None


def test_hurst_summary_string():
    rng = random.Random(1)
    walk = []
    acc = 0.0
    for _ in range(512):
        acc += rng.gauss(0.0, 1.0)
        walk.append(acc)
    res = hurst_exponent(walk)
    assert res is not None
    s = res.summary()
    assert "Hurst H=" in s
    assert res.regime in s


def test_hurst_deterministic():
    rng = random.Random(55)
    walk = []
    acc = 0.0
    for _ in range(600):
        acc += rng.gauss(0.0, 1.0)
        walk.append(acc)
    a = hurst_exponent(walk)
    b = hurst_exponent(walk)
    assert a is not None and b is not None
    assert a.hurst == b.hurst
    assert a.log_rs == b.log_rs
