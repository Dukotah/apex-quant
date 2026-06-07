"""Tests for apex.analytics.win_loss_analysis.

Hand-computed known values plus edge cases (empty series, all-wins, all-losses,
flats, streak structure, undefined-without-both-sides ratios). Pure and fast.
"""
from __future__ import annotations

import math

import pytest

from apex.analytics.win_loss_analysis import (
    SideStats,
    StreakStats,
    WinLossSummary,
    expectancy,
    payoff_ratio,
    side_stats,
    split_returns,
    streak_stats,
    win_loss_summary,
)

# --------------------------------------------------------------------------
# split_returns
# --------------------------------------------------------------------------


def test_split_returns_partitions_by_sign():
    wins, losses, flats = split_returns([0.1, -0.2, 0.0, 0.3, -0.05])
    assert wins == [0.1, 0.3]
    assert losses == [-0.2, -0.05]
    assert flats == [0.0]


def test_split_returns_empty():
    assert split_returns([]) == ([], [], [])


def test_split_returns_preserves_every_value_once():
    data = [0.1, -0.2, 0.0, 0.3, -0.05, 0.0]
    wins, losses, flats = split_returns(data)
    assert len(wins) + len(losses) + len(flats) == len(data)


# --------------------------------------------------------------------------
# side_stats
# --------------------------------------------------------------------------


def test_side_stats_known_values():
    # wins = [0.1, 0.2, 0.3]: total=0.6, mean=0.2, best=0.3, worst=0.1
    # sample std of [0.1,0.2,0.3] = 0.1
    s = side_stats([0.1, 0.2, 0.3])
    assert s.count == 3
    assert s.total == pytest.approx(0.6)
    assert s.mean == pytest.approx(0.2)
    assert s.best == pytest.approx(0.3)
    assert s.worst == pytest.approx(0.1)
    assert s.std == pytest.approx(0.1)


def test_side_stats_loss_side_keeps_sign():
    # losses keep their negative sign: worst is the most negative.
    s = side_stats([-0.2, -0.05])
    assert s.mean == pytest.approx(-0.125)
    assert s.best == pytest.approx(-0.05)
    assert s.worst == pytest.approx(-0.2)


def test_side_stats_single_point_std_none():
    s = side_stats([0.1])
    assert s.count == 1
    assert s.total == pytest.approx(0.1)
    assert s.std is None


def test_side_stats_empty():
    s = side_stats([])
    assert s == SideStats(count=0, total=None, mean=None, best=None, worst=None, std=None)


# --------------------------------------------------------------------------
# streak_stats
# --------------------------------------------------------------------------


def test_streak_stats_basic():
    # W W L W W W L L  -> win runs [2,3], loss runs [1,2]
    data = [0.1, 0.1, -0.1, 0.1, 0.1, 0.1, -0.1, -0.1]
    s = streak_stats(data)
    assert s.max_win_streak == 3
    assert s.max_loss_streak == 2
    assert s.current_streak == -2  # ends on a 2-loss run
    assert s.avg_win_streak == pytest.approx(2.5)
    assert s.avg_loss_streak == pytest.approx(1.5)


def test_streak_flats_break_runs():
    # W W flat W -> two win runs [2,1], current is +1
    data = [0.1, 0.1, 0.0, 0.1]
    s = streak_stats(data)
    assert s.max_win_streak == 2
    assert s.max_loss_streak == 0
    assert s.current_streak == 1
    assert s.avg_win_streak == pytest.approx(1.5)
    assert s.avg_loss_streak is None


def test_streak_ends_on_flat_current_zero():
    s = streak_stats([0.1, -0.1, 0.0])
    assert s.current_streak == 0


def test_streak_empty():
    s = streak_stats([])
    assert s == StreakStats(
        max_win_streak=0,
        max_loss_streak=0,
        current_streak=0,
        avg_win_streak=None,
        avg_loss_streak=None,
    )


def test_streak_current_positive_when_ending_on_wins():
    s = streak_stats([-0.1, 0.1, 0.1, 0.1])
    assert s.current_streak == 3
    assert s.max_win_streak == 3


# --------------------------------------------------------------------------
# payoff_ratio / expectancy
# --------------------------------------------------------------------------


def test_payoff_ratio_known():
    # avg win = 0.2, avg loss = -0.1 -> payoff = 0.2 / 0.1 = 2.0
    assert payoff_ratio([0.1, 0.3, -0.1]) == pytest.approx(2.0)


def test_payoff_ratio_none_without_both_sides():
    assert payoff_ratio([0.1, 0.2]) is None
    assert payoff_ratio([-0.1, -0.2]) is None
    assert payoff_ratio([]) is None


def test_expectancy_known():
    # mean of [0.1, -0.2, 0.3] = 0.2/3
    assert expectancy([0.1, -0.2, 0.3]) == pytest.approx(0.2 / 3)


def test_expectancy_empty_none():
    assert expectancy([]) is None


# --------------------------------------------------------------------------
# win_loss_summary
# --------------------------------------------------------------------------


def test_win_loss_summary_known():
    # data: 2 wins (0.1,0.3), 2 losses (-0.2,-0.05), 1 flat (0.0)
    data = [0.1, -0.2, 0.0, 0.3, -0.05]
    s = win_loss_summary(data)
    assert isinstance(s, WinLossSummary)
    assert s.num_periods == 5
    assert s.num_wins == 2
    assert s.num_losses == 2
    assert s.num_flats == 1
    # win_rate counts flats in denominator: 2/5
    assert s.win_rate == pytest.approx(0.4)
    assert s.loss_rate == pytest.approx(0.4)
    assert s.wins.mean == pytest.approx(0.2)
    assert s.losses.mean == pytest.approx(-0.125)
    # payoff = avg_win / |avg_loss| = 0.2 / 0.125 = 1.6
    assert s.payoff_ratio == pytest.approx(1.6)
    # profit factor = (0.1+0.3) / (0.2+0.05) = 0.4 / 0.25 = 1.6
    assert s.profit_factor == pytest.approx(1.6)
    assert s.expectancy == pytest.approx(0.15 / 5 + 0.0)  # sum=0.15, /5


def test_win_loss_summary_expectancy_value():
    data = [0.1, -0.2, 0.0, 0.3, -0.05]
    # sum = 0.1 - 0.2 + 0.0 + 0.3 - 0.05 = 0.15 ; /5 = 0.03
    assert win_loss_summary(data).expectancy == pytest.approx(0.03)


def test_win_loss_summary_all_wins():
    s = win_loss_summary([0.1, 0.2, 0.3])
    assert s.num_wins == 3
    assert s.num_losses == 0
    assert s.win_rate == pytest.approx(1.0)
    assert s.loss_rate == pytest.approx(0.0)
    assert s.payoff_ratio is None  # no losing side
    assert s.profit_factor == math.inf
    assert s.losses.count == 0
    assert s.streaks.max_win_streak == 3


def test_win_loss_summary_all_losses():
    s = win_loss_summary([-0.1, -0.2])
    assert s.num_losses == 2
    assert s.profit_factor == 0.0
    assert s.payoff_ratio is None
    assert s.streaks.max_loss_streak == 2
    assert s.streaks.current_streak == -2


def test_win_loss_summary_empty():
    s = win_loss_summary([])
    assert s.num_periods == 0
    assert s.num_wins == 0
    assert s.num_losses == 0
    assert s.num_flats == 0
    assert s.win_rate == 0.0
    assert s.loss_rate == 0.0
    assert s.payoff_ratio is None
    assert s.expectancy is None
    assert s.wins.count == 0
    assert s.losses.count == 0
    assert s.streaks == StreakStats(0, 0, 0, None, None)


def test_win_loss_summary_deterministic():
    data = [0.05, -0.03, 0.02, 0.0, -0.01]
    assert win_loss_summary(data) == win_loss_summary(data)
