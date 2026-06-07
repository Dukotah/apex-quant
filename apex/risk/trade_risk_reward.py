"""
apex.risk.trade_risk_reward
===========================
Pure trade-geometry math: given an entry, a protective stop, and a profit
target, compute the per-trade risk/reward profile that every disciplined
strategy is judged on BEFORE a trade is ever placed.

Three layered concepts:

  * **risk-per-unit / reward-per-unit** — the absolute price distances from
    entry to stop (what you stand to lose) and entry to target (what you stand
    to gain) for one unit.
  * **risk/reward ratio (RRR)** — reward distance divided by risk distance. A
    setup with RRR >= 2 risks 1 to make 2.  This is direction-aware: a long
    has its stop below and target above entry; a short is the mirror.
  * **R-multiple** — the realized outcome of a CLOSED trade expressed in units
    of the initial risk ("R").  Exiting at +2R means you made twice what you
    risked.  Aggregated across trades it yields **expectancy** — the average
    R earned per trade, the single number that says whether an edge exists.

Conventions (this is the risk layer):
  - All prices and price-distances are ``Decimal`` — never float — matching
    apex/risk/portfolio.py and apex/risk/risk_manager.py.
  - Ratios, R-multiples and expectancy are derived, dimensionless quantities
    and are also kept as ``Decimal`` so callers in the risk layer never have to
    cross the Decimal/float boundary.
  - Pure and deterministic: no I/O, no clock, no randomness.
  - Fails closed on degenerate input: insufficient/contradictory geometry
    returns ``None`` rather than a garbage number (e.g. a zero-distance stop,
    or a stop on the wrong side of entry for the given direction).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from apex.core.models import OrderSide

_ZERO = Decimal("0")


def _as_decimal(value) -> Decimal:
    """Coerce a numeric input to Decimal without inheriting float noise."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def risk_per_unit(
    entry: Decimal,
    stop: Decimal,
    side: OrderSide,
) -> Optional[Decimal]:
    """
    Absolute price distance from entry to stop for one unit (the "R" value).

    Direction-aware:  for a long the stop must sit BELOW entry; for a short it
    must sit ABOVE.  A stop on the wrong side (or exactly at entry) is invalid
    geometry and yields ``None`` — there is no defined risk to size against.
    """
    entry = _as_decimal(entry)
    stop = _as_decimal(stop)
    if side == OrderSide.BUY:
        risk = entry - stop          # long: lose if price falls to stop
    else:
        risk = stop - entry          # short: lose if price rises to stop
    if risk <= _ZERO:
        return None
    return risk


def reward_per_unit(
    entry: Decimal,
    target: Decimal,
    side: OrderSide,
) -> Optional[Decimal]:
    """
    Absolute price distance from entry to target for one unit.

    Direction-aware:  for a long the target must sit ABOVE entry; for a short
    it must sit BELOW.  A target on the wrong side (or exactly at entry) yields
    ``None`` — there is no defined reward.
    """
    entry = _as_decimal(entry)
    target = _as_decimal(target)
    if side == OrderSide.BUY:
        reward = target - entry      # long: gain if price rises to target
    else:
        reward = entry - target      # short: gain if price falls to target
    if reward <= _ZERO:
        return None
    return reward


def risk_reward_ratio(
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    side: OrderSide,
) -> Optional[Decimal]:
    """
    Reward-to-risk ratio (RRR) of a planned trade = reward distance / risk
    distance.  RRR of 2 means the target is twice as far as the stop, i.e. you
    risk 1 to make 2.

    Returns ``None`` if either leg has invalid geometry (so risk distance is
    undefined or zero) — never divides by zero, never returns a bogus ratio.
    """
    risk = risk_per_unit(entry, stop, side)
    reward = reward_per_unit(entry, target, side)
    if risk is None or reward is None:
        return None
    return reward / risk


def r_multiple(
    entry: Decimal,
    stop: Decimal,
    exit_price: Decimal,
    side: OrderSide,
) -> Optional[Decimal]:
    """
    Realized R-multiple of a CLOSED trade: the P&L per unit expressed in units
    of the initial risk.

        R = (signed gain per unit) / (initial risk per unit)

    +2R means you earned twice what you risked; -1R means the stop was hit;
    a negative beyond -1R means the exit was worse than the planned stop
    (slippage / gap through stop).  The initial risk ("R") is anchored to the
    ORIGINAL entry/stop, not the exit, so it is comparable across trades.

    Returns ``None`` if the initial risk is undefined (bad entry/stop geometry).
    """
    risk = risk_per_unit(entry, stop, side)
    if risk is None:
        return None
    entry = _as_decimal(entry)
    exit_price = _as_decimal(exit_price)
    if side == OrderSide.BUY:
        gain = exit_price - entry    # long profits when exit > entry
    else:
        gain = entry - exit_price    # short profits when exit < entry
    return gain / risk


@dataclass(frozen=True)
class ExpectancySummary:
    """
    Aggregate R-statistics over a sequence of closed trades.

    Attributes:
        expectancy:   mean R per trade — the headline edge number. Positive =
                      the system makes money per trade on average (in R terms).
        trade_count:  number of trades summarized.
        win_rate:     fraction of trades with R > 0 (0..1).
        avg_win_r:    mean R of winning trades (R > 0), or 0 if none.
        avg_loss_r:   mean R of losing trades (R < 0) as a NEGATIVE number,
                      or 0 if none.
        total_r:      sum of all R-multiples.
    """
    expectancy: Decimal
    trade_count: int
    win_rate: Decimal
    avg_win_r: Decimal
    avg_loss_r: Decimal
    total_r: Decimal


def expectancy(r_multiples: Sequence[Decimal]) -> Optional[ExpectancySummary]:
    """
    Per-trade expectancy from a sequence of realized R-multiples.

    Expectancy (mean R) is the single number that tells you whether an edge
    exists: a positive value means that, over many trades, each trade adds R on
    average.  It folds win rate and the win/loss size asymmetry into one figure.

    Returns ``None`` for an empty sequence (nothing to summarize) — never
    divides by zero.  ``None`` entries in the sequence (e.g. trades whose risk
    geometry was undefined) are skipped; if all are skipped, returns ``None``.
    """
    rs = [_as_decimal(r) for r in r_multiples if r is not None]
    n = len(rs)
    if n == 0:
        return None

    total = sum(rs, _ZERO)
    wins = [r for r in rs if r > _ZERO]
    losses = [r for r in rs if r < _ZERO]

    win_rate = Decimal(len(wins)) / Decimal(n)
    avg_win = (sum(wins, _ZERO) / Decimal(len(wins))) if wins else _ZERO
    avg_loss = (sum(losses, _ZERO) / Decimal(len(losses))) if losses else _ZERO

    return ExpectancySummary(
        expectancy=total / Decimal(n),
        trade_count=n,
        win_rate=win_rate,
        avg_win_r=avg_win,
        avg_loss_r=avg_loss,
        total_r=total,
    )
