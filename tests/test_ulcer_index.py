"""Tests for apex.validation.ulcer_index — hand-computed values + edge cases."""

from __future__ import annotations

import math

from apex.validation import metrics
from apex.validation.ulcer_index import (
    drawdown_series,
    ulcer_index,
    ulcer_performance_index,
)

TOL = 1e-9


def test_drawdown_series_basic():
    # peaks all 100; drawdowns 0, 0.2, 0
    assert drawdown_series([100.0, 80.0, 100.0]) == [0.0, 0.2, 0.0]


def test_drawdown_series_uses_running_peak():
    # 100 -> 120 (new peak) -> 90: dd = (120-90)/120 = 0.25
    dds = drawdown_series([100.0, 120.0, 90.0])
    assert dds[0] == 0.0
    assert dds[1] == 0.0
    assert math.isclose(dds[2], 0.25, abs_tol=TOL)


def test_drawdown_series_empty():
    assert drawdown_series([]) == []


def test_drawdown_series_monotonic_up_has_no_drawdown():
    assert drawdown_series([1.0, 2.0, 3.0, 4.0]) == [0.0, 0.0, 0.0, 0.0]


def test_ulcer_index_hand_computed():
    # drawdowns [0, 0.2, 0]; mean sq = 0.04/3; sqrt = 0.115470053...
    expected = math.sqrt(0.04 / 3.0)
    assert math.isclose(ulcer_index([100.0, 80.0, 100.0]), expected, abs_tol=TOL)


def test_ulcer_index_flat_curve_is_zero():
    assert ulcer_index([50.0, 50.0, 50.0]) == 0.0


def test_ulcer_index_rising_curve_is_zero():
    assert ulcer_index([10.0, 11.0, 12.0]) == 0.0


def test_ulcer_index_empty_and_single():
    assert ulcer_index([]) == 0.0
    assert ulcer_index([100.0]) == 0.0


def test_ulcer_index_never_recovers():
    # peak 100, then 50 forever: drawdowns [0, 0.5, 0.5]
    # mean sq = (0 + 0.25 + 0.25)/3 = 0.5/3; sqrt
    expected = math.sqrt(0.5 / 3.0)
    assert math.isclose(ulcer_index([100.0, 50.0, 50.0]), expected, abs_tol=TOL)


def test_ulcer_index_longer_underwater_scores_worse():
    # Same single worst trough (0.2) but one stays underwater longer.
    quick = ulcer_index([100.0, 80.0, 100.0, 100.0])
    slow = ulcer_index([100.0, 80.0, 80.0, 100.0])
    assert slow > quick


def test_upi_zero_when_no_drawdown():
    # No drawdown -> UI 0 -> fail closed to 0.0 (not inf).
    assert ulcer_performance_index([10.0, 11.0, 12.0]) == 0.0


def test_upi_too_short():
    assert ulcer_performance_index([100.0]) == 0.0
    assert ulcer_performance_index([]) == 0.0


def test_upi_matches_definition():
    curve = [100.0, 80.0, 120.0, 110.0]
    ui = ulcer_index(curve)
    ann = metrics.annualized_return(curve, periods_per_year=252)
    expected = (ann - 0.0) / ui
    assert math.isclose(ulcer_performance_index(curve), expected, abs_tol=TOL)


def test_upi_risk_free_lowers_score():
    curve = [100.0, 80.0, 120.0, 110.0]
    base = ulcer_performance_index(curve, risk_free_rate=0.0)
    with_rf = ulcer_performance_index(curve, risk_free_rate=0.05)
    assert with_rf < base


def test_upi_negative_when_losing():
    # Ends below start -> negative annualized return -> negative UPI.
    curve = [100.0, 90.0, 80.0, 70.0]
    assert ulcer_performance_index(curve) < 0.0


def test_deterministic():
    curve = [100.0, 95.0, 110.0, 90.0, 105.0]
    assert ulcer_index(curve) == ulcer_index(curve)
    assert ulcer_performance_index(curve) == ulcer_performance_index(curve)
