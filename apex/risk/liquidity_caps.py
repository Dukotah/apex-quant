"""
apex.risk.liquidity_caps
========================
ADVISORY liquidity sizing helpers.

A position can be "within risk limits" on a notional/exposure basis yet still be
*untradeable* in practice: if a name trades 50k shares a day, a 40k-share order
will move the market against you and may never fill at a sane price. The standard
guardrail is a PARTICIPATION LIMIT — you cap your order at some fraction of the
instrument's average daily volume (ADV) so you stay a small, anonymous part of
the tape.

This module is deliberately ADVISORY: it computes a *suggested* maximum size and
does NOT itself reject or place orders. The RiskManager remains the only producer
of OrderEvents; a caller may use `apply_liquidity_cap` to shrink an intended
quantity before sizing, or `liquidity_cap_quantity` to learn the ceiling. Like the
rest of the risk layer it works in Decimal (these are quantities/volumes, money-
adjacent) and FAILS CLOSED: on insufficient or degenerate data it returns a ZERO
cap (trade nothing) rather than an unbounded one.

Pure and deterministic: no I/O, no wall-clock, no randomness. Given the same
volume history and parameters it always returns the same cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from apex.core.models import Symbol

_ZERO = Decimal("0")


@dataclass(frozen=True)
class LiquidityCapConfig:
    """
    Immutable parameters for the advisory liquidity cap. Frozen so a caller
    cannot mutate the policy mid-run.

    Attributes:
        participation_pct: Max fraction of ADV a single order may represent
            (0.10 = 10% of average daily volume). Must be in (0, 1].
        adv_window: Number of most-recent volume observations to average. Only
            the last `adv_window` values are used; fewer is allowed once at
            least `min_observations` exist.
        min_observations: Fewest volume bars required before a cap is produced.
            Below this the data is considered insufficient and the cap is ZERO
            (fail closed — trade nothing rather than guess).
    """

    participation_pct: Decimal = Decimal("0.10")
    adv_window: int = 20
    min_observations: int = 5


def average_daily_volume(
    volumes: Sequence[Decimal],
    window: int = 20,
    min_observations: int = 5,
) -> Optional[Decimal]:
    """
    Mean of the most-recent `window` volume observations.

    Negative observations are treated as invalid data and the whole computation
    fails closed (returns None). Returns None when there are fewer than
    `min_observations` usable values — never garbage.

    Args:
        volumes: Per-bar traded volume, oldest-to-newest.
        window: How many trailing observations to average over.
        min_observations: Minimum count required to produce a value.

    Returns:
        Average daily volume as a Decimal, or None if data is insufficient
        or invalid.
    """
    if window <= 0 or min_observations <= 0:
        return None
    if len(volumes) < min_observations:
        return None

    recent = list(volumes[-window:])
    if len(recent) < min_observations:
        return None
    if any(v < _ZERO for v in recent):
        # Bad data → fail closed.
        return None

    total = _ZERO
    for v in recent:
        total += v
    return total / Decimal(len(recent))


def liquidity_cap_quantity(
    volumes: Sequence[Decimal],
    config: LiquidityCapConfig = LiquidityCapConfig(),
    *,
    symbol: Optional[Symbol] = None,
) -> Decimal:
    """
    Advisory maximum order quantity from ADV and the participation limit.

    cap = average_daily_volume(...) * participation_pct

    FAILS CLOSED: returns ZERO whenever the cap cannot be computed safely —
    insufficient/invalid volume history, a non-positive ADV, or a misconfigured
    participation_pct outside (0, 1]. A zero cap means "this name is too illiquid
    to size against right now," never "no limit."

    When `symbol` is supplied and the instrument is NOT fractionable, the cap is
    floored to a whole number of units (you cannot send 1.5 shares); fractionable
    instruments keep the fractional cap. With no symbol the raw Decimal cap is
    returned.

    Args:
        volumes: Per-bar traded volume, oldest-to-newest.
        config: Participation/window/minimum-observation policy.
        symbol: Optional instrument, used only to decide whole-vs-fractional units.

    Returns:
        The advisory maximum quantity as a non-negative Decimal (>= 0).
    """
    pct = config.participation_pct
    if pct <= _ZERO or pct > Decimal("1"):
        return _ZERO

    adv = average_daily_volume(
        volumes,
        window=config.adv_window,
        min_observations=config.min_observations,
    )
    if adv is None or adv <= _ZERO:
        return _ZERO

    cap = adv * pct
    if cap <= _ZERO:
        return _ZERO

    # Whole units unless the instrument is explicitly fractionable.
    if symbol is not None and not symbol.fractionable:
        return Decimal(int(cap))
    return cap


def apply_liquidity_cap(
    desired_quantity: Decimal,
    volumes: Sequence[Decimal],
    config: LiquidityCapConfig = LiquidityCapConfig(),
    *,
    symbol: Optional[Symbol] = None,
) -> Decimal:
    """
    Shrink an intended quantity down to the advisory liquidity cap.

    Returns min(desired_quantity, cap), never negative. Because the cap fails
    closed to ZERO on bad/insufficient data, an illiquid or unknown name yields
    a ZERO result — the caller ends up trading nothing rather than an unfillable
    size. A non-positive `desired_quantity` is clamped to ZERO.

    This does not place or reject orders; it only advises a size. The RiskManager
    is still the gate.

    Args:
        desired_quantity: The quantity the caller would otherwise trade.
        volumes: Per-bar traded volume, oldest-to-newest.
        config: Participation/window/minimum-observation policy.
        symbol: Optional instrument, used only to decide whole-vs-fractional units.

    Returns:
        The liquidity-capped quantity as a non-negative Decimal.
    """
    if desired_quantity <= _ZERO:
        return _ZERO
    cap = liquidity_cap_quantity(volumes, config, symbol=symbol)
    return min(desired_quantity, cap)
