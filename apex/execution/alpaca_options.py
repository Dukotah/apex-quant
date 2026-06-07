"""
apex.execution.alpaca_options
=============================
AlpacaOptionsExecutionEngine: submit option orders (single-leg AND multi-leg
verticals) to Alpaca, idempotently and fail-closed.

Built to the same safety standard as ``apex.execution.alpaca``:

  - **Idempotent submits.** A STABLE ``client_order_id`` is derived by hashing the
    LOGICAL order — every leg's OCC symbol + right + ratio, plus quantity and the
    submit date — NOT a fresh UUID. A retried cron run that re-evaluates the same
    decision produces the same id, so the broker (asked first) dedupes it.

  - **Fail closed / never crash on submit.** Any error during submission is caught,
    logged, and surfaced as an ``OptionSubmitResult`` with ``ok=False``; the engine
    never raises out of ``submit_order`` and never books a fill it can't confirm.

  - **Broker-truth fills.** A ``FillEvent`` is emitted ONLY from what the broker
    reports filled. If nothing filled, no fill is booked.

Offline testability: the alpaca-py options/multi-leg client is wrapped behind a
tiny ``OptionBrokerClient`` Protocol injected via the constructor. Tests pass a
fake; the real adapter is a thin, lazily-imported wrapper verified in paper.

DEVIATION FROM BaseExecutionEngine (documented per spec): this engine does NOT
subclass ``BaseExecutionEngine``. That ABC's ``submit_order(self, order: OrderEvent)``
is single-symbol/single-leg and returns ``str``; an ``OptionOrder`` is multi-leg
and submission can legitimately fail-closed without raising, so a richer return
(``OptionSubmitResult``) is needed. We DO reuse ``FillEvent`` for fill reporting
and the same ``on_fill`` callback shape, so downstream portfolio/bus wiring is
unchanged. See the FINAL REPORT for the integration this would need.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Protocol

from apex.core.events import FillEvent
from apex.core.models import OrderSide, utc_now
from apex.core.option import OptionOrder, OptionRight

logger = logging.getLogger(__name__)

# Order statuses we treat as terminal (no point polling further).
_TERMINAL_STATUSES = frozenset(
    {"filled", "canceled", "cancelled", "rejected", "expired", "done_for_day"}
)


def _status_str(status: object) -> str:
    """Normalize a broker status to a lowercase string (handles enum-with-.value)."""
    return str(getattr(status, "value", status)).lower()


@dataclass(frozen=True)
class OptionSubmitResult:
    """
    Outcome of submitting an OptionOrder. Fail-closed: ``ok=False`` carries the
    reason and books no fill. ``fills`` are the FillEvents that were emitted (one
    per filled leg the broker confirmed).
    """

    ok: bool
    client_order_id: str
    broker_order_id: Optional[str] = None
    error: Optional[str] = None
    fills: tuple[FillEvent, ...] = field(default_factory=tuple)


class OptionBrokerClient(Protocol):
    """
    The minimal broker surface AlpacaOptionsExecutionEngine depends on. The real
    adapter wraps alpaca-py's options/multi-leg TradingClient; tests pass a fake.

    ``submit_option_order`` takes the whole structure (legs + qty + optional net
    limit) and returns a broker order object exposing ``id``, ``status``, and a
    ``legs`` collection — each leg exposing ``symbol`` (OCC), ``side`` ('buy'/'sell'),
    ``filled_qty``, ``filled_avg_price``.
    """

    def find_order_by_client_id(self, client_order_id: str) -> Optional[object]: ...
    def submit_option_order(
        self,
        legs: List[dict],
        qty: int,
        client_order_id: str,
        time_in_force: str,
        limit_price: Optional[Decimal],
    ) -> object: ...
    def get_order(self, broker_order_id: str) -> object: ...
    def cancel_order(self, broker_order_id: str) -> bool: ...
    def cancel_open_orders(self) -> None: ...


class AlpacaOptionsExecutionEngine:
    """
    Live/paper option execution against Alpaca. Defined-risk emphasis: prefer
    multi-leg structures (e.g. verticals) submitted as one order so the broker
    legs them atomically.

    Parameters
    ----------
    api_key / api_secret:
        Alpaca credentials (env ALPACA_API_KEY / ALPACA_SECRET_KEY by default).
    paper:
        True → Alpaca paper endpoint; False → LIVE (real money).
    fill_poll_attempts / poll_interval:
        After submitting, poll up to this many times for a terminal state.
    cancel_open_orders_on_disconnect:
        Safe-mode default — cancel working orders on disconnect.
    on_fill:
        callback to publish FillEvents back to the bus (same shape as equities).
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
        broker_client: Optional[OptionBrokerClient] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._paper = paper
        self._fill_poll_attempts = max(1, fill_poll_attempts)
        self._poll_interval = poll_interval
        self._cancel_on_disconnect = cancel_open_orders_on_disconnect
        self._on_fill = on_fill
        self._sleep = sleep

        self._client: Optional[OptionBrokerClient] = broker_client
        self._injected = broker_client is not None
        self._connected: bool = False
        self._submitted: Dict[str, str] = {}  # client_order_id → broker order id

    # ------------------------------------------------------------------ lifecycle

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_paper(self) -> bool:
        return self._paper

    def bind_fill_handler(self, handler: Callable[[FillEvent], None]) -> None:
        """Engine wiring: where to send FillEvents (same contract as equities)."""
        self._on_fill = handler

    def connect(self) -> None:
        """Build/verify the broker client. With an injected client this is a no-op."""
        if not self._injected:
            self._client = self._build_sdk_client()
        self._connected = True
        logger.info("AlpacaOptionsExecutionEngine connected (paper=%s).", self._paper)

    def disconnect(self) -> None:
        """Safe mode: cancel working orders (best-effort), then tear down. Idempotent."""
        if not self._connected:
            return
        if self._cancel_on_disconnect and self._client is not None:
            try:
                self._client.cancel_open_orders()
                logger.info("AlpacaOptionsExecutionEngine: cancelled open orders on disconnect.")
            except Exception as exc:  # noqa: BLE001 — disconnect must never raise
                logger.error("Failed to cancel open option orders on disconnect: %s", exc)
        self._connected = False

    def __enter__(self) -> "AlpacaOptionsExecutionEngine":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # ------------------------------------------------------------------ orders

    def submit_order(self, order: OptionOrder) -> OptionSubmitResult:
        """
        Submit an OptionOrder (single- or multi-leg), idempotently, and emit a
        FillEvent for each leg the broker confirms filled.

        FAIL CLOSED: never raises on a submit error — returns ``ok=False`` with the
        reason and books no fill. Returns an ``OptionSubmitResult``.
        """
        client_order_id = self.client_order_id(order)

        if not self._connected or self._client is None:
            return OptionSubmitResult(
                ok=False,
                client_order_id=client_order_id,
                error="submit_order() called before connect()",
            )

        try:
            broker_order, broker_id = self._submit_idempotent(order, client_order_id)
        except Exception as exc:  # noqa: BLE001 — submission must never crash the engine
            logger.error(
                "Option order %s submission failed (fail-closed): %s", client_order_id, exc
            )
            return OptionSubmitResult(
                ok=False,
                client_order_id=client_order_id,
                error=str(exc),
            )

        self._submitted[client_order_id] = broker_id
        fills = self._emit_leg_fills(broker_order, broker_id)
        return OptionSubmitResult(
            ok=True,
            client_order_id=client_order_id,
            broker_order_id=broker_id,
            fills=tuple(fills),
        )

    def cancel_order(self, broker_order_id: str) -> bool:
        if not self._connected or self._client is None:
            return False
        try:
            return bool(self._client.cancel_order(broker_order_id))
        except Exception as exc:  # noqa: BLE001
            logger.error("cancel_order(%s) failed: %s", broker_order_id, exc)
            return False

    @staticmethod
    def client_order_id(order: OptionOrder) -> str:
        """
        STABLE idempotency key from the LOGICAL order: every leg's OCC symbol +
        right + ratio, the spread quantity, the net limit, and today's date. Same
        logical order on a retried run → same id → broker dedupes it.
        """
        legs_src = "|".join(
            f"{leg.contract.occ_symbol}:{leg.right.value}:{leg.ratio}" for leg in order.legs
        )
        limit_src = "mkt" if order.limit_price is None else str(order.limit_price)
        trade_date = utc_now().date().isoformat()
        key_src = f"{legs_src};q={order.quantity};lim={limit_src};{trade_date}"
        return hashlib.sha256(key_src.encode()).hexdigest()[:32]

    # ----------------------------------------------------------------- internals

    def _submit_idempotent(self, order: OptionOrder, client_order_id: str) -> tuple[object, str]:
        """Adopt an existing broker order with this id, or submit a new one. Returns (order, id)."""
        assert self._client is not None  # guarded by caller
        existing = self._client.find_order_by_client_id(client_order_id)
        if existing is not None:
            broker_id = str(getattr(existing, "id"))
            logger.warning(
                "Option order %s already exists at broker as %s — not resubmitting (idempotent).",
                client_order_id,
                broker_id,
            )
            return self._poll_for_fill(broker_id), broker_id

        legs_payload = [
            {
                "symbol": leg.contract.occ_symbol,
                "side": leg.right.value,  # 'buy' / 'sell'
                "ratio_qty": leg.ratio,
            }
            for leg in order.legs
        ]
        submitted = self._client.submit_option_order(
            legs=legs_payload,
            qty=order.quantity,
            client_order_id=client_order_id,
            time_in_force="day",
            limit_price=order.limit_price,
        )
        broker_id = str(getattr(submitted, "id"))
        logger.info(
            "Submitted option order %s (%d leg(s) x%d) → broker order %s",
            client_order_id,
            len(order.legs),
            order.quantity,
            broker_id,
        )
        return self._poll_for_fill(broker_id), broker_id

    def _poll_for_fill(self, broker_order_id: str) -> object:
        """Poll the broker for a terminal/filled state, up to the configured budget."""
        assert self._client is not None
        order = self._client.get_order(broker_order_id)
        for attempt in range(self._fill_poll_attempts):
            status = _status_str(getattr(order, "status", ""))
            if status in _TERMINAL_STATUSES or self._any_leg_filled(order):
                return order
            if attempt < self._fill_poll_attempts - 1:
                self._sleep(self._poll_interval)
                order = self._client.get_order(broker_order_id)
        return order

    @staticmethod
    def _any_leg_filled(broker_order: object) -> bool:
        for leg in getattr(broker_order, "legs", []) or []:
            if Decimal(str(getattr(leg, "filled_qty", "0") or "0")) > 0:
                return True
        return False

    def _emit_leg_fills(self, broker_order: object, broker_id: str) -> List[FillEvent]:
        """Emit one FillEvent per leg the broker confirms filled (broker-truth)."""
        fills: List[FillEvent] = []
        legs = getattr(broker_order, "legs", None)
        if not legs:
            logger.warning(
                "Option order %s reported no legs (status=%s) — no fill booked.",
                broker_id,
                getattr(broker_order, "status", "?"),
            )
            return fills

        for leg in legs:
            filled_qty = Decimal(str(getattr(leg, "filled_qty", "0") or "0"))
            if filled_qty <= 0:
                continue
            avg_price = getattr(leg, "filled_avg_price", None)
            if avg_price in (None, ""):
                logger.error(
                    "Leg %s of order %s filled qty %s but no avg price — skipping leg fill.",
                    getattr(leg, "symbol", "?"),
                    broker_id,
                    filled_qty,
                )
                continue
            side_str = _status_str(getattr(leg, "side", ""))
            side = OrderSide.BUY if side_str == OptionRight.BUY.value else OrderSide.SELL
            fill = self._make_fill(leg, filled_qty, Decimal(str(avg_price)), side, broker_id)
            fills.append(fill)
            self._emit_fill(fill)
        if not fills:
            logger.warning(
                "Option order %s not filled yet (status=%s) — no fill booked; reconcile next run.",
                broker_id,
                getattr(broker_order, "status", "?"),
            )
        return fills

    def _make_fill(
        self,
        leg: object,
        filled_qty: Decimal,
        avg_price: Decimal,
        side: OrderSide,
        broker_id: str,
    ) -> FillEvent:
        from apex.core.models import AssetClass, Symbol

        # Carry the contract identity through the FillEvent's Symbol.ticker as the OCC
        # symbol — the only string slot available without changing FillEvent. Multiplier
        # 100 matches standard equity options so downstream P&L sizing is correct.
        occ = str(getattr(leg, "symbol", ""))
        symbol = Symbol(
            ticker=occ,
            asset_class=AssetClass.OPTION,
            contract_multiplier=Decimal("100"),
        )
        fill = FillEvent(
            symbol=symbol,
            side=side,
            quantity=filled_qty,
            fill_price=avg_price,
            commission=Decimal("0"),
            slippage=Decimal("0"),
            order_id="",
            broker_order_id=broker_id,
            timestamp=utc_now(),
            is_paper=self._paper,
        )
        logger.info(
            "Option fill booked: %s %s x%s @ %s (broker %s)",
            side.value,
            occ,
            filled_qty,
            avg_price,
            broker_id,
        )
        return fill

    def _emit_fill(self, fill: FillEvent) -> None:
        if self._on_fill is not None:
            self._on_fill(fill)

    def _build_sdk_client(self) -> OptionBrokerClient:
        """
        Build the real alpaca-py options client. Imported lazily so the module and
        test suite load without the SDK. Verified against paper keys, not in CI.
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
            from alpaca.trading.enums import OrderClass
            from alpaca.trading.enums import OrderSide as AlSide
            from alpaca.trading.enums import TimeInForce as AlTIF
            from alpaca.trading.requests import (
                LimitOrderRequest,
                MarketOrderRequest,
                OptionLegRequest,
            )
        except ImportError as exc:  # pragma: no cover
            raise ConnectionError(
                "alpaca-py is required for live options execution. `pip install alpaca-py`."
            ) from exc

        return _AlpacaOptionsClientAdapter(  # pragma: no cover - live path
            TradingClient(key, secret, paper=self._paper),
            MarketOrderRequest,
            LimitOrderRequest,
            OptionLegRequest,
            OrderClass,
            AlSide,
            AlTIF,
        )


class _AlpacaOptionsClientAdapter:  # pragma: no cover - thin live wrapper, verified in paper
    """Adapts alpaca-py's TradingClient (options/multi-leg) to the OptionBrokerClient seam."""

    def __init__(
        self,
        trading_client,
        market_req,
        limit_req,
        leg_req,
        order_class,
        al_side,
        al_tif,
    ):
        self._tc = trading_client
        self._MarketReq = market_req
        self._LimitReq = limit_req
        self._LegReq = leg_req
        self._OrderClass = order_class
        self._AlSide = al_side
        self._AlTIF = al_tif

    def find_order_by_client_id(self, client_order_id):
        try:
            return self._tc.get_order_by_client_id(client_order_id)
        except Exception:  # noqa: BLE001 — not-found surfaces as an error; treat as absent
            return None

    def submit_option_order(self, legs, qty, client_order_id, time_in_force, limit_price):
        leg_reqs = [
            self._LegReq(
                symbol=leg["symbol"],
                side=self._AlSide(leg["side"]),
                ratio_qty=leg["ratio_qty"],
            )
            for leg in legs
        ]
        order_class = self._OrderClass.MLEG if len(leg_reqs) > 1 else self._OrderClass.SIMPLE
        common = {
            "qty": qty,
            "order_class": order_class,
            "legs": leg_reqs,
            "time_in_force": self._AlTIF(time_in_force),
            "client_order_id": client_order_id,
        }
        if limit_price is not None:
            req = self._LimitReq(limit_price=float(limit_price), **common)
        else:
            req = self._MarketReq(**common)
        return self._tc.submit_order(req)

    def get_order(self, broker_order_id):
        return self._tc.get_order_by_id(broker_order_id)

    def cancel_order(self, broker_order_id):
        self._tc.cancel_order_by_id(broker_order_id)
        return True

    def cancel_open_orders(self):
        self._tc.cancel_orders()
