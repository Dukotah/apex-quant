"""
apex.execution.simulated
========================
SimulatedExecutionEngine: paper fills with deterministic slippage + commission.

Powers backtesting and paper trading. The engine never touches a real broker;
it models fills locally, applying adverse slippage on every market order so
that simulated P&L is *more* conservative than reality, not less.

Fill model:
  BUY  → fill at ref_price * (1 + slippage_pct)   # pay more than market
  SELL → fill at ref_price * (1 - slippage_pct)   # receive less than market

Slippage amount  = abs(fill_price - ref_price) * quantity
Commission total = commission_per_share * quantity

Determinism guarantee: broker_order_id is "SIM-N" where N is the
monotonically incrementing submit counter (1-based). Same sequence of
submit_order calls → same ids, always.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Callable, Dict, Optional

from apex.core.events import FillEvent, OrderEvent
from apex.core.models import OrderSide, OrderType, utc_now
from apex.execution.base_execution import BaseExecutionEngine

logger = logging.getLogger(__name__)


class SimulatedExecutionEngine(BaseExecutionEngine):
    """
    Paper execution engine for backtest and paper-trading modes.

    Parameters
    ----------
    slippage_pct:
        Fractional adverse slippage applied to every market fill (default 0.1 %).
    commission_per_share:
        Flat per-share (or per-unit) commission (default $0, Alpaca-style).
    on_fill:
        Optional callback invoked with each FillEvent after submission.
        Can also be bound later via bind_fill_handler().
    """

    def __init__(
        self,
        slippage_pct: Decimal = Decimal("0.001"),
        commission_per_share: Decimal = Decimal("0"),
        on_fill: Optional[Callable[[FillEvent], None]] = None,
    ) -> None:
        super().__init__(on_fill)
        self._slippage_pct: Decimal = slippage_pct
        self._commission_per_share: Decimal = commission_per_share
        # Latest known price per ticker.
        self._prices: Dict[str, Decimal] = {}
        # Monotonic order counter for deterministic broker_order_id.
        self._order_counter: int = 0

    # ------------------------------------------------------------------
    # Price feed
    # ------------------------------------------------------------------

    def update_price(self, ticker: str, price: Decimal) -> None:
        """Register (or refresh) the reference price for a ticker."""
        self._prices[ticker] = price
        logger.debug("SimulatedEngine: price updated %s → %s", ticker, price)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Mark the engine as connected (no-op for the simulator)."""
        self._connected = True
        logger.info("SimulatedExecutionEngine connected (paper mode).")

    def disconnect(self) -> None:
        """Idempotent disconnect."""
        if self._connected:
            self._connected = False
            logger.info("SimulatedExecutionEngine disconnected.")

    # ------------------------------------------------------------------
    # Order handling
    # ------------------------------------------------------------------

    def submit_order(self, order: OrderEvent) -> str:
        """
        Simulate a market fill for *order*.

        Only MARKET orders are filled immediately; other order types are logged
        and refused with a clear error — extend this method when LIMIT support
        is needed.

        Returns
        -------
        str
            The deterministic broker_order_id (e.g. "SIM-1").

        Raises
        ------
        ValueError
            If no reference price is known for the order's symbol.
        """
        ticker: str = order.symbol.ticker

        if ticker not in self._prices:
            msg = (
                f"SimulatedExecutionEngine: no reference price for '{ticker}'. "
                "Call update_price() before submitting orders (fail-closed)."
            )
            logger.error(msg)
            raise ValueError(msg)

        if order.order_type != OrderType.MARKET:
            msg = (
                f"SimulatedExecutionEngine: order type '{order.order_type}' is "
                "not supported — only MARKET orders are filled in simulation."
            )
            logger.error(msg)
            raise ValueError(msg)

        ref_price: Decimal = self._prices[ticker]

        # Adverse slippage: buyer pays more, seller receives less.
        if order.side == OrderSide.BUY:
            fill_price = ref_price * (Decimal("1") + self._slippage_pct)
        else:
            fill_price = ref_price * (Decimal("1") - self._slippage_pct)

        commission: Decimal = self._commission_per_share * order.quantity
        slippage_amount: Decimal = abs(fill_price - ref_price) * order.quantity

        # Deterministic id — increment BEFORE assigning so first id is SIM-1.
        self._order_counter += 1
        broker_order_id: str = f"SIM-{self._order_counter}"

        fill = FillEvent(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            fill_price=fill_price,
            commission=commission,
            slippage=slippage_amount,
            order_id=order.event_id,
            broker_order_id=broker_order_id,
            timestamp=utc_now(),
            is_paper=True,
        )

        logger.info(
            "SimulatedEngine fill: %s %s x%s @ %s (ref %s slip %s comm %s) id=%s",
            order.side,
            ticker,
            order.quantity,
            fill_price,
            ref_price,
            slippage_amount,
            commission,
            broker_order_id,
        )

        self._emit_fill(fill)
        return broker_order_id

    def cancel_order(self, broker_order_id: str) -> bool:
        """
        In simulation there are no working orders; cancellation is always
        trivially accepted.
        """
        logger.debug("SimulatedEngine: cancel_order(%s) — no-op.", broker_order_id)
        return True

    # ------------------------------------------------------------------
    # Account / position queries
    # ------------------------------------------------------------------

    def get_account_equity(self) -> Decimal:
        """
        The simulator has no live account; returns Decimal("0") as a safe default.
        The Portfolio module is the authoritative source of equity in backtest.
        """
        return Decimal("0")

    def reconcile_positions(self) -> Dict[str, object]:
        """
        No broker state to reconcile in simulation; returns an empty mapping.
        The Portfolio is the sole position truth in paper/backtest mode.
        """
        return {}

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_paper(self) -> bool:
        """Always True — this engine never touches real money."""
        return True
