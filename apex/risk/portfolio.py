"""
apex.risk.portfolio
===================
The Portfolio is the position/cash/equity/drawdown tracker that CLOSES THE
RISK LOOP.

It consumes FillEvents (to update holdings and cash) and MarketEvents (to
mark positions to market), then exposes a READ-ONLY snapshot that the
RiskManager reads on every signal evaluation.

Design invariants:
  - All money math uses Decimal — never float.
  - State mutations happen ONLY inside on_fill() and on_market().
  - The six snapshot attributes the RiskManager reads are properties
    (computed from internal state) so they are always consistent.
  - Deterministic: given the same sequence of fills and market events the
    portfolio will always reach the same state.
  - No I/O beyond logging.
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from decimal import Decimal
from typing import Deque, Dict, Optional

from apex.core.events import FillEvent, MarketEvent
from apex.core.models import OrderSide, Position, Symbol

# Rolling daily-return window for realized-volatility targeting (annualized).
_VOL_WINDOW = 30
_VOL_MIN_OBS = 20
_ANN = Decimal(252).sqrt()  # exact, no float intermediary

logger = logging.getLogger("apex.risk.portfolio")

_ZERO = Decimal("0")
_ONE = Decimal("1")


class Portfolio:
    """
    Tracks cash, positions, equity and drawdown across the trading session.

    Args:
        initial_capital: Starting cash balance.  Equity begins equal to cash.
    """

    def __init__(self, initial_capital: Decimal) -> None:
        if initial_capital < _ZERO:
            raise ValueError("initial_capital must be non-negative")

        self._cash: Decimal = initial_capital
        self._realized_pnl: Decimal = _ZERO

        # ticker -> Position (non-zero positions only)
        self._positions: Dict[str, Position] = {}

        # ticker -> symbol (so we can recreate Positions after market updates)
        self._symbols: Dict[str, Symbol] = {}

        # ticker -> latest known price
        self._last_price: Dict[str, Decimal] = {}

        # equity tracking
        initial_equity = initial_capital
        self._peak_equity: Decimal = initial_equity
        self._day_start_equity: Decimal = initial_equity
        # rolling daily returns (close-of-day equity changes) for vol targeting
        self._daily_returns: Deque[float] = deque(maxlen=_VOL_WINDOW)

        logger.info(
            "Portfolio initialised: capital=%s",
            initial_capital,
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_fill(self, fill: FillEvent) -> None:
        """
        Process a confirmed execution.

        For a BUY:  cash decreases by (qty * price + commission).
        For a SELL: cash increases by (qty * price - commission).

        avg_entry_price is updated correctly whether we are adding to,
        reducing, or closing a position.  Realized P&L is booked on reduces
        and closes.
        """
        ticker: str = fill.symbol.ticker
        qty: Decimal = fill.quantity  # always positive per FillEvent
        price: Decimal = fill.fill_price
        commission: Decimal = fill.commission

        # Remember the Symbol so we can reconstruct Position objects later.
        self._symbols[ticker] = fill.symbol

        # Update last_price so the RiskManager has a reference price.
        self._last_price[ticker] = price

        existing: Optional[Position] = self._positions.get(ticker)

        if fill.side == OrderSide.BUY:
            self._cash -= qty * price + commission
            # Book realized P&L when this BUY covers a short, mirroring the SELL branch.
            if existing is not None and existing.quantity < _ZERO:
                covered = min(qty, -existing.quantity)
                self._realized_pnl += (
                    (existing.avg_entry_price - price) * covered * fill.symbol.contract_multiplier
                )
            new_pos = self._apply_buy(existing, fill.symbol, qty, price)
            if new_pos.quantity == _ZERO:
                self._positions.pop(ticker, None)  # exact cover — don't leave a zero-qty zombie
            else:
                self._positions[ticker] = new_pos

        else:  # SELL
            self._cash += qty * price - commission
            pnl, new_pos = self._apply_sell(existing, fill.symbol, qty, price)
            self._realized_pnl += pnl
            if new_pos is None or new_pos.quantity == _ZERO:
                self._positions.pop(ticker, None)
            else:
                self._positions[ticker] = new_pos

        self._update_peak()

        logger.debug(
            "on_fill: %s %s qty=%s @%s  cash=%s realized_pnl=%s",
            fill.side.value,
            ticker,
            qty,
            price,
            self._cash,
            self._realized_pnl,
        )

    def on_market(self, event: MarketEvent) -> None:
        """
        Mark the affected position to market using the bar's close price.
        Updates last_price, recomputes equity, peak, and drawdown.
        """
        bar = event.bar
        if bar is None:
            # Tick-based market event — we only mark to bar closes for now.
            return

        ticker: str = bar.symbol.ticker
        close: Decimal = bar.close

        self._last_price[ticker] = close
        self._symbols[ticker] = bar.symbol

        pos = self._positions.get(ticker)
        if pos is not None:
            # Replace the frozen Position with an updated current_price.
            self._positions[ticker] = Position(
                symbol=pos.symbol,
                quantity=pos.quantity,
                avg_entry_price=pos.avg_entry_price,
                current_price=close,
                stop_loss=pos.stop_loss,
                take_profit=pos.take_profit,
            )

        self._update_peak()

        logger.debug(
            "on_market: %s close=%s  equity=%s drawdown=%.4f",
            ticker,
            close,
            self.equity,
            float(self.drawdown),
        )

    # ------------------------------------------------------------------
    # Day-boundary helper
    # ------------------------------------------------------------------

    def start_new_day(self) -> None:
        """
        Call at the start of each trading day to reset the daily-loss baseline.
        The RiskManager's daily-loss circuit breaker uses day_start_equity.
        Also banks the prior day's return for realized-volatility targeting.
        """
        prev = self._day_start_equity
        cur = self.equity
        if prev > _ZERO:
            self._daily_returns.append(float((cur - prev) / prev))
        self._day_start_equity = cur
        logger.info(
            "New trading day: day_start_equity set to %s",
            self._day_start_equity,
        )

    @property
    def realized_volatility(self) -> Optional[float]:
        """
        Annualized realized volatility from recent daily returns, or None until
        there is enough data. The RiskManager reads this to scale exposure toward a
        target volatility (de-risk into turbulence). None => no scaling.
        """
        if len(self._daily_returns) < _VOL_MIN_OBS:
            return None
        return statistics.pstdev(self._daily_returns) * float(_ANN)

    # ------------------------------------------------------------------
    # RiskManager snapshot attributes (the 6 required)
    # ------------------------------------------------------------------

    @property
    def equity(self) -> Decimal:
        """Total account equity = cash + sum of all position market values."""
        return self._cash + sum(p.market_value for p in self._positions.values())

    @property
    def peak_equity(self) -> Decimal:
        """Highest equity value ever observed (for drawdown computation)."""
        return self._peak_equity

    @property
    def day_start_equity(self) -> Decimal:
        """Equity at the start of the current trading day."""
        return self._day_start_equity

    @property
    def open_positions(self) -> Dict[str, Position]:
        """Dict of ticker -> Position for all non-zero positions (read-only view)."""
        # Return a shallow copy so callers cannot mutate our internal dict.
        return dict(self._positions)

    @property
    def exposure(self) -> Decimal:
        """
        Total absolute notional deployed = sum(abs(market_value)) across all
        open positions.
        """
        return sum(abs(p.market_value) for p in self._positions.values())

    @property
    def last_price(self) -> Dict[str, Decimal]:
        """Latest seen price per symbol ticker (read-only view)."""
        return dict(self._last_price)

    # ------------------------------------------------------------------
    # Additional informational properties
    # ------------------------------------------------------------------

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def realized_pnl(self) -> Decimal:
        """Cumulative realized P&L since inception."""
        return self._realized_pnl

    @property
    def unrealized_pnl(self) -> Decimal:
        """Sum of unrealized P&L across all open positions."""
        return sum(p.unrealized_pnl for p in self._positions.values())

    @property
    def drawdown(self) -> Decimal:
        """
        Current drawdown as a fraction from peak equity.
        Range: 0 (at peak) to 1 (total loss).  Never negative.
        """
        if self._peak_equity <= _ZERO:
            return _ZERO
        dd = (self._peak_equity - self.equity) / self._peak_equity
        return max(_ZERO, dd)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _update_peak(self) -> None:
        """Advance peak_equity if current equity is a new high."""
        current = self.equity
        if current > self._peak_equity:
            self._peak_equity = current

    @staticmethod
    def _apply_buy(
        existing: Optional[Position],
        symbol: Symbol,
        qty: Decimal,
        price: Decimal,
    ) -> Position:
        """
        Compute the new Position after a buy fill.

        If there is no existing position, open one at fill price.
        If adding to a long, compute the new weighted-average entry price.
        If covering a short (existing.quantity < 0), handle the reduction.
        """
        if existing is None or existing.quantity == _ZERO:
            return Position(
                symbol=symbol,
                quantity=qty,
                avg_entry_price=price,
                current_price=price,
            )

        old_qty = existing.quantity

        if old_qty > _ZERO:
            # Adding to a long position — weighted average entry.
            total_qty = old_qty + qty
            new_avg = (old_qty * existing.avg_entry_price + qty * price) / total_qty
            return Position(
                symbol=symbol,
                quantity=total_qty,
                avg_entry_price=new_avg,
                current_price=price,
                stop_loss=existing.stop_loss,
                take_profit=existing.take_profit,
            )

        # Covering a short position.
        new_qty = old_qty + qty  # moves toward zero (or flips long)
        if new_qty == _ZERO:
            # Fully closed — caller will remove from dict.
            return Position(
                symbol=symbol,
                quantity=_ZERO,
                avg_entry_price=_ZERO,
                current_price=price,
            )
        if new_qty < _ZERO:
            # Partial cover — avg_entry_price (short entry) unchanged.
            return Position(
                symbol=symbol,
                quantity=new_qty,
                avg_entry_price=existing.avg_entry_price,
                current_price=price,
                stop_loss=existing.stop_loss,
                take_profit=existing.take_profit,
            )
        # Flip: covered the short and went long.
        return Position(
            symbol=symbol,
            quantity=new_qty,
            avg_entry_price=price,
            current_price=price,
        )

    @staticmethod
    def _apply_sell(
        existing: Optional[Position],
        symbol: Symbol,
        qty: Decimal,
        price: Decimal,
    ) -> tuple:
        """
        Compute realized P&L and the new Position after a sell fill.

        Returns (realized_pnl, new_position_or_None).

        If there is no existing long, treat as opening a new short.
        """
        if existing is None or existing.quantity == _ZERO:
            # Opening a short from flat.
            new_pos = Position(
                symbol=symbol,
                quantity=-qty,
                avg_entry_price=price,
                current_price=price,
            )
            return (_ZERO, new_pos)

        old_qty = existing.quantity

        if old_qty > _ZERO:
            # Reducing / closing a long.
            closed_qty = min(qty, old_qty)
            pnl = (price - existing.avg_entry_price) * closed_qty * symbol.contract_multiplier
            new_qty = old_qty - qty
            if new_qty <= _ZERO:
                if new_qty < _ZERO:
                    # Flip: closed the long and went short.
                    new_pos = Position(
                        symbol=symbol,
                        quantity=new_qty,
                        avg_entry_price=price,
                        current_price=price,
                    )
                else:
                    new_pos = None  # exactly closed
                return (pnl, new_pos)
            # Partial reduction.
            new_pos = Position(
                symbol=symbol,
                quantity=new_qty,
                avg_entry_price=existing.avg_entry_price,
                current_price=price,
                stop_loss=existing.stop_loss,
                take_profit=existing.take_profit,
            )
            return (pnl, new_pos)

        # Adding to a short (old_qty < 0).
        new_qty = old_qty - qty
        total_qty = abs(old_qty) + qty
        new_avg = (abs(old_qty) * existing.avg_entry_price + qty * price) / total_qty
        new_pos = Position(
            symbol=symbol,
            quantity=new_qty,
            avg_entry_price=new_avg,
            current_price=price,
            stop_loss=existing.stop_loss,
            take_profit=existing.take_profit,
        )
        return (_ZERO, new_pos)
