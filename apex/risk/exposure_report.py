"""
apex.risk.exposure_report
=========================
Pure exposure analytics for a portfolio snapshot.

Given the open positions and the account equity, this module decomposes the
book into the exposures that actually matter for risk:

  - gross   = sum(abs(market_value))            — total notional deployed
  - net     = sum(market_value)                 — directional tilt (longs - shorts)
  - long    = sum(market_value where qty > 0)   — total long notional (>= 0)
  - short   = sum(abs(market_value) where qty < 0) — total short notional (>= 0)
  - per-symbol breakdown of each of the above

Each absolute figure is also expressed as a fraction of equity (its "pct"),
so a caller can compare against the RiskManager's percentage limits
(max_total_exposure_pct, max_leverage) directly.

Design invariants (this is the MONEY layer, mirroring apex.risk.portfolio):
  - All money/quantity math uses Decimal — never float.
  - PURE: no I/O, no wall-clock time, no randomness. Same inputs -> same output.
  - Reads positions read-only; never mutates the input.
  - Insufficient data fails gracefully: zero/empty exposures, and pct is None
    when equity is non-positive (cannot form a meaningful ratio) rather than
    dividing by zero or emitting garbage.
  - Determinism: per-symbol mappings are ordered by ticker for stable output.

The single public entry point is `build_exposure_report(positions, equity)`.
`positions` is any mapping of ticker -> Position (e.g. Portfolio.open_positions)
or any iterable of Position objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, Mapping, Optional, Union

from apex.core.models import Position

_ZERO = Decimal("0")

PositionsInput = Union[Mapping[str, Position], Iterable[Position]]


@dataclass(frozen=True)
class SymbolExposure:
    """Exposure attributable to a single instrument."""

    ticker: str
    quantity: Decimal  # signed: negative = short
    market_value: Decimal  # signed notional (qty * price * multiplier)
    gross: Decimal  # abs(market_value)  (>= 0)
    net: Decimal  # == market_value (signed); kept for symmetry
    long: Decimal  # market_value if long else 0  (>= 0)
    short: Decimal  # abs(market_value) if short else 0  (>= 0)
    pct_of_equity: Optional[Decimal]  # gross / equity, or None if equity <= 0

    @property
    def is_long(self) -> bool:
        return self.quantity > _ZERO

    @property
    def is_short(self) -> bool:
        return self.quantity < _ZERO


@dataclass(frozen=True)
class ExposureReport:
    """
    Portfolio-wide exposure decomposition plus a per-symbol breakdown.

    The four headline figures:
      gross — total absolute notional deployed (sum of abs market values).
      net   — directional notional (long minus short).
      long  — total long notional (>= 0).
      short — total short notional (>= 0).

    The matching `*_pct` fields express each absolute figure as a fraction of
    equity, or None when equity is non-positive (no meaningful ratio).
    """

    equity: Decimal
    gross: Decimal
    net: Decimal
    long: Decimal
    short: Decimal
    gross_pct: Optional[Decimal]
    net_pct: Optional[Decimal]
    long_pct: Optional[Decimal]
    short_pct: Optional[Decimal]
    by_symbol: Dict[str, SymbolExposure] = field(default_factory=dict)

    @property
    def num_positions(self) -> int:
        """Count of open (non-zero) positions in the report."""
        return len(self.by_symbol)

    @property
    def num_long(self) -> int:
        return sum(1 for s in self.by_symbol.values() if s.is_long)

    @property
    def num_short(self) -> int:
        return sum(1 for s in self.by_symbol.values() if s.is_short)

    @property
    def leverage(self) -> Optional[Decimal]:
        """
        Gross leverage = gross exposure / equity (same ratio the RiskManager
        caps via max_leverage). None when equity is non-positive.
        """
        return self.gross_pct


def _iter_positions(positions: PositionsInput) -> Iterable[Position]:
    """Yield Position objects from either a ticker->Position mapping or an iterable."""
    if isinstance(positions, Mapping):
        return positions.values()
    return positions


def _safe_pct(value: Decimal, equity: Decimal) -> Optional[Decimal]:
    """value / equity, or None when equity is non-positive (no meaningful ratio)."""
    if equity <= _ZERO:
        return None
    return value / equity


def build_exposure_report(
    positions: PositionsInput,
    equity: Decimal,
) -> ExposureReport:
    """
    Decompose a set of positions into gross/net/long/short exposure, both in
    absolute notional and as a fraction of `equity`, with a per-symbol breakdown.

    Args:
        positions: ticker -> Position mapping (e.g. Portfolio.open_positions)
                   or any iterable of Position objects. Zero-quantity positions
                   contribute nothing and are omitted from the breakdown.
        equity: total account equity. Used only as the denominator for the
                pct fields; if <= 0 those fields are None.

    Returns:
        An ExposureReport. With no positions, all absolute figures are 0 and the
        per-symbol breakdown is empty (graceful empty-window behaviour).
    """
    equity = Decimal(equity)

    long_total = _ZERO
    short_total = _ZERO
    by_symbol: Dict[str, SymbolExposure] = {}

    for pos in _iter_positions(positions):
        qty = pos.quantity
        if qty == _ZERO:
            continue

        mv = pos.market_value  # signed notional
        gross = abs(mv)
        if qty > _ZERO:
            long_total += mv
            sym_long = mv
            sym_short = _ZERO
        else:
            short_total += gross
            sym_long = _ZERO
            sym_short = gross

        by_symbol[pos.symbol.ticker] = SymbolExposure(
            ticker=pos.symbol.ticker,
            quantity=qty,
            market_value=mv,
            gross=gross,
            net=mv,
            long=sym_long,
            short=sym_short,
            pct_of_equity=_safe_pct(gross, equity),
        )

    gross_total = long_total + short_total
    net_total = long_total - short_total

    # Deterministic, stable output: order the per-symbol map by ticker.
    ordered = {t: by_symbol[t] for t in sorted(by_symbol)}

    return ExposureReport(
        equity=equity,
        gross=gross_total,
        net=net_total,
        long=long_total,
        short=short_total,
        gross_pct=_safe_pct(gross_total, equity),
        net_pct=_safe_pct(net_total, equity),
        long_pct=_safe_pct(long_total, equity),
        short_pct=_safe_pct(short_total, equity),
        by_symbol=ordered,
    )
