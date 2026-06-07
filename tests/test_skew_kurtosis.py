"""Tests for apex.validation.skew_kurtosis — hand-computed values + edge cases."""
from __future__ import annotations

import math

import pytest

from apex.validation.skew_kurtosis import (
    ShapeStats,
    excess_kurtosis,
    jarque_bera,
    shape_stats,
    skewness,
)

TOL = 1e-9


# --------------------------------------------------------------------------- #
# Insufficient-data / degenerate windows: must fail closed (None), no garbage. #
# --------------------------------------------------------------------------- #

def test_skewness_too_few_points():
    assert skewness([]) is None
    assert skewness([1.0]) is None
    assert skewness([1.0, 2.0]) is None  # needs >= 3


def test_skewness_zero_variance():
    assert skewness([5.0, 5.0, 5.0, 5.0]) is None


def test_excess_kurtosis_too_few_points():
    assert excess_kurtosis([1.0, 2.0, 3.0]) is None  # needs >= 4


def test_excess_kurtosis_zero_variance():
    assert excess_kurtosis([2.0, 2.0, 2.0, 2.0, 2.0]) is None


def test_jarque_bera_too_few_points():
    assert jarque_bera([1.0, 2.0, 3.0]) is None


def test_jarque_bera_zero_variance():
    assert jarque_bera([0.0, 0.0, 0.0, 0.0]) is None


def test_shape_stats_insufficient_or_degenerate():
    assert shape_stats([1.0, 2.0, 3.0]) is None
    assert shape_stats([7.0, 7.0, 7.0, 7.0]) is None


# --------------------------------------------------------------------------- #
# Symmetric data: skewness exactly 0.                                          #
# --------------------------------------------------------------------------- #

def test_skewness_symmetric_is_zero():
    # [1,2,3,4,5] is symmetric about 3 -> third moment 0 -> skew 0.
    assert skewness([1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(0.0, abs=TOL)


def test_skewness_negative_for_left_tail():
    # A long left tail (one big negative) -> negative skew.
    data = [-10.0, 1.0, 1.0, 1.0, 1.0]
    val = skewness(data)
    assert val is not None and val < 0.0


def test_skewness_positive_for_right_tail():
    data = [1.0, 1.0, 1.0, 1.0, 10.0]
    val = skewness(data)
    assert val is not None and val > 0.0


# --------------------------------------------------------------------------- #
# Hand-computed excess kurtosis for [1,2,3,4,5].                               #
#   mean=3, m2=2, m4=6.8 -> biased g2 = 6.8/4 - 3 = -1.3                       #
#   bias-corrected (n=5):                                                      #
#   ((n-1)/((n-2)(n-3))) * ((n+1)*g2 + 6)                                      #
#   = (4/(3*2)) * (6*(-1.3) + 6) = (4/6) * (-1.8) = -1.2                       #
# --------------------------------------------------------------------------- #

def test_excess_kurtosis_known_value():
    assert excess_kurtosis([1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(-1.2, abs=1e-9)


# --------------------------------------------------------------------------- #
# Hand-computed Jarque-Bera for [1,2,3,4,5].                                   #
#   biased skew S = 0 (symmetric)                                             #
#   biased excess kurt K = -1.3                                               #
#   JB = (n/6)*(S^2 + K^2/4) = (5/6)*(0 + 1.69/4) = (5/6)*0.4225 = 0.3520833.. #
#   p = exp(-JB/2) = exp(-0.17604166..) = 0.838573...                          #
# --------------------------------------------------------------------------- #

def test_jarque_bera_known_value():
    result = jarque_bera([1.0, 2.0, 3.0, 4.0, 5.0])
    assert result is not None
    jb, p = result
    expected_jb = (5 / 6.0) * (1.69 / 4.0)
    assert jb == pytest.approx(expected_jb, abs=1e-9)
    assert jb == pytest.approx(0.3520833333, abs=1e-9)
    assert p == pytest.approx(math.exp(-expected_jb / 2.0), abs=1e-12)


def test_jarque_bera_statistic_nonnegative():
    for data in ([1.0, 2.0, 3.0, 4.0, 5.0], [-10.0, 1.0, 1.0, 1.0, 1.0]):
        result = jarque_bera(data)
        assert result is not None
        assert result[0] >= 0.0
        assert 0.0 <= result[1] <= 1.0


def test_jarque_bera_pvalue_small_for_heavy_tails():
    # Strongly non-normal: a fat-tailed, skewed sample should reject normality.
    data = [0.0] * 40 + [50.0, -3.0]
    result = jarque_bera(data)
    assert result is not None
    jb, p = result
    assert jb > 10.0
    assert p < 0.05


# --------------------------------------------------------------------------- #
# shape_stats bundle.                                                          #
# --------------------------------------------------------------------------- #

def test_shape_stats_bundle_consistent():
    data = [1.0, 2.0, 3.0, 4.0, 5.0]
    stats = shape_stats(data)
    assert isinstance(stats, ShapeStats)
    assert stats.n == 5
    assert stats.mean == pytest.approx(3.0, abs=TOL)
    # sample stdev (ddof=1) of 1..5 = sqrt(2.5)
    assert stats.std == pytest.approx(math.sqrt(2.5), abs=TOL)
    assert stats.skewness == pytest.approx(skewness(data), abs=TOL)
    assert stats.excess_kurtosis == pytest.approx(excess_kurtosis(data), abs=TOL)
    jb, p = jarque_bera(data)
    assert stats.jarque_bera == pytest.approx(jb, abs=TOL)
    assert stats.jb_p_value == pytest.approx(p, abs=TOL)


def test_shape_stats_is_normal_flag():
    normal_ish = shape_stats([1.0, 2.0, 3.0, 4.0, 5.0])
    assert normal_ish is not None
    assert normal_ish.is_normal(0.05) is True  # high JB p-value

    skewed = shape_stats([0.0] * 40 + [50.0, -3.0])
    assert skewed is not None
    assert skewed.is_normal(0.05) is False


def test_summary_is_string():
    stats = shape_stats([1.0, 2.0, 3.0, 4.0, 5.0])
    assert stats is not None
    text = stats.summary()
    assert isinstance(text, str) and "Shape" in text


def test_determinism():
    data = [0.01, -0.02, 0.03, -0.01, 0.05, -0.04, 0.02]
    assert shape_stats(data) == shape_stats(data)
