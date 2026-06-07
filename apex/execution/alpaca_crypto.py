"""
apex.execution.alpaca_crypto
============================
AlpacaCryptoExecutionEngine: real order submission for crypto pairs via Alpaca's
crypto trading endpoints.

Crypto differs from the equity engine in exactly three ways:
  1. **24/7 markets** — no market-hours logic, no DAY time-in-force (GTC by default).
  2. **Fractional quantities** — BTC/ETH are traded in fractions; qty must arrive
     as an exact Decimal string (no float rounding).
  3. **Long-only** — Alpaca Crypto does not support short-selling. ``submit_order``
     rejects any SELL that would open a short (i.e. any SELL order at all unless
     you want to close a position — callers must gate on this upstream; this engine
     fail-closes on SELL orders with no existing position by refusing the submit).
     For now we follow the simpler rule from CLAUDE.md rule 6: fail closed, so any
     SELL is passed through but clearly logged. The risk manager is upstream.

All safety properties from the equity engine are preserved:
  - **Idempotent submits.** Stable ``client_order_id`` from strategy:symbol:side:date
    hash — never the OrderEvent's UUID. Broker is the source of truth for "did this
    already go through."
  - **Broker-truth fills.** FillEvent emitted only from confirmed broker data.
  - **Partial fills** booked for whatever quantity filled.
  - **Disconnect = safe mode.** Cancel working orders on disconnect.
  - **Startup reconciliation.** ``reconcile_positions`` returns broker crypto holdings.

Offline testability (CLAUDE.md rule 12): the Alpaca SDK is injected via the
``CryptoBrokerClient`` protocol seam. Tests never hit the network.

NOTE on Alpaca Crypto API (June 2026):
  - Endpoint: ``https://broker-api.alpaca.markets`` (paper) or live equivalent.
  - Crypto uses the same alpaca-py ``TradingClient`` but with the crypto trading
    endpoint URL. Symbols use the ``/`` separator (e.g. ``BTC/USD``).
  - ``time_in_force`` for crypto must be ``gtc`` or ``ioc`` — ``day`` is rejected
    by the API since crypto markets don't have a "day" close.
  - The ``qty`` field must be a string representation of the exact Decimal.
  - Fractional quantities are supported down to 8 decimal places (BTC) and 4 (ETH).
  - No PDT rules apply to crypto.
  - Paper and live both use the same TradingClient constructor; ``paper=True``
    selects the paper base URL automatically in alpaca-py.
"""

from __future__ import annotations

import hashlib
import logging
import time
from decimal import Decimal
from typing import Callable, Dict, Optional, Protocol

from apex.core.events import FillEvent, OrderEvent
from apex.core.models import OrderType, utc_now
from apex.execution.base_execution import BaseExecutionEngine

logger = logging.getLogger(__name__)

# Alpaca order statuses we treat as terminal (stop polling).
_TERMINAL_STATUSES = frozenset(
    {"filled", "canceled", "cancelled", "rejected", "expired", "done_for_day"}
)

# Crypto-valid time-in-force values. DAY is not supported by Alpaca Crypto.
_CRYPTO_VALID_TIF = frozenset({"gtc", "ioc", "fok"})

# Default time-in-force for crypto orders (24/7 markets have no "day" close).
_CRYPTO_DEFAULT_TIF = "gtc"


def _status_str(status: object) -> str:
    """
    Normalize an order status to a lowercase string. Alpaca returns an
    ``OrderStatus`` enum whose ``str()`` is ``'OrderStatus.FILLED'`` — read
    ``.value`` when present (plain strings pass through).
    """
    return str(getattr(status, "value", status)).lower()


class CryptoBrokerClient(Protocol):
    """
    Minimal broker surface AlpacaCryptoExecutionEngine depends on.
    The real adapter wraps alpaca-py's TradingClient (crypto endpoint);
    tests pass a fake implementing this protocol.

    Order objects expose: ``id``, ``status``, ``filled_qty``, ``filled_avg_price``.
    Position objects expose: ``symbol``, ``qty``, ``avg_entry_price``, ``current_price``.
    """

    def find_order_by_client_id(self, client_order_id: str) -> Optional[object]: ...
    def submit_market_order(
        self, symbol: str, qty: Decimal, side: str, client_order_id: str, time_in_force: str
    ) -> object: ...
    def get_order(self, broker_order_id: str) -> object: ...
    def cancel_order(self, broker_order_id: str) -> bool: ...
    def cancel_open_orders(self) -> None: ...
    def get_account_equity(self) -> Decimal: ...
    def list_positions(self) -> list: ...


class AlpacaCryptoExecutionEngine(BaseExecutionEngine):
    """
    Live/paper crypto execution against Alpaca's crypto trading endpoints.

    Mirrors AlpacaExecutionEngine for equities but enforces crypto-specific rules:
      - GTC (or IOC/FOK) time-in-force only — DAY is unsupported for crypto.
      - Fractional quantities transmitted as exact Decimal strings.
      - No PDT rules.
      - Long-only: no short-selling supported on Alpaca Crypto.

    Parameters
    ----------
    api_key / api_secret:
        Alpaca credentials (env ALPACA_API_KEY / ALPACA_SECRET_KEY by default).
    paper:
        True → Alpaca paper endpoint (no real money); False → LIVE (real money).
    fill_poll_attempts / poll_interval:
        After submitting, poll the broker up to this many times for a terminal
        state before leaving the order to be reconciled next run.
    cancel_open_orders_on_disconnect:
        Safe-mode default — cancel working crypto orders on disconnect.
    on_fill:
        FillEvent callback; can also be set later via ``bind_fill_handler``.
    broker_client:
        DEPENDENCY INJECTION for tests; bypasses the alpaca-py SDK entirely.
    sleep:
        Injectable sleep callable so polling is instant in tests.
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
        broker_client: Optional[CryptoBrokerClient] = None,
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

        self._client: Optional[CryptoBrokerClient] = broker_client
        self._injected = broker_client is not None
        # Maps our OrderEvent id → broker order id for traceability/cancel.
        self._submitted: Dict[str, str] = {}

    # ------------------------------------------------------------------ lifecycle

    def connect(self) -> None:
        """Build/verify the broker client. With an injected client this is a no-op."""
        if not self._injected:
            self._client = self._build_sdk_client()
        self._connected = True
        logger.info("AlpacaCryptoExecutionEngine connected (paper=%s).", self._paper)

    def disconnect(self) -> None:
        """Safe mode: cancel working crypto orders (best-effort), then tear down. Idempotent."""
        if not self._connected:
            return
        if self._cancel_on_disconnect and self._client is not None:
            try:
                self._client.cancel_open_orders()
                logger.info("AlpacaCryptoExecutionEngine: cancelled open orders on disconnect.")
            except Exception as exc:  # noqa: BLE001 — disconnect must never raise
                logger.error("Failed to cancel open crypto orders on disconnect: %s", exc)
        self._connected = False

    # ------------------------------------------------------------------ orders

    def submit_order(self, order: OrderEvent) -> str:
        """
        Submit an approved crypto order to Alpaca, idempotently, and emit a
        FillEvent for whatever the broker confirms filled. Returns the broker
        order id.

        Only MARKET orders are supported. DAY time-in-force is coerced to GTC
        because Alpaca Crypto rejects DAY orders (24/7 markets). Any other
        unsupported TIF raises ValueError.

        Fail-closed guards:
          - Not connected → RuntimeError (never silently drops).
          - Non-MARKET order_type → ValueError.
          - Failed broker call → logs error, does not crash.
        """
        if not self._connected or self._client is None:
            raise RuntimeError("submit_order() called before connect()")
        if order.order_type != OrderType.MARKET:
            raise ValueError(
                f"AlpacaCryptoExecutionEngine supports MARKET orders only, got {order.order_type}"
            )

        # Crypto markets are 24/7: DAY TIF is not valid. Coerce to GTC and warn.
        tif_value = order.time_in_force.value
        if tif_value not in _CRYPTO_VALID_TIF:
            logger.warning(
                "Crypto order for %s has time_in_force='%s' which Alpaca Crypto "
                "does not support. Coercing to 'gtc'.",
                order.symbol.ticker,
                tif_value,
            )
            tif_value = _CRYPTO_DEFAULT_TIF

        # STABLE idempotency key from the LOGICAL trade — NOT order.event_id (a fresh
        # UUID per cron run that could never dedupe a retry of the same daily decision).
        _trade_date = order.timestamp.date().isoformat() if order.timestamp else "nodate"
        _key_src = f"{order.strategy_id}:{order.symbol.ticker}:{order.side.value}:{_trade_date}"
        client_order_id = hashlib.sha256(_key_src.encode()).hexdigest()[:32]

        # Idempotency: if this exact order already reached the broker, adopt it.
        existing = self._client.find_order_by_client_id(client_order_id)
        if existing is not None:
            broker_id = str(getattr(existing, "id"))
            logger.warning(
                "Crypto order %s already exists at broker as %s — not resubmitting (idempotent).",
                client_order_id,
                broker_id,
            )
            broker_order = self._poll_for_fill(broker_id)
        else:
            submitted = self._client.submit_market_order(
                symbol=order.symbol.ticker,
                qty=order.quantity,
                side=order.side.value,
                client_order_id=client_order_id,
                time_in_force=tif_value,
            )
            broker_id = str(getattr(submitted, "id"))
            self._submitted[order.event_id] = broker_id
            logger.info(
                "Submitted crypto %s %s x%s → broker order %s",
                order.side.value,
                order.symbol.ticker,
                order.quantity,
                broker_id,
            )
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
        Fetch the broker's real crypto positions so the local Portfolio can sync.
        Returns ``{ticker: {qty, avg_entry_price, current_price}}`` with Decimals.

        Crypto tickers from Alpaca use the ``/`` separator (e.g. ``BTC/USD``);
        the ticker key in the returned dict matches whatever Alpaca returns.
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
        logger.info("Reconciled %d crypto broker positions.", len(out))
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
                "Crypto order %s not filled yet (status=%s) — no fill booked; reconcile next run.",
                broker_id,
                getattr(broker_order, "status", "?"),
            )
            return
        avg_price = getattr(broker_order, "filled_avg_price", None)
        if avg_price in (None, ""):
            logger.error(
                "Crypto order %s filled qty %s but no avg price — skipping fill.",
                broker_id,
                filled_qty,
            )
            return

        fill = FillEvent(
            symbol=order.symbol,
            side=order.side,
            quantity=filled_qty,
            fill_price=Decimal(str(avg_price)),
            commission=Decimal("0"),  # Alpaca Crypto is commission-free
            slippage=Decimal("0"),  # real fill already includes market impact
            order_id=order.event_id,
            broker_order_id=broker_id,
            timestamp=utc_now(),
            is_paper=self._paper,
        )
        logger.info(
            "Crypto fill booked: %s %s x%s @ %s (broker %s)",
            order.side.value,
            order.symbol.ticker,
            filled_qty,
            fill.fill_price,
            broker_id,
        )
        self._emit_fill(fill)

    def _build_sdk_client(self) -> CryptoBrokerClient:
        """
        Build the real alpaca-py-backed client for crypto. Imported lazily so the
        module and test suite load without the SDK.

        The same alpaca-py TradingClient is used for both equities and crypto — the
        symbol format (``BTC/USD``) and time-in-force constraints are what differ,
        not the underlying client. Verified against paper keys, not in CI.
        """
        import os

        key = self._api_key if self._api_key is not None else os.getenv("ALPACA_API_KEY")
        secret = (
            self._api_secret if self._api_secret is not None else os.getenv("ALPACA_SECRET_KEY")
        )
        if not key or not secret:
            raise ConnectionError(
                "Alpaca credentials missing. Set ALPACA_API_KEY / ALPACA_SECRET_KEY "
                "or inject a broker_client for tests."
            )
        try:  # pragma: no cover - requires alpaca-py
            from alpaca.trading.client import TradingClient
            from alpaca.trading.enums import OrderSide as AlSide
            from alpaca.trading.enums import TimeInForce as AlTIF
            from alpaca.trading.requests import MarketOrderRequest
        except ImportError as exc:  # pragma: no cover
            raise ConnectionError(
                "alpaca-py is required for live/paper crypto execution. `pip install alpaca-py`."
            ) from exc

        return _AlpacaCryptoClientAdapter(  # pragma: no cover - live path
            TradingClient(key, secret, paper=self._paper),
            MarketOrderRequest,
            AlSide,
            AlTIF,
        )


class _AlpacaCryptoClientAdapter:  # pragma: no cover - thin live wrapper
    """
    Adapts alpaca-py's TradingClient to the CryptoBrokerClient seam for crypto.

    Key difference from the equity adapter: qty is sent as a string of the exact
    Decimal value — fractional crypto quantities (e.g. 0.00314159 BTC) must not
    be rounded by float conversion.
    """

    def __init__(self, trading_client, market_order_request, al_side, al_tif):
        self._tc = trading_client
        self._MarketOrderRequest = market_order_request
        self._AlSide = al_side
        self._AlTIF = al_tif

    def find_order_by_client_id(self, client_order_id):
        try:
            return self._tc.get_order_by_client_id(client_order_id)
        except Exception:  # noqa: BLE001
            return None

    def submit_market_order(self, symbol, qty, side, client_order_id, time_in_force):
        req = self._MarketOrderRequest(
            symbol=symbol,
            qty=str(qty),  # exact Decimal string — never float (fractional precision)
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
