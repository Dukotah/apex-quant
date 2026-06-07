"""
apex.validation.cost_model
==========================
Per-trade transaction cost modelling for the Validation Gauntlet.

Backtests that ignore frictions lie. A strategy that looks profitable gross can
be flatly unprofitable once you pay the broker, cross the spread, and suffer
slippage. This module turns a few cost assumptions into a per-trade cost
(expressed as a *fraction of notional*) and applies it to gross trade returns so
the gates downstream judge a strategy on its NET edge.

Three friction sources, all standard:

  1. Commission  — flat per-trade fee and/or a per-notional rate.
  2. Slippage    — adverse price move between decision and fill, quoted in basis
                   points of price (1 bp = 0.01%).
  3. Half-spread — you buy at the ask and sell at the bid; a round trip across
                   the quoted bid/ask spread costs roughly the full spread, so a
                   single fill costs half of it, quoted in basis points.

This is the *statistics/metrics* layer (mirrors apex/validation/metrics.py), so
costs are modelled with float as fractions/bps — NOT Decimal money. The Decimal
money path lives in apex/execution/simulated.py; this module is the offline
"what would frictions have done to my backtest" estimator.

All functions are pure and deterministic given their inputs. Tested in
tests/test_cost_model.py against hand-computed values.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# 1 basis point = 0.01% = 0.0001 as a fraction.
BPS_PER_UNIT = 10_000.0


@dataclass(frozen=True)
class CostModel:
    """
    Assumptions about per-trade frictions, all as conservative (cost-increasing)
    inputs. Defaults are zero so an unconfigured model is a transparent no-op.

    Parameters
    ----------
    commission_per_trade:
        Flat currency fee charged once per trade (e.g. 1.0 for $1/trade).
    commission_rate:
        Proportional commission as a fraction of notional (e.g. 0.0005 = 5 bps).
    slippage_bps:
        Adverse slippage in basis points of the traded price.
    half_spread_bps:
        Half of the quoted bid/ask spread, in basis points. One fill pays this
        once (a round trip pays it twice — see round_trip_cost_fraction).
    """

    commission_per_trade: float = 0.0
    commission_rate: float = 0.0
    slippage_bps: float = 0.0
    half_spread_bps: float = 0.0

    def __post_init__(self) -> None:
        # Fail closed: negative frictions would understate cost (free money).
        for name in (
            "commission_per_trade",
            "commission_rate",
            "slippage_bps",
            "half_spread_bps",
        ):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative, got {value!r}")

    # ------------------------------------------------------------------
    # Currency cost (absolute) — needs the trade notional.
    # ------------------------------------------------------------------

    def variable_fraction(self) -> float:
        """
        The notional-INDEPENDENT part of a one-way cost, as a fraction: the
        proportional commission plus slippage plus half-spread (the flat
        per-trade commission is excluded — it only matters once you know the
        notional). This is the cost fraction in the limit of a large trade.
        """
        return (
            self.commission_rate
            + self.slippage_bps / BPS_PER_UNIT
            + self.half_spread_bps / BPS_PER_UNIT
        )

    def cost_per_trade(self, notional: float) -> float:
        """
        Total one-way cost of a single fill in CURRENCY terms, given the trade's
        gross notional (price * quantity).

        cost = flat commission
             + commission_rate    * notional
             + slippage_fraction  * notional
             + half_spread_fraction * notional

        Notional is taken as its absolute value, so the sign of a position never
        flips a cost into a credit. Cost is always >= 0.
        """
        abs_notional = abs(notional)
        return self.commission_per_trade + self.variable_fraction() * abs_notional

    # ------------------------------------------------------------------
    # Fractional cost (relative) — for adjusting returns.
    # ------------------------------------------------------------------

    def cost_fraction(self, notional: float = 0.0) -> float:
        """
        One-way cost of a single fill as a FRACTION of notional.

        Equals variable_fraction() (notional-independent) plus the flat
        commission spread over the notional. With no flat commission this is
        purely (commission_rate + slippage + half_spread); the flat fee adds a
        fee/notional term that hurts small trades most.

        A zero (or omitted) notional drops only the flat-fee term — the bps-based
        fraction still applies — so callers with pure bps assumptions get the
        right answer without a divide-by-zero (never NaN/garbage).
        """
        abs_notional = abs(notional)
        flat_fraction = (
            0.0 if abs_notional == 0.0 else self.commission_per_trade / abs_notional
        )
        return self.variable_fraction() + flat_fraction

    def round_trip_cost_fraction(self, notional: float = 0.0) -> float:
        """
        Cost of a full round trip (entry + exit) as a fraction of notional:
        two fills, so two commissions, two slippages, two half-spreads (the two
        half-spreads sum to the full quoted spread).

        Defaults notional=0 so callers with only bps-based assumptions get the
        notional-independent round-trip fraction directly.
        """
        return 2.0 * self.cost_fraction(notional)


def net_trade_return(
    gross_return: float,
    model: CostModel,
    notional: float = 0.0,
) -> float:
    """
    Adjust a single trade's GROSS return for round-trip frictions.

    A trade return already reflects an entry and an exit, so we subtract the
    full round-trip cost fraction:

        net = gross - round_trip_cost_fraction(notional)

    `gross_return` and the result are fractions (0.02 = +2%). `notional` only
    matters when the model has a flat per-trade commission; for pure bps models
    it can be omitted.
    """
    return gross_return - model.round_trip_cost_fraction(notional)


def apply_costs(
    gross_trade_returns: Sequence[float],
    model: CostModel,
    notionals: Sequence[float] | None = None,
) -> list[float]:
    """
    Vectorised net_trade_return over a series of gross trade returns.

    If `notionals` is given it must be the same length as `gross_trade_returns`
    (a flat commission is charged against each trade's own notional). If omitted,
    a notional of 0 is assumed for every trade — correct for pure bps models and
    fine when there is no flat per-trade commission.

    Returns an empty list for empty input (never garbage).
    """
    n = len(gross_trade_returns)
    if n == 0:
        return []
    if notionals is None:
        return [net_trade_return(r, model) for r in gross_trade_returns]
    if len(notionals) != n:
        raise ValueError(
            f"notionals length {len(notionals)} != trade returns length {n}"
        )
    return [
        net_trade_return(r, model, notional)
        for r, notional in zip(gross_trade_returns, notionals)
    ]
