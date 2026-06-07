"""
tests.test_trade_analyzer
=========================
Pure, fast tests for apex.analytics.trade_analyzer. Hand-computed known values
plus the insufficient-data and degenerate edge cases the golden rules require.
"""
from __future__ import annotations

import math

from apex.analytics.trade_analyzer import (
    TradeStats,
    analyze_trades,
    average_loss,
    average_trade,
    average_win,
    expectancy,
    gross_loss,
    gross_profit,
    largest_loss,
    largest_win,
    loss_count,
    loss_rate,
    net_profit,
    payoff_ratio,
    profit_factor,
    scratch_count,
    trade_count,
    win_count,
    win_rate,
)

# A worked example used across many assertions.
#   wins:    +120, +30, +50      -> gross_profit = 200, 3 winners
#   losses:  -45, -25            -> gross_loss   = 70,  2 losers
#   scratch: 0.0                 -> 1 scratch
#   total:   6 trades, net = 130
SAMPLE = [120.0, -45.0, 30.0, 0.0, -25.0, 50.0]


def test_counts():
    assert trade_count(SAMPLE) == 6
    assert win_count(SAMPLE) == 3
    assert loss_count(SAMPLE) == 2
    assert scratch_count(SAMPLE) == 1


def test_gross_and_net():
    assert gross_profit(SAMPLE) == 200.0
    assert gross_loss(SAMPLE) == 70.0   # abs of (-45 + -25)
    assert net_profit(SAMPLE) == 130.0  # 200 - 70


def test_averages():
    assert average_win(SAMPLE) == 200.0 / 3
    assert average_loss(SAMPLE) == 35.0          # 70 / 2, positive magnitude
    assert average_trade(SAMPLE) == 130.0 / 6
    assert expectancy(SAMPLE) == average_trade(SAMPLE)


def test_rates():
    assert win_rate(SAMPLE) == 3 / 6
    assert loss_rate(SAMPLE) == 2 / 6
    # win + loss + scratch fractions sum to 1.
    scratch_rate = scratch_count(SAMPLE) / trade_count(SAMPLE)
    assert math.isclose(win_rate(SAMPLE) + loss_rate(SAMPLE) + scratch_rate, 1.0)


def test_profit_and_payoff_factors():
    assert profit_factor(SAMPLE) == 200.0 / 70.0
    # payoff = avg_win / avg_loss = (200/3) / 35
    assert math.isclose(payoff_ratio(SAMPLE), (200.0 / 3) / 35.0)


def test_largest():
    assert largest_win(SAMPLE) == 120.0
    assert largest_loss(SAMPLE) == 45.0  # positive magnitude of -45


def test_analyze_trades_bundle_matches_standalone():
    stats = analyze_trades(SAMPLE)
    assert isinstance(stats, TradeStats)
    assert stats.trade_count == trade_count(SAMPLE)
    assert stats.win_count == win_count(SAMPLE)
    assert stats.loss_count == loss_count(SAMPLE)
    assert stats.scratch_count == scratch_count(SAMPLE)
    assert stats.win_rate == win_rate(SAMPLE)
    assert stats.loss_rate == loss_rate(SAMPLE)
    assert stats.gross_profit == gross_profit(SAMPLE)
    assert stats.gross_loss == gross_loss(SAMPLE)
    assert stats.net_profit == net_profit(SAMPLE)
    assert stats.average_win == average_win(SAMPLE)
    assert stats.average_loss == average_loss(SAMPLE)
    assert stats.average_trade == average_trade(SAMPLE)
    assert stats.profit_factor == profit_factor(SAMPLE)
    assert stats.expectancy == expectancy(SAMPLE)
    assert stats.payoff_ratio == payoff_ratio(SAMPLE)
    assert stats.largest_win == largest_win(SAMPLE)
    assert stats.largest_loss == largest_loss(SAMPLE)


def test_frozen_dataclass_is_immutable():
    stats = analyze_trades(SAMPLE)
    try:
        stats.win_count = 99  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("TradeStats should be frozen/immutable")


# ---------------------------------------------------------------------------
# Edge cases — insufficient / degenerate data must fail closed.
# ---------------------------------------------------------------------------

def test_empty_list():
    empty: list[float] = []
    assert trade_count(empty) == 0
    assert win_count(empty) == 0
    assert loss_count(empty) == 0
    assert scratch_count(empty) == 0
    assert win_rate(empty) == 0.0
    assert loss_rate(empty) == 0.0
    assert gross_profit(empty) == 0.0
    assert gross_loss(empty) == 0.0
    assert net_profit(empty) == 0.0
    assert average_win(empty) == 0.0
    assert average_loss(empty) == 0.0
    assert average_trade(empty) == 0.0
    assert expectancy(empty) == 0.0
    assert largest_win(empty) == 0.0
    assert largest_loss(empty) == 0.0
    # No profit, no loss -> 0.0 (matches metrics.profit_factor).
    assert profit_factor(empty) == 0.0
    assert payoff_ratio(empty) == 0.0


def test_empty_bundle():
    stats = analyze_trades([])
    assert stats == TradeStats(
        trade_count=0,
        win_count=0,
        loss_count=0,
        scratch_count=0,
        win_rate=0.0,
        loss_rate=0.0,
        gross_profit=0.0,
        gross_loss=0.0,
        net_profit=0.0,
        average_win=0.0,
        average_loss=0.0,
        average_trade=0.0,
        profit_factor=0.0,
        expectancy=0.0,
        payoff_ratio=0.0,
        largest_win=0.0,
        largest_loss=0.0,
    )


def test_all_wins_profit_factor_is_inf():
    wins = [10.0, 20.0, 5.0]
    assert profit_factor(wins) == math.inf
    assert payoff_ratio(wins) == math.inf
    assert win_rate(wins) == 1.0
    assert average_loss(wins) == 0.0
    assert largest_loss(wins) == 0.0


def test_all_losses():
    losses = [-10.0, -20.0, -5.0]
    assert profit_factor(losses) == 0.0       # no profit -> 0.0
    assert payoff_ratio(losses) == 0.0
    assert win_rate(losses) == 0.0
    assert loss_rate(losses) == 1.0
    assert gross_loss(losses) == 35.0
    assert average_loss(losses) == 35.0 / 3
    assert largest_loss(losses) == 20.0
    assert net_profit(losses) == -35.0
    assert expectancy(losses) == -35.0 / 3


def test_all_scratches():
    scratches = [0.0, 0.0]
    assert trade_count(scratches) == 2
    assert scratch_count(scratches) == 2
    assert win_count(scratches) == 0
    assert loss_count(scratches) == 0
    assert profit_factor(scratches) == 0.0
    assert payoff_ratio(scratches) == 0.0
    assert win_rate(scratches) == 0.0
    assert loss_rate(scratches) == 0.0
    assert expectancy(scratches) == 0.0


def test_single_trade():
    assert profit_factor([42.0]) == math.inf
    assert expectancy([42.0]) == 42.0
    assert win_rate([42.0]) == 1.0
    assert largest_win([42.0]) == 42.0


def test_determinism():
    # Same input -> same output, every time.
    a = analyze_trades(SAMPLE)
    b = analyze_trades(list(SAMPLE))
    assert a == b
