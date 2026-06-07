"""
apex.analytics.trade_analyzer
=============================
Per-trade statistics from a list of trade results (the realized P&L of each
closed trade). Turns a raw ``[+120.0, -45.0, +30.0, ...]`` PnL list into the
trade-quality numbers a strategy review actually leans on: trade count, win /
loss counts, average win, average loss, profit factor, expectancy, and the
win rate.

This is analytics/metric code, not money-movement code: it lives in the same
statistical layer as ``apex.validation.metrics`` and follows that layer's
convention of using ``float`` (matching the ``Sequence[float]`` trade-return
inputs that metrics.py's ``profit_factor`` / ``win_rate`` already accept).
Position/cash bookkeeping that must be exact lives in ``apex.risk.portfolio``
and uses Decimal — that is a different layer.

Convention used throughout: a trade with PnL > 0 is a WIN, PnL < 0 is a LOSS,
and exactly 0.0 (a scratch / breakeven trade) is neither — it counts toward the
total trade count but not toward wins or losses. This mirrors the strict ``> 0``
/ ``< 0`` partitioning in ``apex.validation.metrics``.

All functions are pure and deterministic given their inputs. They degrade
gracefully on insufficient data: an empty trade list yields zeros (and ``inf``
for profit factor only when there is profit and no loss, matching metrics.py),
never garbage or a divide-by-zero. Tested in tests/test_trade_analyzer.py
against hand-computed values.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


def trade_count(trade_pnls: Sequence[float]) -> int:
    """Total number of trades (wins, losses, and scratches alike)."""
    return len(trade_pnls)


def win_count(trade_pnls: Sequence[float]) -> int:
    """Number of trades with strictly positive PnL."""
    return sum(1 for p in trade_pnls if p > 0)


def loss_count(trade_pnls: Sequence[float]) -> int:
    """Number of trades with strictly negative PnL."""
    return sum(1 for p in trade_pnls if p < 0)


def scratch_count(trade_pnls: Sequence[float]) -> int:
    """Number of breakeven trades (PnL exactly 0.0)."""
    return sum(1 for p in trade_pnls if p == 0)


def win_rate(trade_pnls: Sequence[float]) -> float:
    """
    Fraction of ALL trades that were winners (0.0-1.0). Scratch trades count in
    the denominator, so this is wins / total, consistent with
    ``apex.validation.metrics.win_rate``. Returns 0.0 with no trades.
    """
    if not trade_pnls:
        return 0.0
    return win_count(trade_pnls) / len(trade_pnls)


def loss_rate(trade_pnls: Sequence[float]) -> float:
    """
    Fraction of ALL trades that were losers (0.0-1.0). Scratch trades count in
    the denominator. Returns 0.0 with no trades.
    """
    if not trade_pnls:
        return 0.0
    return loss_count(trade_pnls) / len(trade_pnls)


def gross_profit(trade_pnls: Sequence[float]) -> float:
    """Sum of all winning trades' PnL (>= 0). 0.0 if there are no winners."""
    return float(sum(p for p in trade_pnls if p > 0))


def gross_loss(trade_pnls: Sequence[float]) -> float:
    """
    Absolute value of the sum of all losing trades' PnL (>= 0).
    0.0 if there are no losers.
    """
    return float(abs(sum(p for p in trade_pnls if p < 0)))


def net_profit(trade_pnls: Sequence[float]) -> float:
    """Total PnL across every trade (gross profit minus gross loss)."""
    return float(sum(trade_pnls))


def average_win(trade_pnls: Sequence[float]) -> float:
    """
    Mean PnL of winning trades (a positive number). Returns 0.0 if there are no
    winners (fail closed — no garbage, no divide-by-zero).
    """
    n = win_count(trade_pnls)
    if n == 0:
        return 0.0
    return gross_profit(trade_pnls) / n


def average_loss(trade_pnls: Sequence[float]) -> float:
    """
    Mean PnL of losing trades reported as a POSITIVE magnitude (the average size
    of a loss). Returns 0.0 if there are no losers.
    """
    n = loss_count(trade_pnls)
    if n == 0:
        return 0.0
    return gross_loss(trade_pnls) / n


def average_trade(trade_pnls: Sequence[float]) -> float:
    """
    Mean PnL across ALL trades (net profit / trade count). This equals the
    expectancy per trade. Returns 0.0 with no trades.
    """
    if not trade_pnls:
        return 0.0
    return net_profit(trade_pnls) / len(trade_pnls)


def profit_factor(trade_pnls: Sequence[float]) -> float:
    """
    Gross profit / gross loss. > 1 means profitable. ``inf`` when there are
    winners but no losers (treat with suspicion — usually too few trades), and
    0.0 when there is no profit at all. Mirrors
    ``apex.validation.metrics.profit_factor`` exactly.
    """
    gp = gross_profit(trade_pnls)
    gl = gross_loss(trade_pnls)
    if gl == 0.0:
        return math.inf if gp > 0.0 else 0.0
    return gp / gl


def expectancy(trade_pnls: Sequence[float]) -> float:
    """
    Expected PnL per trade = mean of all trade PnLs. Identical to
    ``average_trade``; provided under the name traders look for. The
    probability-weighted form (win_rate * avg_win - loss_rate * avg_loss) is
    algebraically the same value when scratch trades are absent, and remains the
    true per-trade average when they are present. Returns 0.0 with no trades.
    """
    return average_trade(trade_pnls)


def payoff_ratio(trade_pnls: Sequence[float]) -> float:
    """
    Average win divided by average loss (the reward-to-risk per trade). ``inf``
    when there are wins but no losing trades, and 0.0 when there are no winners.
    Returns 0.0 with no trades.
    """
    avg_win = average_win(trade_pnls)
    avg_loss = average_loss(trade_pnls)
    if avg_loss == 0.0:
        return math.inf if avg_win > 0.0 else 0.0
    return avg_win / avg_loss


def largest_win(trade_pnls: Sequence[float]) -> float:
    """Best single trade's PnL (>= 0). 0.0 if there are no winners."""
    wins = [p for p in trade_pnls if p > 0]
    return float(max(wins)) if wins else 0.0


def largest_loss(trade_pnls: Sequence[float]) -> float:
    """
    Worst single trade's PnL reported as a POSITIVE magnitude. 0.0 if there are
    no losers.
    """
    losses = [p for p in trade_pnls if p < 0]
    return float(abs(min(losses))) if losses else 0.0


@dataclass(frozen=True)
class TradeStats:
    """Immutable summary of per-trade performance statistics."""

    trade_count: int
    win_count: int
    loss_count: int
    scratch_count: int
    win_rate: float
    loss_rate: float
    gross_profit: float
    gross_loss: float
    net_profit: float
    average_win: float
    average_loss: float
    average_trade: float
    profit_factor: float
    expectancy: float
    payoff_ratio: float
    largest_win: float
    largest_loss: float


def analyze_trades(trade_pnls: Sequence[float]) -> TradeStats:
    """
    Compute the full per-trade statistics summary in a single pass-friendly call.

    ``trade_pnls`` is a sequence of realized per-trade PnL values in account
    currency (e.g. ``[+120.0, -45.0, +30.0]``). Wins are PnL > 0, losses are
    PnL < 0, breakeven trades (== 0.0) are scratches that count toward the total
    but not toward wins or losses.

    With an empty list every field is 0 / 0.0 (profit_factor and payoff_ratio
    included), never garbage. Each field matches the standalone function of the
    same name, so callers can pull just one number or the whole bundle.
    """
    return TradeStats(
        trade_count=trade_count(trade_pnls),
        win_count=win_count(trade_pnls),
        loss_count=loss_count(trade_pnls),
        scratch_count=scratch_count(trade_pnls),
        win_rate=win_rate(trade_pnls),
        loss_rate=loss_rate(trade_pnls),
        gross_profit=gross_profit(trade_pnls),
        gross_loss=gross_loss(trade_pnls),
        net_profit=net_profit(trade_pnls),
        average_win=average_win(trade_pnls),
        average_loss=average_loss(trade_pnls),
        average_trade=average_trade(trade_pnls),
        profit_factor=profit_factor(trade_pnls),
        expectancy=expectancy(trade_pnls),
        payoff_ratio=payoff_ratio(trade_pnls),
        largest_win=largest_win(trade_pnls),
        largest_loss=largest_loss(trade_pnls),
    )
