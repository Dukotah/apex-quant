"""
Tests for apex.validation.tail_ratio.

Hand-computed known values plus edge cases. Pure and fast.
"""

from __future__ import annotations

import math

from apex.validation.tail_ratio import percentile, tail_ratio

# --- percentile -----------------------------------------------------------


def test_percentile_empty_returns_none():
    assert percentile([], 95.0) is None


def test_percentile_single_value_is_that_value():
    assert percentile([7.0], 0.0) == 7.0
    assert percentile([7.0], 50.0) == 7.0
    assert percentile([7.0], 100.0) == 7.0


def test_percentile_min_and_max():
    vals = [-2.0, -1.0, 0.0, 1.0, 2.0]
    assert percentile(vals, 0.0) == -2.0
    assert percentile(vals, 100.0) == 2.0


def test_percentile_median():
    # n=5, q=50 -> rank = 0.5*4 = 2 -> exact middle element.
    assert percentile([-2.0, -1.0, 0.0, 1.0, 2.0], 50.0) == 0.0


def test_percentile_linear_interpolation_known_value():
    # n=5, q=95 -> rank = 0.95*4 = 3.8 -> between index3(1) and index4(2),
    # frac 0.8 -> 1 + 0.8*(2-1) = 1.8
    assert math.isclose(percentile([-2.0, -1.0, 0.0, 1.0, 2.0], 95.0), 1.8)
    # q=5 -> rank = 0.05*4 = 0.2 -> -2 + 0.2*(-1 - -2) = -1.8
    assert math.isclose(percentile([-2.0, -1.0, 0.0, 1.0, 2.0], 5.0), -1.8)


def test_percentile_unsorted_input():
    # Order should not matter; it sorts internally.
    assert math.isclose(percentile([2.0, -2.0, 1.0, 0.0, -1.0], 95.0), 1.8)


def test_percentile_clamps_out_of_range_q():
    vals = [-2.0, -1.0, 0.0, 1.0, 2.0]
    assert percentile(vals, -10.0) == percentile(vals, 0.0)
    assert percentile(vals, 200.0) == percentile(vals, 100.0)


# --- tail_ratio -----------------------------------------------------------


def test_tail_ratio_symmetric_is_one():
    # Symmetric distribution: right and left tail magnitudes are equal -> 1.0
    assert math.isclose(tail_ratio([-2.0, -1.0, 0.0, 1.0, 2.0]), 1.0)


def test_tail_ratio_nine_point_symmetric_known_value():
    # n=9: 95th -> 0.95*8=7.6 -> 3 + 0.6*(4-3)=3.6
    #      5th  -> 0.05*8=0.4 -> -4 + 0.4*(-3 - -4)=-3.6
    # ratio = 3.6 / 3.6 = 1.0
    vals = [-4.0, -3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0, 4.0]
    assert math.isclose(tail_ratio(vals), 1.0)


def test_tail_ratio_fat_right_tail_greater_than_one():
    # Big winners, small losers -> right tail dominates -> ratio > 1.
    vals = [-1.0, -1.0, 0.0, 1.0, 10.0]
    r = tail_ratio(vals)
    assert r is not None and r > 1.0


def test_tail_ratio_fat_left_tail_less_than_one():
    # Big losers, small winners -> left tail dominates -> ratio < 1.
    vals = [-10.0, -1.0, 0.0, 1.0, 1.0]
    r = tail_ratio(vals)
    assert r is not None and r < 1.0


def test_tail_ratio_explicit_known_value():
    # n=5 vals sorted: [-10, -1, 0, 1, 1]
    # 95th: 0.95*4=3.8 -> idx3(1) + 0.8*(1-1) = 1.0
    # 5th : 0.05*4=0.2 -> idx0(-10) + 0.2*(-1 - -10) = -10 + 1.8 = -8.2
    # ratio = 1.0 / 8.2
    vals = [-10.0, -1.0, 0.0, 1.0, 1.0]
    assert math.isclose(tail_ratio(vals), 1.0 / 8.2)


def test_tail_ratio_custom_quantiles():
    vals = [-2.0, -1.0, 0.0, 1.0, 2.0]
    # 90th: 0.90*4=3.6 -> 1 + 0.6*(2-1)=1.6
    # 10th: 0.10*4=0.4 -> -2 + 0.4*(-1 - -2)=-1.6
    assert math.isclose(tail_ratio(vals, upper_q=90.0, lower_q=10.0), 1.0)


def test_tail_ratio_insufficient_data_returns_none():
    assert tail_ratio([]) is None
    assert tail_ratio([0.05]) is None


def test_tail_ratio_zero_lower_tail_returns_none():
    # Lowest two values are 0 so the 5th percentile interpolates to exactly 0
    # -> division undefined -> None.
    # n=4, 5th: 0.05*3=0.15 -> idx0(0) + 0.15*(0 - 0) = 0.0
    assert tail_ratio([0.0, 0.0, 1.0, 2.0]) is None


def test_tail_ratio_is_deterministic():
    vals = [0.03, -0.01, 0.05, -0.04, 0.02, -0.02, 0.01, -0.03]
    assert tail_ratio(vals) == tail_ratio(vals)
