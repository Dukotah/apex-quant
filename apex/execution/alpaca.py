"""
apex.execution.alpaca
=====================
AlpacaExecutionEngine: real order submission to Alpaca (paper or live).

This is the only place in the system that can move real money, so it is built to
fail safe at every step:

  - **Idempotent submits.** Each OrderEvent already carries a stable ``event_id``;
    we pass it to Alpaca as the ``client_order_id``. Before submitting we ask the
    broker whether an order with that id already exists — so a retried cron run
    (the same OrderEvent re-evaluated) can never double-submit. The broker, not
    our memory, is the source of truth for "did this already go through."

  - **Broker-truth fills.** A FillEvent is emitted ONLY from what the broker
    reports filled (``filled_qty`` / ``filled_avg_price``) — never an optimistic
    local estimate. If the order is still working when this process exits, no fill
    is booked; the next run's reconciliation reflects reality. This preserves
    backtest/live parity: a position changes only on a confirmed fill.

  - **Partial fills** are emitted for whatever quantity actually filled.

  - **Disconnect = safe mode.** On disconnect we cancel working orders so a dying
    process never leaves unmanaged exposure.

  - **Startup reconciliation.** ``reconcile_positions()`` returns the broker's
    real positions so the local Portfolio syncs to truth before trading, never
    drifting from it.

Offline testability (Golden Rule 12): the alpaca-py ``TradingClient`` is wrapped
behind a tiny ``BrokerClient`` seam injected via the constructor. Every safety
property above is unit-tested with a fake broker; the real adapter is a thin,
lazily-imported wrapper verified against paper keys, not in CI.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Callable, Dict, Optional, Protocol

from apex.core.events import FillEvent, OrderEvent
from apex.core.models import OrderSide, OrderType, utc_now
from apex.execution.base_execution import BaseExecutionEngine

logger = logging.getLogger(__name__)

# Alpaca order statuses we treat as terminal (no point polling further).
_TERMINAL_STATUSES = frozenset({"filled", "canceled", "cancelled", "rejected", "expired", "done_for_day"})


def _status_str(status: object) -> str:
    """
    Normalize an order status to a lowercase string. Alpaca returns an
    ``OrderStatus`` enum whose ``str()`` is ``'OrderStatus.FILLED'``, not
    ``'filled'`` — so read ``.value`` when present (plain strings pass through).
    """
    return str(getattr(status, "value", status)).lower()


class BrokerClient(Protocol):
    """
    The minimal broker surface AlpacaExecutionEngine depends on. The real
    adapter wraps alpaca-py's TradingClient; tests pass a fake implementing this.
    Order objects expose: ``id``, ``status``, ``filled_qty``, ``filled_avg_price``.
    Position objects expose: ``symbol``, ``qty``, ``avg_entry_price``, ``current_price``.
    """

    def find_order_by_client_id(self, client_order_id: str) -> Optional[object]: ...
    def submit_market_order(self, symbol: str, qty: Decimal, side: str,
                            client_order_id: str, time_in_force: str) -> object: ...
    def get_order(self, broker_order_id: str) -> object: ...
    def cancel_order(self, broker_order_id: str) -> bool: ...
    def cancel_open_orders(self) -> None: ...
    def get_account_equity(self) -> Decimal: ...
    def list_positions(self) -> list: ...


class AlpacaExecutionEngine(BaseExecutionEngine):
    """
    Live/paper execution against Alpaca.

    Parameters
    ----------
    api_key / api_secret:
        Alpaca credentials (env ALPACA_API_KEY / ALPACA_SECRET_KEY by default).
    paper:
        True → Alpaca paper endpoint (no real money); False → LIVE (real money).
    fill_poll_attempts / poll_interval:
        After submitting, poll the broker up to this many times for a terminal
        state before giving up and leaving the order to be reconciled next run.
    cancel_open_orders_on_disconnect:
        Safe-mode default — cancel working orders when the engine disconnects.
    broker_client:
        DEPENDENCY INJECTION for tests; bypasses the alpaca-py SDK entirely.
    sleep:
        injectable sleep so polling is instant in tests.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        paper: bool = True,
        fill_poll_attempts: int = 3,
        poll_interval: float = 1.0,
        cancel_open_orders_on_disconnect: bool = True,
        on_fill: Optional[Callable[[FillEvent], None]] = None,
        broker_client: Optional[BrokerClient] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(on_fill)
        self._api_key = api_key
        self._api_secret = api_secret
        self._paper = paper
        self._fill_poll_attempts = max(1, fill_poll_attempts)
        self._poll_interval = poll_interval
        self._cancel_on_disconnect = cancel_open_orders_on_disconnect
        self._sleep = sleep

        self._client: Optional[BrokerClient] = broker_client
        self._injected = broker_client is not None
        # Maps our OrderEvent id → broker order id, for traceability/cancel.
        self._submitted: Dict[str, str] = {}

    # ------------------------------------------------------------------ lifecycle

    def connect(self) -> None:
        """Build/verify the broker client. With an injected client this is a no-op."""
        if not self._injected:
            self._client = self._build_sdk_client()
        self._connected = True
        logger.info("AlpacaExecutionEngine connected (paper=%s).", self._paper)

    def disconnect(self) -> None:
        """Safe mode: cancel working orders (best-effort), then tear down. Idempotent."""
        if not self._connected:
            return
        if self._cancel_on_disconnect and self._client is not None:
            try:
                self._client.cancel_open_orders()
                logger.info("AlpacaExecutionEngine: cancelled open orders on disconnect.")
            except Exception as exc:  # noqa: BLE001 — disconnect must never raise
                logger.error("Failed to cancel open orders on disconnect: %s", exc)
        self._connected = False

    # ------------------------------------------------------------------ orders

    def submit_order(self, order: OrderEvent) -> str:
        """
        Submit an approved order to Alpaca, idempotently, and emit a FillEvent for
        whatever the broker confirms filled. Returns the broker order id.

        Only MARKET orders are supported here (the strategies + risk manager emit
        market orders); other types fail closed.
        """
        if not self._connected or self._client is None:
            raise RuntimeError("submit_order() called before connect()")
        if order.order_type != OrderType.MARKET:
            raise ValueError(
                f"AlpacaExecutionEngine supports MARKET orders only, got {order.order_type}"
            )

        client_order_id = order.event_id   # stable → idempotency key

        # Idempotency: if this exact order already reached the broker, adopt it
        # instead of submitting again.
        existing = self._client.find_order_by_client_id(client_order_id)
        if existing is not None:
            broker_id = str(getattr(existing, "id"))
            logger.warning(
                "Order %s already exists at broker as %s — not resubmitting (idempotent).",
                client_order_id, broker_id,
            )
            broker_order = self._poll_for_fill(broker_id)
        else:
            submitted = self._client.submit_market_order(
                symbol=order.symbol.ticker,
                qty=order.quantity,
                side=order.side.value,
                client_order_id=client_order_id,
                time_in_force=order.time_in_force.value,
            )
            broker_id = str(getattr(submitted, "id"))
            self._submitted[order.event_id] = broker_id
            logger.info("Submitted %s %s x%s → broker order %s",
                        order.side.value, order.symbol.ticker, order.quantity, broker_id)
            broker_order = self._poll_for_fill(broker_id)

        self._submitted[order.event_id] = broker_id
        self._maybe_emit_fill(order, broker_order, broker_id)
        return broker_id

    def cancel_order(self, broker_order_id: str) -> bool:
        if not self._connected or self._client is None:
            raise RuntimeError("cancel_order() called before connect()")
        try:
            return bool(self._client.cancel_order(broker_order_id))
        except Exception as exc:  # noqa: BLE001
            logger.error("cancel_order(%s) failed: %s", broker_order_id, exc)
            return False

    # ----------------------------------------------------------- account / sync

    def get_account_equity(self) -> Decimal:
        if not self._connected or self._client is None:
            raise RuntimeError("get_account_equity() called before connect()")
        return Decimal(str(self._client.get_account_equity()))

    def reconcile_positions(self) -> Dict[str, dict]:
        """
        Fetch the broker's real positions so the local Portfolio can sync to truth.
        Returns ``{ticker: {qty, avg_entry_price, current_price}}`` with Decimals.
        """
        if not self._connected or self._client is None:
            raise RuntimeError("reconcile_positions() called before connect()")
        out: Dict[str, dict] = {}
        for pos in self._client.list_positions():
            ticker = str(getattr(pos, "symbol"))
            out[ticker] = {
                "qty": Decimal(str(getattr(pos, "qty"))),
                "avg_entry_price": Decimal(str(getattr(pos, "avg_entry_price"))),
                "current_price": Decimal(str(getattr(pos, "current_price"))),
            }
        logger.info("Reconciled %d broker positions.", len(out))
        return out

    @property
    def is_paper(self) -> bool:
        return self._paper

    # ----------------------------------------------------------------- internals

    def _poll_for_fill(self, broker_order_id: str) -> object:
        """Poll the broker for a terminal/filled state, up to the configured budget."""
        order = self._client.get_order(broker_order_id)  # type: ignore[union-attr]
        for attempt in range(self._fill_poll_attempts):
            status = _status_str(getattr(order, "status", ""))
            filled_qty = Decimal(str(getattr(order, "filled_qty", "0") or "0"))
            if status in _TERMINAL_STATUSES or filled_qty > 0:
                return order
            if attempt < self._fill_poll_attempts - 1:
                self._sleep(self._poll_interval)
                order = self._client.get_order(broker_order_id)  # type: ignore[union-attr]
        return order

    def _maybe_emit_fill(self, order: OrderEvent, broker_order: object, broker_id: str) -> None:
        """Emit a FillEvent for the quantity the broker actually filled, if any."""
        filled_qty = Decimal(str(getattr(broker_order, "filled_qty", "0") or "0"))
        if filled_qty <= 0:
            logger.warning(
                "Order %s not filled yet (status=%s) — no fill booked; reconcile next run.",
                broker_id, getattr(broker_order, "status", "?"),
            )
            return
        avg_price = getattr(broker_order, "filled_avg_price", None)
        if avg_price in (None, ""):
            logger.error("Order %s filled qty %s but no avg price — skipping fill.",
                         broker_id, filled_qty)
            return

        fill = FillEvent(
            symbol=order.symbol,
            side=order.side,
            quantity=filled_qty,
            fill_price=Decimal(str(avg_price)),
            commission=Decimal("0"),     # Alpaca is commission-free
            slippage=Decimal("0"),       # real fill already includes market impact
            order_id=order.event_id,
            broker_order_id=broker_id,
            timestamp=utc_now(),
            is_paper=self._paper,
        )
        logger.info("Fill booked: %s %s x%s @ %s (broker %s)",
                    order.side.value, order.symbol.ticker, filled_qty, fill.fill_price, broker_id)
        self._emit_fill(fill)

    def _build_sdk_client(self) -> BrokerClient:
        """
        Build the real alpaca-py-backed client. Imported lazily so the module and
        test suite load without the SDK. Verified against paper keys, not in CI.
        """
        import os
        key = self._api_key if self._api_key is not None else os.getenv("ALPACA_API_KEY")
        secret = self._api_secret if self._api_secret is not None else os.getenv("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise ConnectionError(
                "Alpaca credentials missing. Set ALPACA_API_KEY / ALPACA_SECRET_KEY "
                "or inject a broker_client for tests."
            )
        try:  # pragma: no cover - requires alpaca-py
            from alpaca.trading.client import TradingClient
            from alpaca.trading.enums import OrderSide as AlSide, TimeInForce as AlTIF
            from alpaca.trading.requests import MarketOrderRequest
        except ImportError as exc:  # pragma: no cover
            raise ConnectionError(
                "alpaca-py is required for live execution. `pip install alpaca-py`."
            ) from exc

        return _AlpacaClientAdapter(  # pragma: no cover - live path
            TradingClient(key, secret, paper=self._paper),
            MarketOrderRequest, AlSide, AlTIF,
        )


class _AlpacaClientAdapter:  # pragma: no cover - thin live wrapper, verified in paper
    """Adapts alpaca-py's TradingClient to the BrokerClient seam."""

    def __init__(self, trading_client, market_order_request, al_side, al_tif):
        self._tc = trading_client
        self._MarketOrderRequest = market_order_request
        self._AlSide = al_side
        self._AlTIF = al_tif

    def find_order_by_client_id(self, client_order_id):
        try:
            return self._tc.get_order_by_client_id(client_order_id)
        except Exception:  # noqa: BLE001 — not-found surfaces as an error; treat as absent
            return None

    def submit_market_order(self, symbol, qty, side, client_order_id, time_in_force):
        req = self._MarketOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=self._AlSide(side),
            time_in_force=self._AlTIF(time_in_force),
            client_order_id=client_order_id,
        )
        return self._tc.submit_order(req)

    def get_order(self, broker_order_id):
        return self._tc.get_order_by_id(broker_order_id)

    def cancel_order(self, broker_order_id):
        self._tc.cancel_order_by_id(broker_order_id)
        return True

    def cancel_open_orders(self):
        self._tc.cancel_orders()

    def get_account_equity(self):
        return Decimal(str(self._tc.get_account().equity))

    def list_positions(self):
        return self._tc.get_all_positions()
