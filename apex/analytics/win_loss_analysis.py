"""
apex.analytics.win_loss_analysis
================================
Distributions and summary of winning vs losing periods from a return series.

A headline win rate ("we win 55% of days") hides the asymmetry that actually
determines whether a strategy survives: how big is the average win versus the
average loss, what is the *worst* loss, how long do losing streaks run, and does
the payoff (avg win / avg loss) more than compensate for the hit rate? This
module splits a period-return series into wins (>0), losses (<0) and flats (==0),
then summarises each side and the streak structure between them.

This is statistical/reporting code, so it follows the float convention of
``apex.validation.metrics`` rather than Decimal: the inputs are already-computed
fractional returns (0.01 = +1%), not money. Deliberately dependency-light
(stdlib ``math`` + ``statistics``) plus the already-existing
``apex.validation.metrics`` so it runs anywhere, including the free GitHub
Actions runner.

All functions are pure and deterministic given their inputs. There is no I/O and
no wall-clock access. Insufficient-data windows are handled gracefully: statistics
that are undefined for too-few points return ``None`` (not garbage), and an empty
series yields an all-empty / all-``None`` summary rather than raising. Tested in
tests/test_win_loss_analysis.py against hand-computed values.
"""
from __future__ import annotations

import math
import statistics
from typing import List, NamedTuple, Optional, Sequence

from apex.validation.metrics import profit_factor, win_rate


class SideStats(NamedTuple):
    """
    Summary of one side of the distribution (the winners or the losers).

    Returns are reported with their natural sign: winners are positive, losers
    are negative. So ``mean`` of the losing side is a negative number and
    ``worst`` (its minimum) is the single most negative observation.

    Every dispersion/extreme field is ``Optional`` so that an empty side reports
    ``None`` rather than a misleading zero:
      - ``count`` is always present (0 if this side has no observations).
      - ``total``/``mean``/``best``/``worst`` need >= 1 observation.
      - ``std`` (sample) needs >= 2 observations.
    """

    count: int
    total: Optional[float]
    mean: Optional[float]
    best: Optional[float]
    worst: Optional[float]
    std: Optional[float]


class StreakStats(NamedTuple):
    """
    Run-length structure of consecutive wins and losses.

    A streak is a maximal run of same-signed periods. Flats (exactly-zero
    returns) break a streak without belonging to either side. Fields are
    ``None`` when there were no streaks of that kind.

    Attributes:
        max_win_streak: longest run of consecutive winning periods.
        max_loss_streak: longest run of consecutive losing periods.
        current_streak: signed length of the run ending at the last period
            (positive = winning run, negative = losing run, 0 = the series ends
            on a flat or is empty).
        avg_win_streak: mean length of winning runs, or ``None`` if none.
        avg_loss_streak: mean length of losing runs, or ``None`` if none.
    """

    max_win_streak: int
    max_loss_streak: int
    current_streak: int
    avg_win_streak: Optional[float]
    avg_loss_streak: Optional[float]


class WinLossSummary(NamedTuple):
    """
    The full winning-vs-losing digest for a return series.

    Attributes:
        num_periods: total number of observations.
        num_wins: count of strictly positive returns.
        num_losses: count of strictly negative returns.
        num_flats: count of exactly-zero returns.
        win_rate: fraction of *all* periods that were wins (0.0-1.0); 0.0 for an
            empty series. (Flats count in the denominator, matching
            ``apex.validation.metrics.win_rate``.)
        loss_rate: fraction of all periods that were losses (0.0-1.0).
        wins: :class:`SideStats` for the winning periods.
        losses: :class:`SideStats` for the losing periods.
        payoff_ratio: average win / |average loss| — how many units of gain a
            typical win delivers per unit of a typical loss. ``None`` if either
            side is empty (undefined without both an average win and loss).
        profit_factor: gross wins / gross losses (from
            ``apex.validation.metrics``); ``inf`` if there are wins but no
            losses, 0.0 if there are no wins.
        expectancy: mean return per period across all observations (the simple
            average); ``None`` for an empty series. The bottom line that
            payoff and hit-rate together produce.
        streaks: :class:`StreakStats` describing the run structure.
    """

    num_periods: int
    num_wins: int
    num_losses: int
    num_flats: int
    win_rate: float
    loss_rate: float
    wins: SideStats
    losses: SideStats
    payoff_ratio: Optional[float]
    profit_factor: float
    expectancy: Optional[float]
    streaks: StreakStats


def split_returns(
    returns: Sequence[float],
) -> tuple[List[float], List[float], List[float]]:
    """
    Partition a return series into ``(wins, losses, flats)``.

    A win is a strictly positive return, a loss strictly negative, and a flat
    exactly zero. The three lists preserve the original order and together
    contain every observation exactly once. An empty input yields three empty
    lists.
    """
    wins: List[float] = []
    losses: List[float] = []
    flats: List[float] = []
    for r in returns:
        if r > 0.0:
            wins.append(r)
        elif r < 0.0:
            losses.append(r)
        else:
            flats.append(r)
    return wins, losses, flats


def side_stats(side: Sequence[float]) -> SideStats:
    """
    Summarise one already-split side (the winners or the losers).

    Computes count, total, mean, best (max), worst (min) and sample std for the
    given observations, keeping their natural sign. ``std`` needs >= 2 points and
    is ``None`` otherwise; an empty side reports count 0 and ``None`` everywhere
    else, failing closed rather than returning garbage.
    """
    n = len(side)
    if n == 0:
        return SideStats(
            count=0,
            total=None,
            mean=None,
            best=None,
            worst=None,
            std=None,
        )
    return SideStats(
        count=n,
        total=math_fsum(side),
        mean=statistics.fmean(side),
        best=max(side),
        worst=min(side),
        std=statistics.stdev(side) if n >= 2 else None,
    )


def math_fsum(values: Sequence[float]) -> float:
    """
    Sum a sequence with extended floating-point precision (``math.fsum``).

    A thin wrapper so the running totals on each side don't accumulate the
    rounding drift a naive ``sum`` would on long series.
    """
    return math.fsum(values)


def streak_stats(returns: Sequence[float]) -> StreakStats:
    """
    Compute the win/loss run-length structure of a return series.

    Walks the series once, treating each period as +1 (win), -1 (loss) or 0
    (flat). Same-signed consecutive periods form a streak; a flat or a sign change
    ends the current streak. Returns longest/average win and loss runs and the
    signed length of the run ending at the final period.

    An empty series yields zero-length streaks and ``None`` averages.
    """
    max_win = 0
    max_loss = 0
    win_runs: List[int] = []
    loss_runs: List[int] = []

    run_sign = 0  # +1 win run, -1 loss run, 0 no active run
    run_len = 0

    def _close_run(sign: int, length: int) -> None:
        if sign > 0:
            win_runs.append(length)
        elif sign < 0:
            loss_runs.append(length)

    for r in returns:
        sign = 1 if r > 0.0 else (-1 if r < 0.0 else 0)
        if sign == 0:
            # Flat breaks any active run and starts none.
            if run_sign != 0:
                _close_run(run_sign, run_len)
            run_sign = 0
            run_len = 0
            continue
        if sign == run_sign:
            run_len += 1
        else:
            if run_sign != 0:
                _close_run(run_sign, run_len)
            run_sign = sign
            run_len = 1

    # Close the trailing run and record the current (final) streak.
    current = 0
    if run_sign != 0:
        _close_run(run_sign, run_len)
        current = run_sign * run_len

    max_win = max(win_runs) if win_runs else 0
    max_loss = max(loss_runs) if loss_runs else 0
    avg_win = statistics.fmean(win_runs) if win_runs else None
    avg_loss = statistics.fmean(loss_runs) if loss_runs else None

    return StreakStats(
        max_win_streak=max_win,
        max_loss_streak=max_loss,
        current_streak=current,
        avg_win_streak=avg_win,
        avg_loss_streak=avg_loss,
    )


def payoff_ratio(returns: Sequence[float]) -> Optional[float]:
    """
    Average win divided by the magnitude of the average loss.

    A payoff > 1 means a typical win is bigger than a typical loss; combined with
    win rate it determines expectancy. Returns ``None`` if there are no winning
    periods or no losing periods (the ratio is undefined without both).
    """
    wins, losses, _ = split_returns(returns)
    if not wins or not losses:
        return None
    avg_win = statistics.fmean(wins)
    avg_loss = statistics.fmean(losses)
    if avg_loss == 0.0:  # losses are strictly negative, so this can't happen
        return None
    return avg_win / abs(avg_loss)


def expectancy(returns: Sequence[float]) -> Optional[float]:
    """
    Mean return per period across all observations (wins, losses and flats).

    This is the simple average return — the bottom line that hit-rate and payoff
    together produce. Returns ``None`` for an empty series.
    """
    if not returns:
        return None
    return statistics.fmean(returns)


def win_loss_summary(returns: Sequence[float]) -> WinLossSummary:
    """
    Compute the full :class:`WinLossSummary` for a period-return series.

    Args:
        returns: Period-over-period returns as fractions (0.01 = +1%). Wins are
            strictly positive, losses strictly negative, flats exactly zero.

    Returns:
        A :class:`WinLossSummary` bundling counts, win/loss rates, per-side
        :class:`SideStats`, payoff ratio, profit factor, expectancy and
        :class:`StreakStats`. Every per-statistic minimum-data rule documented on
        the component types applies; nothing throws on a short or empty series.
        An empty series yields zero counts, 0.0 rates, empty side stats and
        ``None`` for the undefined-without-data fields.
    """
    wins, losses, flats = split_returns(returns)
    n = len(returns)
    loss_rate = (len(losses) / n) if n else 0.0
    return WinLossSummary(
        num_periods=n,
        num_wins=len(wins),
        num_losses=len(losses),
        num_flats=len(flats),
        win_rate=win_rate(returns),
        loss_rate=loss_rate,
        wins=side_stats(wins),
        losses=side_stats(losses),
        payoff_ratio=payoff_ratio(returns),
        profit_factor=profit_factor(returns),
        expectancy=expectancy(returns),
        streaks=streak_stats(returns),
    )
