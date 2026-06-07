"""Tests for apex.analytics.rolling_correlation.

Hand-computed known values plus edge cases. Pure and fast.
"""
from __future__ import annotations

import math

import pytest

from apex.analytics.rolling_correlation import (
    average_rolling_correlation,
    latest_rolling_correlation,
    max_rolling_correlation,
    pearson_correlation,
    rolling_correlation,
)

TOL = 1e-9


# ---------------------------------------------------------------------------
# pearson_correlation
# ---------------------------------------------------------------------------

def test_pearson_perfect_positive():
    # b = 2*a, perfectly correlated -> +1.0
    a = [1.0, 2.0, 3.0, 4.0]
    b = [2.0, 4.0, 6.0, 8.0]
    assert pearson_correlation(a, b) == pytest.approx(1.0, abs=TOL)


def test_pearson_perfect_negative():
    a = [1.0, 2.0, 3.0, 4.0]
    b = [4.0, 3.0, 2.0, 1.0]
    assert pearson_correlation(a, b) == pytest.approx(-1.0, abs=TOL)


def test_pearson_known_value():
    # a = [1,2,3], b = [1,3,2].
    # mean_a=2, mean_b=2.
    # cov = (-1)(-1) + 0*1 + 1*0 = 1
    # var_a = 1+0+1 = 2 ; var_b = 1+1+0 = 2 ; denom = sqrt(4)=2
    # corr = 1/2 = 0.5
    a = [1.0, 2.0, 3.0]
    b = [1.0, 3.0, 2.0]
    assert pearson_correlation(a, b) == pytest.approx(0.5, abs=TOL)


def test_pearson_zero_variance_returns_none():
    a = [5.0, 5.0, 5.0]
    b = [1.0, 2.0, 3.0]
    assert pearson_correlation(a, b) is None


def test_pearson_too_few_points_returns_none():
    assert pearson_correlation([1.0], [2.0]) is None
    assert pearson_correlation([], []) is None


def test_pearson_truncates_to_common_length():
    # Extra element on b is ignored; result equals the 0.5 known value.
    a = [1.0, 2.0, 3.0]
    b = [1.0, 3.0, 2.0, 99.0]
    assert pearson_correlation(a, b) == pytest.approx(0.5, abs=TOL)


def test_pearson_clamped_to_unit_interval():
    val = pearson_correlation([1.0, 2.0, 3.0, 4.0], [3.0, 6.0, 9.0, 12.0])
    assert val is not None
    assert -1.0 <= val <= 1.0


# ---------------------------------------------------------------------------
# rolling_correlation
# ---------------------------------------------------------------------------

def test_rolling_basic_lengths():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [1.0, 2.0, 3.0, 4.0, 5.0]
    res = rolling_correlation(a, b, window=3)
    # n=5, window=3 -> 3 windows
    assert len(res) == 3
    for c in res:
        assert c == pytest.approx(1.0, abs=TOL)


def test_rolling_known_window_values():
    # Window 1: a[0:3]=[1,2,3], b[0:3]=[1,3,2] -> 0.5 (computed above)
    # Window 2: a[1:4]=[2,3,4], b[1:4]=[3,2,4]
    #   mean_a=3, mean_b=3
    #   cov = (-1)(0)+(0)(-1)+(1)(1) = 1
    #   var_a = 1+0+1 = 2 ; var_b = 0+1+1 = 2 ; denom = 2
    #   corr = 0.5
    a = [1.0, 2.0, 3.0, 4.0]
    b = [1.0, 3.0, 2.0, 4.0]
    res = rolling_correlation(a, b, window=3)
    assert len(res) == 2
    assert res[0] == pytest.approx(0.5, abs=TOL)
    assert res[1] == pytest.approx(0.5, abs=TOL)


def test_rolling_window_too_large_returns_empty():
    assert rolling_correlation([1.0, 2.0], [3.0, 4.0], window=5) == []


def test_rolling_window_equals_length():
    a = [1.0, 2.0, 3.0]
    b = [3.0, 2.0, 1.0]
    res = rolling_correlation(a, b, window=3)
    assert len(res) == 1
    assert res[0] == pytest.approx(-1.0, abs=TOL)


def test_rolling_none_for_flat_window():
    # First window of a is flat -> None; later windows defined.
    a = [5.0, 5.0, 5.0, 6.0, 7.0]
    b = [1.0, 2.0, 3.0, 4.0, 5.0]
    res = rolling_correlation(a, b, window=3)
    assert len(res) == 3
    assert res[0] is None  # a flat over [5,5,5]
    assert res[1] is not None
    assert res[2] is not None


def test_rolling_window_below_two_raises():
    with pytest.raises(ValueError):
        rolling_correlation([1.0, 2.0], [3.0, 4.0], window=1)


def test_rolling_truncates_uneven_inputs():
    a = [1.0, 2.0, 3.0, 4.0]
    b = [1.0, 2.0, 3.0]  # shorter -> common length 3
    res = rolling_correlation(a, b, window=3)
    assert len(res) == 1
    assert res[0] == pytest.approx(1.0, abs=TOL)


# ---------------------------------------------------------------------------
# latest_rolling_correlation
# ---------------------------------------------------------------------------

def test_latest_uses_most_recent_window():
    # Last 3 of a=[..,2,3,4], b=[..,3,2,4] -> 0.5 known value.
    a = [9.0, 2.0, 3.0, 4.0]
    b = [9.0, 3.0, 2.0, 4.0]
    assert latest_rolling_correlation(a, b, window=3) == pytest.approx(0.5, abs=TOL)


def test_latest_insufficient_data_returns_none():
    assert latest_rolling_correlation([1.0, 2.0], [3.0, 4.0], window=5) is None


def test_latest_window_below_two_raises():
    with pytest.raises(ValueError):
        latest_rolling_correlation([1.0, 2.0], [3.0, 4.0], window=1)


# ---------------------------------------------------------------------------
# average_rolling_correlation / max_rolling_correlation
# ---------------------------------------------------------------------------

def test_average_skips_none_windows():
    # Two windows both 0.5 -> mean 0.5
    a = [1.0, 2.0, 3.0, 4.0]
    b = [1.0, 3.0, 2.0, 4.0]
    assert average_rolling_correlation(a, b, window=3) == pytest.approx(0.5, abs=TOL)


def test_average_none_when_no_window_defined():
    # window too large -> empty -> None
    assert average_rolling_correlation([1.0], [2.0], window=2) is None


def test_average_ignores_flat_window():
    a = [5.0, 5.0, 5.0, 6.0, 7.0]
    b = [1.0, 2.0, 3.0, 4.0, 5.0]
    avg = average_rolling_correlation(a, b, window=3)
    assert avg is not None
    # The first window is flat (None) and skipped; the average is the mean of
    # the two remaining defined windows and must be a valid in-bounds value.
    assert -1.0 <= avg <= 1.0


def test_max_returns_highest_window():
    # Window 0: [1,2,3] vs [1,3,2] -> 0.5
    # Window 1: [2,3,4] vs [3,2,1] -> -1.0
    a = [1.0, 2.0, 3.0, 4.0]
    b = [1.0, 3.0, 2.0, 1.0]
    assert max_rolling_correlation(a, b, window=3) == pytest.approx(0.5, abs=TOL)


def test_max_none_when_empty():
    assert max_rolling_correlation([1.0, 2.0], [3.0, 4.0], window=5) is None


def test_max_window_below_two_raises():
    with pytest.raises(ValueError):
        max_rolling_correlation([1.0, 2.0], [3.0, 4.0], window=0)


def test_result_values_in_bounds():
    a = [0.1, -0.2, 0.3, 0.05, -0.1, 0.2, -0.05]
    b = [-0.05, 0.1, -0.2, 0.15, 0.0, -0.1, 0.25]
    for c in rolling_correlation(a, b, window=3):
        if c is not None:
            assert -1.0 <= c <= 1.0
            assert not math.isnan(c)
