"""Tests for apex.analytics.underwater_curve (drawdown-over-time series)."""

from __future__ import annotations

import math

import pytest

from apex.analytics.underwater_curve import (
    DrawdownEpisode,
    drawdown_episodes,
    max_time_underwater,
    running_peak,
    time_underwater,
    underwater_curve,
    underwater_curve_absolute,
)

# Reference curve used across several tests:
#   index: 0    1    2    3    4    5    6
#   value: 100  120  90   60   80   120  150
# running peak: 100  120  120  120  120  120  150
CURVE = [100.0, 120.0, 90.0, 60.0, 80.0, 120.0, 150.0]


def approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=tol)


# --------------------------------------------------------------------------
# running_peak
# --------------------------------------------------------------------------


def test_running_peak_basic():
    assert running_peak(CURVE) == [100.0, 120.0, 120.0, 120.0, 120.0, 120.0, 150.0]


def test_running_peak_empty():
    assert running_peak([]) == []


def test_running_peak_monotonic():
    assert running_peak([1.0, 2.0, 3.0]) == [1.0, 2.0, 3.0]


# --------------------------------------------------------------------------
# underwater_curve (fractional)
# --------------------------------------------------------------------------


def test_underwater_curve_hand_computed():
    uw = underwater_curve(CURVE)
    # peaks: 100,120,120,120,120,120,150
    # dd:    0, 0, (120-90)/120=.25, (120-60)/120=.5, (120-80)/120=1/3, 0, 0
    expected = [0.0, 0.0, 0.25, 0.5, (120.0 - 80.0) / 120.0, 0.0, 0.0]
    assert len(uw) == len(expected)
    for got, want in zip(uw, expected):
        assert approx(got, want)


def test_underwater_curve_all_nonnegative():
    assert all(d >= 0.0 for d in underwater_curve(CURVE))


def test_underwater_curve_empty():
    assert underwater_curve([]) == []


def test_underwater_curve_monotonic_rising_all_zero():
    assert underwater_curve([10.0, 20.0, 30.0, 40.0]) == [0.0, 0.0, 0.0, 0.0]


def test_underwater_curve_single_point():
    assert underwater_curve([100.0]) == [0.0]


def test_underwater_curve_zero_peak_fail_closed():
    # First point is zero -> running peak 0 until a positive value appears.
    uw = underwater_curve([0.0, 0.0, 50.0, 25.0])
    # peaks: 0,0,50,50 ; dd: 0,0,0,(50-25)/50=.5
    assert uw[0] == 0.0
    assert uw[1] == 0.0
    assert approx(uw[2], 0.0)
    assert approx(uw[3], 0.5)


def test_underwater_curve_negative_peak_fail_closed():
    uw = underwater_curve([-10.0, -20.0])
    assert uw == [0.0, 0.0]


# --------------------------------------------------------------------------
# underwater_curve_absolute
# --------------------------------------------------------------------------


def test_underwater_curve_absolute_hand_computed():
    uw = underwater_curve_absolute(CURVE)
    # peak - value: 0,0,30,60,40,0,0
    assert uw == [0.0, 0.0, 30.0, 60.0, 40.0, 0.0, 0.0]


def test_underwater_curve_absolute_empty():
    assert underwater_curve_absolute([]) == []


def test_underwater_curve_absolute_works_with_negative_peak():
    # peaks: -10,-10 ; amounts: 0, (-10 - -20)=10
    assert underwater_curve_absolute([-10.0, -20.0]) == [0.0, 10.0]


# --------------------------------------------------------------------------
# time_underwater / max_time_underwater
# --------------------------------------------------------------------------


def test_time_underwater_hand_computed():
    # CURVE peaks: 100,120,120,120,120,120,150. idx5 (120) equals the running
    # peak so it counts as recovered -> streak resets to 0 there and at idx6.
    # idx:    0  1  2  3  4  5  6
    # streak: 0  0  1  2  3  0  0
    assert time_underwater(CURVE) == [0, 0, 1, 2, 3, 0, 0]


def test_time_underwater_empty():
    assert time_underwater([]) == []


def test_time_underwater_flat_counts_as_at_peak():
    # Equal-to-peak counts as a (re)peak: streak stays 0.
    assert time_underwater([5.0, 5.0, 5.0]) == [0, 0, 0]


def test_max_time_underwater_basic():
    # Longest run below a high-water mark is idx2..idx4 (3 points) before idx5
    # touches the peak again.
    assert max_time_underwater(CURVE) == 3


def test_max_time_underwater_never_underwater():
    assert max_time_underwater([1.0, 2.0, 3.0]) == 0


def test_max_time_underwater_still_underwater_at_end():
    # Drops and never recovers: 10, 9, 8, 7 -> streaks 0,1,2,3 -> max 3.
    assert max_time_underwater([10.0, 9.0, 8.0, 7.0]) == 3


def test_max_time_underwater_empty():
    assert max_time_underwater([]) == 0


# --------------------------------------------------------------------------
# drawdown_episodes
# --------------------------------------------------------------------------


def test_drawdown_episodes_single_recovered():
    eps = drawdown_episodes(CURVE)
    # One episode: peak at idx1 (120), trough at idx3 (60), recover at idx5
    # (120>=120 — equality counts as recovery).
    assert len(eps) == 1
    ep = eps[0]
    assert ep.start_index == 1
    assert ep.trough_index == 3
    assert ep.end_index == 5
    assert ep.recovered is True
    assert approx(ep.max_drawdown, 0.5)
    assert ep.length == 4


def test_drawdown_episodes_none_for_rising():
    assert drawdown_episodes([1.0, 2.0, 3.0, 4.0]) == []


def test_drawdown_episodes_none_for_flat():
    assert drawdown_episodes([5.0, 5.0, 5.0]) == []


def test_drawdown_episodes_empty_and_single():
    assert drawdown_episodes([]) == []
    assert drawdown_episodes([100.0]) == []


def test_drawdown_episodes_unrecovered_at_end():
    # 100 -> 80 -> 60, never recovers.
    eps = drawdown_episodes([100.0, 80.0, 60.0])
    assert len(eps) == 1
    ep = eps[0]
    assert ep.start_index == 0
    assert ep.trough_index == 2
    assert ep.end_index == 2  # set to trough when unrecovered
    assert ep.recovered is False
    assert approx(ep.max_drawdown, 0.4)


def test_drawdown_episodes_two_distinct():
    # 100,90,100,80,100 -> two episodes, both recovered.
    # ep1: peak idx0(100), trough idx1(90), recover idx2(100) -> dd .1
    # ep2: peak idx2(100), trough idx3(80), recover idx4(100) -> dd .2
    curve = [100.0, 90.0, 100.0, 80.0, 100.0]
    eps = drawdown_episodes(curve)
    assert len(eps) == 2
    assert eps[0].start_index == 0
    assert eps[0].trough_index == 1
    assert eps[0].end_index == 2
    assert eps[0].recovered is True
    assert approx(eps[0].max_drawdown, 0.1)
    assert eps[1].start_index == 2
    assert eps[1].trough_index == 3
    assert eps[1].end_index == 4
    assert eps[1].recovered is True
    assert approx(eps[1].max_drawdown, 0.2)


def test_drawdown_episodes_max_matches_metric():
    # The deepest episode max_drawdown should equal the overall max drawdown.
    eps = drawdown_episodes(CURVE)
    overall = max(underwater_curve(CURVE))
    assert approx(max(e.max_drawdown for e in eps), overall)


def test_episode_is_frozen():
    ep = DrawdownEpisode(0, 1, 2, 0.1, True)
    with pytest.raises(Exception):
        ep.start_index = 5  # type: ignore[misc]


def test_drawdown_episodes_new_high_then_drop():
    # Rises to a new high, then a fresh drawdown opens from that high.
    # 100, 150, 120, 150 -> peak idx1(150), trough idx2(120), recover idx3(150).
    curve = [100.0, 150.0, 120.0, 150.0]
    eps = drawdown_episodes(curve)
    assert len(eps) == 1
    ep = eps[0]
    assert ep.start_index == 1
    assert ep.trough_index == 2
    assert ep.end_index == 3
    assert approx(ep.max_drawdown, (150.0 - 120.0) / 150.0)
