"""Tests for apex.strategy.ind_standard_error_bands.

Hand-computed known values plus edge cases. Float math compared with tolerance.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from apex.strategy.ind_standard_error_bands import (
    linear_regression_endpoint,
    standard_error_bands,
)

TOL = 1e-9


def test_warmup_returns_none_until_window_full():
    data = [1.0, 2.0, 3.0]
    endpoint, se = linear_regression_endpoint(data, period=3)
    # First two positions are warmup; only the last has a value.
    assert endpoint[0] is None and endpoint[1] is None
    assert se[0] is None and se[1] is None
    assert endpoint[2] is not None
    assert se[2] is not None


def test_too_few_values_all_none():
    endpoint, se = linear_regression_endpoint([10.0, 11.0], period=3)
    assert endpoint == [None, None]
    assert se == [None, None]


def test_perfect_line_zero_error_endpoint_equals_price():
    # y = 5 + 2x : a perfect straight line -> zero residuals -> SE == 0,
    # and the regression endpoint equals the last actual value of each window.
    data = [5.0, 7.0, 9.0, 11.0, 13.0, 15.0]
    endpoint, se = linear_regression_endpoint(data, period=3)
    for i in range(2, len(data)):
        assert math.isclose(endpoint[i], data[i], rel_tol=0, abs_tol=TOL)
        assert math.isclose(se[i], 0.0, rel_tol=0, abs_tol=TOL)


def test_known_endpoint_and_standard_error():
    # Window = [1, 3, 2] over x = 0,1,2.
    #   mean_x = 1, mean_y = 2
    #   Sxx = (0-1)^2 + (1-1)^2 + (2-1)^2 = 2
    #   Sxy = (-1)(1-2) + (0)(3-2) + (1)(2-2) = 1
    #   b = 1/2 = 0.5 ; a = 2 - 0.5*1 = 1.5
    #   endpoint = a + b*2 = 1.5 + 1.0 = 2.5
    #   fitted = [1.5, 2.0, 2.5]; residuals = [-0.5, 1.0, -0.5]
    #   SSE = 0.25 + 1.0 + 0.25 = 1.5 ; SE = sqrt(1.5/(3-2)) = sqrt(1.5)
    data = [1.0, 3.0, 2.0]
    endpoint, se = linear_regression_endpoint(data, period=3)
    assert math.isclose(endpoint[2], 2.5, rel_tol=0, abs_tol=TOL)
    assert math.isclose(se[2], math.sqrt(1.5), rel_tol=0, abs_tol=TOL)


def test_bands_collapse_on_perfect_line():
    # Perfect line + smooth=1 -> SE 0 -> upper == middle == lower == price.
    data = [10.0, 12.0, 14.0, 16.0, 18.0]
    upper, middle, lower = standard_error_bands(data, period=3, smooth=1, num_errors=2.0)
    for i in range(2, len(data)):
        assert math.isclose(middle[i], data[i], rel_tol=0, abs_tol=TOL)
        assert math.isclose(upper[i], data[i], rel_tol=0, abs_tol=TOL)
        assert math.isclose(lower[i], data[i], rel_tol=0, abs_tol=TOL)


def test_bands_ordering_and_symmetry():
    data = [1.0, 3.0, 2.0, 5.0, 4.0, 7.0, 6.0, 9.0]
    upper, middle, lower = standard_error_bands(data, period=3, smooth=1, num_errors=2.0)
    for i in range(len(data)):
        if middle[i] is None:
            continue
        assert upper[i] >= middle[i] >= lower[i]
        # Bands are symmetric about the middle.
        assert math.isclose(upper[i] - middle[i], middle[i] - lower[i], rel_tol=0, abs_tol=TOL)


def test_smoothing_pushes_warmup_out():
    # period=3, smooth=3 means the first band value needs the regression to be
    # available for 3 consecutive bars -> index 4 (0-based) is first non-None.
    data = [1.0, 3.0, 2.0, 5.0, 4.0, 7.0]
    upper, middle, lower = standard_error_bands(data, period=3, smooth=3)
    assert middle[3] is None  # only 2 regression points available at index 3
    assert middle[4] is not None
    assert upper[4] is not None and lower[4] is not None


def test_num_errors_scales_spread():
    data = [1.0, 3.0, 2.0, 5.0, 4.0, 7.0, 6.0]
    u1, m1, _ = standard_error_bands(data, period=3, smooth=1, num_errors=1.0)
    u2, m2, _ = standard_error_bands(data, period=3, smooth=1, num_errors=2.0)
    for i in range(len(data)):
        if m1[i] is None:
            continue
        assert math.isclose(m1[i], m2[i], rel_tol=0, abs_tol=TOL)
        spread1 = u1[i] - m1[i]
        spread2 = u2[i] - m2[i]
        assert math.isclose(spread2, 2.0 * spread1, rel_tol=0, abs_tol=TOL)


def test_zero_num_errors_collapses_to_regression():
    data = [1.0, 3.0, 2.0, 5.0, 4.0]
    upper, middle, lower = standard_error_bands(data, period=3, smooth=1, num_errors=0.0)
    for i in range(len(data)):
        if middle[i] is None:
            continue
        assert math.isclose(upper[i], middle[i], rel_tol=0, abs_tol=TOL)
        assert math.isclose(lower[i], middle[i], rel_tol=0, abs_tol=TOL)


def test_accepts_decimal_input():
    data = [Decimal("1"), Decimal("3"), Decimal("2")]
    endpoint, se = linear_regression_endpoint(data, period=3)
    assert math.isclose(endpoint[2], 2.5, rel_tol=0, abs_tol=TOL)
    assert math.isclose(se[2], math.sqrt(1.5), rel_tol=0, abs_tol=TOL)


def test_period_below_three_raises():
    with pytest.raises(ValueError):
        linear_regression_endpoint([1.0, 2.0, 3.0], period=2)
    with pytest.raises(ValueError):
        standard_error_bands([1.0, 2.0, 3.0], period=2)


def test_invalid_smooth_and_num_errors_raise():
    with pytest.raises(ValueError):
        standard_error_bands([1.0, 2.0, 3.0], period=3, smooth=0)
    with pytest.raises(ValueError):
        standard_error_bands([1.0, 2.0, 3.0], period=3, num_errors=-1.0)


def test_output_length_matches_input():
    data = [float(x) for x in range(30)]
    upper, middle, lower = standard_error_bands(data, period=10, smooth=3)
    assert len(upper) == len(middle) == len(lower) == len(data)


def test_empty_input():
    endpoint, se = linear_regression_endpoint([], period=3)
    assert endpoint == [] and se == []
    upper, middle, lower = standard_error_bands([], period=3)
    assert upper == [] and middle == [] and lower == []
