"""
apex.risk.leverage_metrics
==========================
Gross and net leverage from open positions and account equity.

Two numbers describe how much market exposure a book is carrying relative to
the capital backing it:

  - GROSS leverage = sum of the ABSOLUTE notional of every position / equity.
    Longs and shorts BOTH add to it — a market-neutral book that is 100% long
    and 100% short is 2.0x gross even though it has no directional bet. This is
    the number that governs margin and tail risk.

  - NET leverage = the SIGNED notional (longs minus shorts) / equity. It is the
    book's directional tilt: +1.0 means net 100% long, -0.5 means net 50% short,
    0 means market-neutral.

These mirror the same exposure-vs-equity logic the RiskManager enforces in
`_check_leverage`, exposed here as a pure, read-only reporting layer over a
Portfolio-style snapshot (or any iterable of Positions).

Design invariants:
  - All money / notional / leverage math uses Decimal — never float (this is the
    risk layer; follow apex.risk.portfolio, not the float-based metrics layer).
  - Pure and deterministic: same positions + equity -> same result, always.
  - Fails closed on degenerate input: non-positive equity yields None for every
    leverage ratio (you cannot lever against zero/negative capital), never a
    garbage or infinite number.
  - No I/O. No mutation of the inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, Optional, Union

from apex.core.models import Position

_ZERO = Decimal("0")


def _to_decimal(value: object) -> Decimal:
    """Coerce a numeric input to Decimal without binary-float contamination."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def gross_exposure(positions: Iterable[Position]) -> Decimal:
    """
    Sum of the ABSOLUTE market value (notional) of every position.

    Longs and shorts both contribute their magnitude, so this is the total
    capital at risk to the market regardless of direction. Empty book -> 0.
    """
    return sum((abs(p.market_value) for p in positions), _ZERO)


def net_exposure(positions: Iterable[Position]) -> Decimal:
    """
    Signed market value (notional) summed across positions: longs positive,
    shorts negative. The book's directional exposure. Empty book -> 0.
    """
    return sum((p.market_value for p in positions), _ZERO)


def long_exposure(positions: Iterable[Position]) -> Decimal:
    """Total notional of long positions only (>= 0). Empty/flat -> 0."""
    return sum((p.market_value for p in positions if p.market_value > _ZERO), _ZERO)


def short_exposure(positions: Iterable[Position]) -> Decimal:
    """
    Total ABSOLUTE notional of short positions only (>= 0). Returned as a
    positive magnitude. Empty/flat -> 0.
    """
    return sum((abs(p.market_value) for p in positions if p.market_value < _ZERO), _ZERO)


def gross_leverage(
    positions: Iterable[Position],
    equity: Union[Decimal, int, float, str],
) -> Optional[Decimal]:
    """
    Gross leverage = gross_exposure / equity.

    Returns None if equity is non-positive (cannot lever against zero or
    negative capital) — fail closed rather than emit a meaningless or infinite
    ratio. A flat book against positive equity returns Decimal('0').
    """
    eq = _to_decimal(equity)
    if eq <= _ZERO:
        return None
    return gross_exposure(positions) / eq


def net_leverage(
    positions: Iterable[Position],
    equity: Union[Decimal, int, float, str],
) -> Optional[Decimal]:
    """
    Net leverage = net_exposure / equity (signed: positive = net long,
    negative = net short).

    Returns None if equity is non-positive — fail closed.
    """
    eq = _to_decimal(equity)
    if eq <= _ZERO:
        return None
    return net_exposure(positions) / eq


@dataclass(frozen=True)
class LeverageSnapshot:
    """
    An immutable, fully-computed leverage report for a single point in time.

    `equity` is the account equity used as the denominator. The four exposure
    fields are always populated (they need no denominator); the two leverage
    ratios are None when equity is non-positive (fail-closed). `is_levered`
    flags whether gross leverage exceeds 1.0x (more market exposure than
    capital).
    """
    equity: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    long_exposure: Decimal
    short_exposure: Decimal
    gross_leverage: Optional[Decimal]
    net_leverage: Optional[Decimal]

    @property
    def is_levered(self) -> bool:
        """True if gross leverage is strictly above 1.0x. False if unknown."""
        if self.gross_leverage is None:
            return False
        return self.gross_leverage > Decimal("1")

    @property
    def is_market_neutral(self) -> bool:
        """True if the book carries exposure but is net-flat (net == 0)."""
        return self.gross_exposure > _ZERO and self.net_exposure == _ZERO

    def to_dict(self) -> Dict[str, Optional[Decimal]]:
        """Plain dict view (e.g. for logging or serialization upstream)."""
        return {
            "equity": self.equity,
            "gross_exposure": self.gross_exposure,
            "net_exposure": self.net_exposure,
            "long_exposure": self.long_exposure,
            "short_exposure": self.short_exposure,
            "gross_leverage": self.gross_leverage,
            "net_leverage": self.net_leverage,
        }


def compute_leverage(
    positions: Iterable[Position],
    equity: Union[Decimal, int, float, str],
) -> LeverageSnapshot:
    """
    Compute the full leverage report in a single pass-friendly call.

    `positions` may be any iterable of Position (e.g. the values of a
    Portfolio.open_positions dict). `equity` is the account equity denominator
    (e.g. Portfolio.equity). The iterable is materialized once so a one-shot
    generator is consumed correctly.
    """
    pos_list = list(positions)
    eq = _to_decimal(equity)

    gross = gross_exposure(pos_list)
    net = net_exposure(pos_list)
    longs = long_exposure(pos_list)
    shorts = short_exposure(pos_list)

    if eq <= _ZERO:
        gross_lev: Optional[Decimal] = None
        net_lev: Optional[Decimal] = None
    else:
        gross_lev = gross / eq
        net_lev = net / eq

    return LeverageSnapshot(
        equity=eq,
        gross_exposure=gross,
        net_exposure=net,
        long_exposure=longs,
        short_exposure=shorts,
        gross_leverage=gross_lev,
        net_leverage=net_lev,
    )


def leverage_from_portfolio(portfolio: object) -> LeverageSnapshot:
    """
    Convenience adapter: build a LeverageSnapshot directly from a Portfolio-style
    snapshot exposing `.open_positions` (dict of ticker -> Position) and
    `.equity` (Decimal). Read-only — it never mutates the portfolio.
    """
    positions = portfolio.open_positions.values()  # type: ignore[attr-defined]
    equity = portfolio.equity                       # type: ignore[attr-defined]
    return compute_leverage(positions, equity)
