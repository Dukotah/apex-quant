"""
apex.validation.capacity_estimate
=================================
How much money can this strategy actually run before it eats its own edge?

A strategy that looks great on $10k can be uninvestable at $10M: once your
orders are a meaningful fraction of a name's average daily volume (ADV), you
move the price against yourself and the backtested edge evaporates. This module
turns liquidity facts (ADV, price) and a chosen participation cap into a hard
dollar ceiling on deployable capital.

The core idea
-------------
  * For ONE name, the most you can trade per day without exceeding the cap is::

        tradable_dollars_per_day = ADV_shares * price * participation_cap

  * A strategy turns over its book at some rate (`turnover`, the fraction of the
    portfolio that has to be bought/sold per rebalance). To deploy capital `C`,
    the daily traded notional is `C * turnover`. Capacity is the largest `C`
    whose required daily trading still fits under the per-day tradable budget::

        capacity = tradable_dollars_per_day / turnover

  * For a basket, the budget is the sum across names (assuming you can spread
    flow across the book); capacity is constrained by total liquidity and the
    strategy's turnover.

Deliberately dependency-light (stdlib math only). Statistical/sizing layer, so
it follows the metrics.py convention and uses float, not Decimal. All functions
are pure and deterministic given their inputs. Insufficient / degenerate inputs
return a fail-closed estimate (capacity 0.0) rather than garbage. Tested in
tests/test_capacity_estimate.py against hand-computed values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class CapacityEstimate:
    """Result of a strategy capacity estimate (all dollar figures in USD)."""

    capacity_usd: float  # max deployable capital before breaching the cap
    daily_volume_usd: float  # total ADV across the basket, in dollars
    tradable_usd_per_day: float  # ADV dollars * participation_cap
    participation_cap: float  # fraction of ADV we allow ourselves to be
    turnover: float  # fraction of book traded per rebalance period
    num_names: int  # how many tradable names fed the estimate

    def summary(self) -> str:
        return (
            f"Capacity ~${self.capacity_usd:,.0f} "
            f"({self.num_names} names, {self.participation_cap:.1%} of "
            f"${self.daily_volume_usd:,.0f}/day ADV, turnover {self.turnover:.2f})"
        )


def adv_dollars(adv_shares: float, price: float) -> float:
    """
    Average daily traded *dollars* for one name = ADV in shares * price.

    Returns 0.0 for non-positive / non-finite inputs (fail closed).
    """
    if not _is_positive(adv_shares) or not _is_positive(price):
        return 0.0
    return adv_shares * price


def tradable_per_day(
    daily_volume_usd: float,
    participation_cap: float,
) -> float:
    """
    Dollars we can trade per day without exceeding the participation cap.

    participation_cap is the max fraction of ADV we're willing to be (e.g. 0.10
    = trade at most 10% of a name's daily volume). Out-of-range caps are clamped
    to (0, 1]; a non-positive cap or volume yields 0.0.
    """
    if not _is_positive(daily_volume_usd) or not _is_positive(participation_cap):
        return 0.0
    cap = min(participation_cap, 1.0)
    return daily_volume_usd * cap


def capacity_from_adv(
    adv_shares: float,
    price: float,
    participation_cap: float = 0.10,
    turnover: float = 1.0,
) -> CapacityEstimate:
    """
    Single-name strategy capacity from average daily volume and a participation cap.

    Args:
        adv_shares: average daily volume in shares.
        price: representative price per share (USD).
        participation_cap: max fraction of ADV the strategy may consume per day
            (default 0.10 = 10%). Clamped to (0, 1].
        turnover: fraction of the deployed book that must be traded each
            rebalance period (default 1.0 = the whole book turns over, the most
            conservative assumption). Values <= 0 are treated as "no trading
            required", which cannot bound capacity, so we fail closed at 0.0.

    Returns a CapacityEstimate. The capacity is the largest capital `C` such that
    `C * turnover <= ADV_dollars * participation_cap`.
    """
    return capacity_from_basket(
        [adv_shares],
        [price],
        participation_cap=participation_cap,
        turnover=turnover,
    )


def capacity_from_basket(
    adv_shares: Sequence[float],
    prices: Sequence[float],
    participation_cap: float = 0.10,
    turnover: float = 1.0,
) -> CapacityEstimate:
    """
    Capacity for a basket (multi-name) strategy.

    The per-day tradable budget is summed across names (you can spread your flow
    over the whole book). Names with non-positive ADV or price contribute zero
    liquidity and are not counted. Capacity is the budget divided by turnover.

    Args:
        adv_shares: per-name average daily volume in shares.
        prices: per-name price (must align 1:1 with adv_shares; the shorter of
            the two lengths is used).
        participation_cap: max fraction of each name's ADV consumed per day.
        turnover: fraction of book traded per rebalance period.

    Returns a CapacityEstimate, fail-closed (capacity 0.0) when there is no
    usable liquidity or turnover is non-positive.
    """
    n = min(len(adv_shares), len(prices))

    total_adv_usd = 0.0
    usable_names = 0
    for i in range(n):
        name_adv_usd = adv_dollars(adv_shares[i], prices[i])
        if name_adv_usd > 0.0:
            total_adv_usd += name_adv_usd
            usable_names += 1

    cap = participation_cap if _is_positive(participation_cap) else 0.0
    cap = min(cap, 1.0)
    tradable = tradable_per_day(total_adv_usd, cap)

    if tradable <= 0.0 or not _is_positive(turnover):
        capacity = 0.0
    else:
        capacity = tradable / turnover

    return CapacityEstimate(
        capacity_usd=capacity,
        daily_volume_usd=total_adv_usd,
        tradable_usd_per_day=tradable,
        participation_cap=cap,
        turnover=turnover if _is_positive(turnover) else 0.0,
        num_names=usable_names,
    )


def _is_positive(x: float) -> bool:
    """True iff x is a finite, strictly positive number."""
    return math.isfinite(x) and x > 0.0
