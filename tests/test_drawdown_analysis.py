"""Tests for apex.validation.drawdown_analysis — hand-computed known values."""
from __future__ import annotations

import math

from apex.validation.drawdown_analysis import (
    analyze_drawdowns,
    average_drawdown,
    average_drawdown_duration,
    average_recovery_time,
    drawdown_periods,
    drawdown_series,
    max_drawdown_duration,
    max_recovery_time,
)

# A single recovered drawdown:
# index:  0    1    2    3    4    5
# value: 100  120  90   80  110  130
# peak rises to 120 at idx1, falls to trough 80 at idx3, recovers at idx5 (130>=120).
SIMPLE = [100.0, 120.0, 90.0, 80.0, 110.0, 130.0]


def test_drawdown_series_known_values():
    series = drawdown_series(SIMPLE)
    # peaks: 100,120,120,120,120,130 -> dd = (peak-value)/peak
    expected = [
        0.0,
        0.0,
        (120 - 90) / 120,
        (120 - 80) / 120,
        (120 - 110) / 120,
        0.0,
    ]
    assert len(series) == len(expected)
    for got, want in zip(series, expected):
        assert math.isclose(got, want, rel_tol=1e-12)


def test_single_recovered_period():
    periods = drawdown_periods(SIMPLE)
    assert len(periods) == 1
    p = periods[0]
    assert p.peak_index == 1
    assert p.peak_value == 120.0
    assert p.trough_index == 3
    assert p.trough_value == 80.0
    assert p.recovery_index == 5
    assert p.recovered is True
    # depth = (120-80)/120
    assert math.isclose(p.depth, 40.0 / 120.0, rel_tol=1e-12)
    # duration = recovery - peak = 5 - 1 = 4
    assert p.duration == 4
    # recovery_duration = recovery - trough = 5 - 3 = 2
    assert p.recovery_duration == 2


def test_summary_functions_single_period():
    assert max_drawdown_duration(SIMPLE) == 4
    assert max_recovery_time(SIMPLE) == 2
    assert math.isclose(average_drawdown(SIMPLE), 40.0 / 120.0, rel_tol=1e-12)
    assert average_drawdown_duration(SIMPLE) == 4.0
    assert average_recovery_time(SIMPLE) == 2.0


# Two distinct drawdowns, second one unrecovered at the end.
# index:  0    1    2    3    4    5    6    7
# value: 100  90  100  150  120  130 200  150
# DD1: peak 100@0, trough 90@1, recover @2 (100>=100): dur=2-0=2, rec=2-1=1, depth=0.1
# new peaks 150@3, then DD2: trough 120@4, recover @? 130<150,200>=150 @6:
#   dur=6-3=3, rec=6-4=2, depth=(150-120)/150=0.2
# new peak 200@6, DD3: trough 150@7, never recovers. dur = 7-6 = 1, depth=(200-150)/200=0.25
TWO = [100.0, 90.0, 100.0, 150.0, 120.0, 130.0, 200.0, 150.0]


def test_multiple_periods_with_trailing_unrecovered():
    periods = drawdown_periods(TWO)
    assert len(periods) == 3

    p1, p2, p3 = periods

    assert p1.peak_index == 0 and p1.trough_index == 1 and p1.recovery_index == 2
    assert p1.duration == 2 and p1.recovery_duration == 1
    assert math.isclose(p1.depth, 0.1, rel_tol=1e-12)
    assert p1.recovered is True

    assert p2.peak_index == 3 and p2.trough_index == 4 and p2.recovery_index == 6
    assert p2.duration == 3 and p2.recovery_duration == 2
    assert math.isclose(p2.depth, 0.2, rel_tol=1e-12)
    assert p2.recovered is True

    assert p3.peak_index == 6 and p3.trough_index == 7
    assert p3.recovery_index is None
    assert p3.recovered is False
    assert p3.recovery_duration is None
    assert p3.duration == 1  # last index 7 - peak 6
    assert math.isclose(p3.depth, 0.25, rel_tol=1e-12)


def test_aggregates_two():
    assert max_drawdown_duration(TWO) == 3
    assert max_recovery_time(TWO) == 2  # only recovered episodes (1, 2)
    assert math.isclose(average_recovery_time(TWO), (1 + 2) / 2, rel_tol=1e-12)
    assert math.isclose(average_drawdown(TWO), (0.1 + 0.2 + 0.25) / 3, rel_tol=1e-12)
    assert math.isclose(average_drawdown_duration(TWO), (2 + 3 + 1) / 3, rel_tol=1e-12)


def test_analyze_rollup():
    a = analyze_drawdowns(TWO)
    assert a.num_drawdowns == 3
    assert math.isclose(a.max_drawdown_depth, 0.25, rel_tol=1e-12)
    assert a.max_drawdown_duration == 3
    assert a.max_recovery_time == 2
    assert a.currently_underwater is True
    assert len(a.periods) == 3
    assert isinstance(a.summary(), str)


def test_monotonic_increasing_has_no_drawdowns():
    eq = [100.0, 101.0, 102.0, 110.0]
    assert drawdown_periods(eq) == []
    assert max_drawdown_duration(eq) == 0
    assert max_recovery_time(eq) is None
    assert average_drawdown(eq) == 0.0
    assert average_drawdown_duration(eq) == 0.0
    assert average_recovery_time(eq) is None
    a = analyze_drawdowns(eq)
    assert a.num_drawdowns == 0
    assert a.currently_underwater is False
    assert a.max_drawdown_depth == 0.0


def test_empty_and_single_point():
    for eq in ([], [100.0]):
        assert drawdown_series(eq) == ([] if not eq else [0.0])
        assert drawdown_periods(eq) == []
        assert max_drawdown_duration(eq) == 0
        assert max_recovery_time(eq) is None
        a = analyze_drawdowns(eq)
        assert a.num_drawdowns == 0
        assert a.currently_underwater is False


def test_flat_curve_no_drawdown():
    eq = [50.0, 50.0, 50.0]
    # value >= peak each step, so never underwater.
    assert drawdown_periods(eq) == []
    assert max_drawdown_duration(eq) == 0


def test_flat_revisit_of_peak_counts_as_recovery():
    # down then back to exactly the peak.
    eq = [100.0, 80.0, 100.0]
    periods = drawdown_periods(eq)
    assert len(periods) == 1
    p = periods[0]
    assert p.recovered is True
    assert p.recovery_index == 2
    assert p.duration == 2
    assert math.isclose(p.depth, 0.2, rel_tol=1e-12)


def test_nonpositive_peak_fails_closed_to_zero_depth():
    # Degenerate curve with a non-positive starting peak: depth/series fail to 0.
    eq = [0.0, -10.0, 0.0]
    series = drawdown_series(eq)
    assert series == [0.0, 0.0, 0.0]
    periods = drawdown_periods(eq)
    # It does dip below peak (0 -> -10) then recovers to 0.
    assert len(periods) == 1
    assert periods[0].depth == 0.0  # peak not positive -> 0.0
    assert periods[0].recovered is True
