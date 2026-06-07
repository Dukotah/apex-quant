"""Tests for apex.validation.cagr_mar — hand-computed values + edge cases."""
from __future__ import annotations

import math

import pytest

from apex.validation.cagr_mar import (
    CagrMarResult,
    cagr,
    cagr_mar,
    mar_ratio,
    max_drawdown,
)


# --------------------------------------------------------------------------- #
# max_drawdown                                                                 #
# --------------------------------------------------------------------------- #
def test_max_drawdown_known_value():
    # Peak 120, trough 90 -> (120-90)/120 = 0.25
    curve = [100.0, 120.0, 90.0, 110.0]
    assert max_drawdown(curve) == pytest.approx(0.25)


def test_max_drawdown_monotonic_up_is_zero():
    assert max_drawdown([100.0, 101.0, 102.0]) == 0.0


def test_max_drawdown_empty_and_single():
    assert max_drawdown([]) == 0.0
    assert max_drawdown([100.0]) == 0.0


# --------------------------------------------------------------------------- #
# cagr                                                                         #
# --------------------------------------------------------------------------- #
def test_cagr_exactly_one_year_doubling():
    # 252 periods (one trading year), doubled. CAGR = 2**(252/252) - 1 = 1.0
    curve = _geometric_curve(start=100.0, end=200.0, steps=252)
    assert len(curve) == 253
    assert cagr(curve) == pytest.approx(1.0, rel=1e-9)


def test_cagr_half_year_doubling_annualizes_to_quadruple():
    # 126 periods = half a trading year. Doubling in half a year -> 2**2 - 1 = 3.0
    curve = _geometric_curve(start=100.0, end=200.0, steps=126)
    assert cagr(curve) == pytest.approx(3.0, rel=1e-9)


def test_cagr_custom_periods_per_year_two_years():
    # 2 periods at 1 period/year = 2 years; growth 1.21 -> 1.21**(1/2)-1 = 0.10
    curve = [100.0, 110.0, 121.0]
    assert cagr(curve, periods_per_year=1) == pytest.approx(0.10, rel=1e-9)


def test_cagr_too_few_points_is_zero():
    assert cagr([]) == 0.0
    assert cagr([100.0]) == 0.0


def test_cagr_nonpositive_start_is_zero():
    assert cagr([0.0, 100.0, 200.0]) == 0.0
    assert cagr([-5.0, 100.0]) == 0.0


def test_cagr_total_wipeout_is_minus_one():
    assert cagr([100.0, 50.0, 0.0]) == -1.0
    assert cagr([100.0, -10.0]) == -1.0


# --------------------------------------------------------------------------- #
# mar_ratio                                                                    #
# --------------------------------------------------------------------------- #
def test_mar_ratio_known_value():
    # 1 period/year, 2 periods: 100 -> 150 -> 120.
    # growth = 1.20, cagr = 1.20**(1/2) - 1 = 0.0954451...
    # max dd: peak 150, trough 120 -> 0.20
    # mar = 0.0954451 / 0.20
    curve = [100.0, 150.0, 120.0]
    expected_cagr = 1.20 ** (1 / 2) - 1.0
    expected_mar = expected_cagr / 0.20
    assert mar_ratio(curve, periods_per_year=1) == pytest.approx(expected_mar, rel=1e-9)


def test_mar_ratio_zero_drawdown_returns_zero_not_inf():
    curve = [100.0, 110.0, 120.0]  # monotonic up -> dd == 0
    result = mar_ratio(curve)
    assert result == 0.0
    assert not math.isinf(result)


def test_mar_ratio_can_be_negative_for_losing_strategy():
    # Losing curve: cagr negative, drawdown positive -> mar negative.
    curve = [100.0, 80.0, 90.0]
    assert mar_ratio(curve, periods_per_year=1) < 0.0


def test_mar_ratio_insufficient_data_is_zero():
    assert mar_ratio([]) == 0.0
    assert mar_ratio([100.0]) == 0.0


# --------------------------------------------------------------------------- #
# cagr_mar bundle                                                              #
# --------------------------------------------------------------------------- #
def test_cagr_mar_bundle_matches_individual_functions():
    curve = [100.0, 150.0, 120.0, 180.0]
    result = cagr_mar(curve, periods_per_year=1)
    assert isinstance(result, CagrMarResult)
    assert result.cagr == pytest.approx(cagr(curve, periods_per_year=1))
    assert result.max_drawdown == pytest.approx(max_drawdown(curve))
    assert result.mar_ratio == pytest.approx(mar_ratio(curve, periods_per_year=1))


def test_cagr_mar_frozen():
    result = cagr_mar([100.0, 110.0], periods_per_year=1)
    with pytest.raises(Exception):
        result.cagr = 0.5  # type: ignore[misc]


def test_cagr_mar_summary_is_string():
    result = cagr_mar([100.0, 150.0, 120.0], periods_per_year=1)
    s = result.summary()
    assert isinstance(s, str)
    assert "CAGR" in s and "MAR" in s


def test_cagr_mar_empty_curve_all_zero():
    result = cagr_mar([])
    assert result.cagr == 0.0
    assert result.max_drawdown == 0.0
    assert result.mar_ratio == 0.0


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _geometric_curve(start: float, end: float, steps: int) -> list[float]:
    """Build a smooth geometric equity curve with ``steps`` periods (steps+1 pts)."""
    ratio = (end / start) ** (1.0 / steps)
    return [start * (ratio ** i) for i in range(steps + 1)]
