"""Tests for apex.validation.hit_rate_stats — hand-computed values + edges."""
from __future__ import annotations

import math

from apex.validation.hit_rate_stats import (
    HitRateStats,
    compute_hit_rate_stats,
    expectancy,
    loss_rate,
    max_loss_streak,
    max_win_streak,
    payoff_ratio,
    win_rate,
)

# A simple, hand-checkable trade sequence:
#   3 wins (0.10, 0.20, 0.30), 2 losses (-0.05, -0.15), 1 scratch (0.0)
SAMPLE = [0.10, -0.05, 0.20, 0.0, 0.30, -0.15]


def test_win_rate_hand_computed():
    # 3 wins out of 6 trades
    assert win_rate(SAMPLE) == 0.5


def test_loss_rate_hand_computed():
    # 2 losses out of 6 trades
    assert loss_rate(SAMPLE) == 2 / 6


def test_win_and_loss_rate_need_not_sum_to_one_with_scratch():
    assert win_rate(SAMPLE) + loss_rate(SAMPLE) < 1.0


def test_payoff_ratio_hand_computed():
    # avg_win = (0.10 + 0.20 + 0.30) / 3 = 0.20
    # avg_loss = |(-0.05 + -0.15) / 2| = 0.10
    # payoff = 0.20 / 0.10 = 2.0
    assert math.isclose(payoff_ratio(SAMPLE), 2.0)


def test_expectancy_hand_computed():
    # sum = 0.10 - 0.05 + 0.20 + 0.0 + 0.30 - 0.15 = 0.40 over 6 trades
    assert math.isclose(expectancy(SAMPLE), 0.40 / 6)


def test_expectancy_equals_winrate_avgwin_minus_lossrate_avgloss():
    s = compute_hit_rate_stats(SAMPLE)
    reconstructed = s.win_rate * s.avg_win - s.loss_rate * s.avg_loss
    assert math.isclose(s.expectancy, reconstructed)


def test_max_win_streak_hand_computed():
    # wins at positions: W L W . W L  -> longest consecutive win run is 1
    assert max_win_streak(SAMPLE) == 1
    # explicit longer streak
    assert max_win_streak([0.1, 0.2, 0.3, -0.1, 0.5]) == 3


def test_max_loss_streak_hand_computed():
    assert max_loss_streak(SAMPLE) == 1
    assert max_loss_streak([-0.1, -0.2, 0.3, -0.1, -0.1, -0.1]) == 3


def test_scratch_breaks_both_streaks():
    # win, scratch, win -> no streak longer than 1
    assert max_win_streak([0.1, 0.0, 0.1]) == 1
    # loss, scratch, loss -> no streak longer than 1
    assert max_loss_streak([-0.1, 0.0, -0.1]) == 1


def test_compute_bundle_counts():
    s = compute_hit_rate_stats(SAMPLE)
    assert s.trades == 6
    assert s.wins == 3
    assert s.losses == 2
    assert s.scratches == 1
    assert math.isclose(s.avg_win, 0.20)
    assert math.isclose(s.avg_loss, 0.10)
    assert s.max_win_streak == 1
    assert s.max_loss_streak == 1


def test_empty_input_fails_closed():
    s = compute_hit_rate_stats([])
    assert s == HitRateStats(
        trades=0, wins=0, losses=0, scratches=0,
        win_rate=0.0, loss_rate=0.0, avg_win=0.0, avg_loss=0.0,
        payoff_ratio=0.0, expectancy=0.0, max_win_streak=0, max_loss_streak=0,
    )
    assert win_rate([]) == 0.0
    assert loss_rate([]) == 0.0
    assert payoff_ratio([]) == 0.0
    assert expectancy([]) == 0.0
    assert max_win_streak([]) == 0
    assert max_loss_streak([]) == 0


def test_payoff_ratio_no_losers_is_inf():
    assert payoff_ratio([0.1, 0.2, 0.3]) == math.inf


def test_payoff_ratio_no_winners_is_zero():
    assert payoff_ratio([-0.1, -0.2]) == 0.0


def test_all_scratches():
    s = compute_hit_rate_stats([0.0, 0.0, 0.0])
    assert s.trades == 3
    assert s.wins == 0
    assert s.losses == 0
    assert s.scratches == 3
    assert s.win_rate == 0.0
    assert s.loss_rate == 0.0
    assert s.payoff_ratio == 0.0
    assert s.expectancy == 0.0
    assert s.max_win_streak == 0
    assert s.max_loss_streak == 0


def test_single_win():
    s = compute_hit_rate_stats([0.05])
    assert s.win_rate == 1.0
    assert s.loss_rate == 0.0
    assert s.payoff_ratio == math.inf
    assert math.isclose(s.expectancy, 0.05)
    assert s.max_win_streak == 1
    assert s.max_loss_streak == 0


def test_frozen_dataclass_is_immutable():
    s = compute_hit_rate_stats(SAMPLE)
    try:
        s.win_rate = 0.99  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("HitRateStats should be frozen/immutable")


def test_summary_is_readable_string():
    s = compute_hit_rate_stats(SAMPLE)
    out = s.summary()
    assert "win" in out
    assert "payoff" in out
    assert "expectancy" in out


def test_determinism():
    a = compute_hit_rate_stats(SAMPLE)
    b = compute_hit_rate_stats(SAMPLE)
    assert a == b
