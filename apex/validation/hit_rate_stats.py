"""
apex.validation.hit_rate_stats
==============================
Hit-rate diagnostics for a strategy's realized trade returns: win rate, loss
rate, payoff ratio, expectancy, and the longest winning / losing streaks.

These are the "is the edge real, and what does it feel like to trade?" numbers
that sit alongside the risk-adjusted metrics in apex.validation.metrics. Win
rate alone is meaningless — a 30%-win strategy with a 4:1 payoff is excellent,
and a 70%-win strategy with a 1:5 payoff is ruin. Expectancy ties them together
into the single per-trade number that actually predicts long-run P&L; the streak
counts tell you the psychological pain you must survive to capture it.

Convention follows the validation layer (apex.validation.metrics): float math,
pure deterministic functions, frozen result dataclass. A "trade return" is a
fraction (0.02 = +2% on that trade). A zero return is treated as a scratch — it
counts as neither a win nor a loss (it breaks streaks of both).

Tested in tests/test_hit_rate_stats.py against hand-computed values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class HitRateStats:
    """Per-trade hit-rate diagnostics for one strategy's realized trades."""

    trades: int  # total trades considered
    wins: int  # trades with return > 0
    losses: int  # trades with return < 0
    scratches: int  # trades with return == 0 (neither win nor loss)
    win_rate: float  # wins / trades (0.0-1.0); 0.0 if no trades
    loss_rate: float  # losses / trades (0.0-1.0); 0.0 if no trades
    avg_win: float  # mean return of winning trades (>= 0); 0.0 if none
    avg_loss: float  # mean return of losing trades, as a POSITIVE
    # magnitude (0.0 if none)
    payoff_ratio: float  # avg_win / avg_loss; inf if there are wins but no
    # losses; 0.0 if no wins
    expectancy: float  # mean return PER TRADE across all trades; the
    # single number that predicts long-run edge
    max_win_streak: int  # longest run of consecutive winning trades
    max_loss_streak: int  # longest run of consecutive losing trades

    def summary(self) -> str:
        return (
            f"Hit rate: {self.win_rate:.1%} win ({self.wins}/{self.trades}), "
            f"payoff {self.payoff_ratio:.2f}, expectancy {self.expectancy:+.4f}/trade, "
            f"streaks +{self.max_win_streak}/-{self.max_loss_streak}"
        )


def win_rate(trade_returns: Sequence[float]) -> float:
    """Fraction of trades that were profitable (return > 0), 0.0-1.0.

    Empty input returns 0.0 (fail closed — no trades is no edge).
    """
    if not trade_returns:
        return 0.0
    wins = sum(1 for r in trade_returns if r > 0)
    return wins / len(trade_returns)


def loss_rate(trade_returns: Sequence[float]) -> float:
    """Fraction of trades that lost money (return < 0), 0.0-1.0.

    Scratches (return == 0) are excluded, so win_rate + loss_rate need not sum
    to 1.0 when scratch trades are present. Empty input returns 0.0.
    """
    if not trade_returns:
        return 0.0
    losses = sum(1 for r in trade_returns if r < 0)
    return losses / len(trade_returns)


def payoff_ratio(trade_returns: Sequence[float]) -> float:
    """Average winning trade / average losing trade (a positive ratio).

    Also called the win/loss ratio. > 1 means your average winner is bigger than
    your average loser. Returns inf if there are winners but no losers (treat
    with suspicion — usually too few trades), and 0.0 if there are no winners.
    """
    wins = [r for r in trade_returns if r > 0]
    losses = [r for r in trade_returns if r < 0]
    if not wins:
        return 0.0
    if not losses:
        return float("inf")
    avg_win = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses))
    if avg_loss == 0:
        return float("inf")
    return avg_win / avg_loss


def expectancy(trade_returns: Sequence[float]) -> float:
    """Expected return PER TRADE: the simple mean across all trades.

    Positive expectancy is the necessary condition for a strategy to make money.
    Equivalent to win_rate * avg_win - loss_rate * avg_loss. Empty input → 0.0.
    """
    if not trade_returns:
        return 0.0
    return sum(trade_returns) / len(trade_returns)


def max_win_streak(trade_returns: Sequence[float]) -> int:
    """Longest run of consecutive winning trades (return > 0).

    A scratch or a loss breaks the streak. Empty input → 0.
    """
    best = 0
    current = 0
    for r in trade_returns:
        if r > 0:
            current += 1
            if current > best:
                best = current
        else:
            current = 0
    return best


def max_loss_streak(trade_returns: Sequence[float]) -> int:
    """Longest run of consecutive losing trades (return < 0).

    A scratch or a win breaks the streak. This is the drawdown-of-confidence
    number — the number of losses in a row you must be willing to sit through.
    Empty input → 0.
    """
    best = 0
    current = 0
    for r in trade_returns:
        if r < 0:
            current += 1
            if current > best:
                best = current
        else:
            current = 0
    return best


def compute_hit_rate_stats(trade_returns: Sequence[float]) -> HitRateStats:
    """Compute the full HitRateStats bundle from a sequence of trade returns.

    Args:
        trade_returns: per-trade returns as fractions (0.02 = +2% on that trade).
            A value of exactly 0.0 is treated as a scratch trade (neither win nor
            loss; it breaks both win and loss streaks).

    Returns a HitRateStats. Empty input yields an all-zero stats object rather
    than raising (fail closed — no trades means no measurable edge).
    """
    trades = len(trade_returns)
    wins_list = [r for r in trade_returns if r > 0]
    losses_list = [r for r in trade_returns if r < 0]
    scratches = trades - len(wins_list) - len(losses_list)

    avg_win = (sum(wins_list) / len(wins_list)) if wins_list else 0.0
    avg_loss = abs(sum(losses_list) / len(losses_list)) if losses_list else 0.0

    return HitRateStats(
        trades=trades,
        wins=len(wins_list),
        losses=len(losses_list),
        scratches=scratches,
        win_rate=win_rate(trade_returns),
        loss_rate=loss_rate(trade_returns),
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=payoff_ratio(trade_returns),
        expectancy=expectancy(trade_returns),
        max_win_streak=max_win_streak(trade_returns),
        max_loss_streak=max_loss_streak(trade_returns),
    )
