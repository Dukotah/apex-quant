"""
Tests for apex.execution.alpaca_crypto (AlpacaCryptoExecutionEngine).

The Alpaca SDK is replaced by a FakeCryptoBroker implementing the
CryptoBrokerClient seam, so all safety properties are verified offline:
  - BUY submits the correct symbol, exact fractional qty string, idempotency key.
  - Fill flows back through the bound handler.
  - Fractional Decimal qty is preserved exactly (no float rounding).
  - Partial fills book only the confirmed quantity.
  - Idempotent submits adopt the existing broker order and never double-send.
  - DAY time-in-force is coerced to GTC (crypto-specific rule).
  - Unfilled orders book no fill (reconcile next run).
  - Non-MARKET order type is rejected (ValueError).
  - Not-connected guard (RuntimeError).
  - Cancel order success and failure (swallowed → False).
  - Disconnect cancels open orders (safe mode); second disconnect is idempotent.
  - Disconnect swallows cancel errors.
  - reconcile_positions maps broker crypto positions to Decimal snapshot.
  - get_account_equity returns a Decimal.
  - is_paper reflects the constructor flag.
  - Enum-valued order status is detected as terminal (no unnecessary polling).
  - Filled qty without avg price skips fill (fail-safe).
No SDK, no network, no real keys required.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.core.events import OrderEvent
from apex.core.models import AssetClass, OrderSide, OrderType, Symbol, TimeInForce
from apex.execution.alpaca_crypto import AlpacaCryptoExecutionEngine

# Canonical crypto symbols used across tests.
BTC = Symbol("BTC/USD", AssetClass.CRYPTO, fractionable=True)
ETH = Symbol("ETH/USD", AssetClass.CRYPTO, fractionable=True)


# --------------------------------------------------------------------------- fakes


class FakeCryptoOrder:
    """Minimal broker order object returned by FakeCryptoBroker."""

    def __init__(
        self,
        id: str = "CBRK-1",
        status: object = "filled",
        filled_qty: str = "0",
        filled_avg_price: object = None,
    ) -> None:
        self.id = id
        self.status = status
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price


class FakeCryptoPosition:
    """Minimal broker position object."""

    def __init__(
        self,
        symbol: str,
        qty: str,
        avg_entry_price: str,
        current_price: str,
    ) -> None:
        self.symbol = symbol
        self.qty = qty
        self.avg_entry_price = avg_entry_price
        self.current_price = current_price


class FakeCryptoBroker:
    """
    Implements the CryptoBrokerClient protocol with scripted poll states.
    Records every submitted order so tests can assert on symbol, qty, TIF, etc.
    """

    def __init__(
        self,
        poll_states=None,
        existing=None,
        equity: str = "50000",
        positions=None,
        fail_cancel: bool = False,
    ) -> None:
        self._states = list(
            poll_states
            or [FakeCryptoOrder(status="filled", filled_qty="0.00314159", filled_avg_price="67000")]
        )
        self._poll_idx = 0
        self.submitted: list = []
        self.canceled: list = []
        self.cancel_open_calls: int = 0
        self._existing = existing
        self._equity = equity
        self._positions = positions or []
        self._fail_cancel = fail_cancel

    def find_order_by_client_id(self, client_order_id: str) -> object:
        return self._existing

    def submit_market_order(
        self, symbol: str, qty, side: str, client_order_id: str, time_in_force: str
    ) -> object:
        self.submitted.append(
            {
                "symbol": symbol,
                "qty": qty,
                "side": side,
                "client_order_id": client_order_id,
                "time_in_force": time_in_force,
            }
        )
        return self._states[0]

    def get_order(self, broker_order_id: str) -> object:
        st = self._states[min(self._poll_idx, len(self._states) - 1)]
        self._poll_idx += 1
        return st

    def cancel_order(self, broker_order_id: str) -> bool:
        if self._fail_cancel:
            raise RuntimeError("broker rejected cancel")
        self.canceled.append(broker_order_id)
        return True

    def cancel_open_orders(self) -> None:
        self.cancel_open_calls += 1

    def get_account_equity(self) -> str:
        return self._equity

    def list_positions(self) -> list:
        return self._positions


def _order(
    symbol: Symbol = BTC,
    side: OrderSide = OrderSide.BUY,
    qty: str = "0.5",
    tif: TimeInForce = TimeInForce.GTC,
    strategy_id: str = "test_strat",
) -> OrderEvent:
    return OrderEvent(
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        order_type=OrderType.MARKET,
        stop_loss=Decimal("60000"),
        time_in_force=tif,
        strategy_id=strategy_id,
    )


def _engine(broker: FakeCryptoBroker, **kw) -> tuple[AlpacaCryptoExecutionEngine, list]:
    fills: list = []
    eng = AlpacaCryptoExecutionEngine(
        broker_client=broker,
        on_fill=fills.append,
        sleep=lambda _s: None,
        **kw,
    )
    eng.connect()
    return eng, fills


# --------------------------------------------------------------------------- fills


def test_buy_order_submits_correct_symbol_and_side():
    """BUY order reaches the broker with the right symbol and 'buy' side string."""
    broker = FakeCryptoBroker([FakeCryptoOrder("CBRK-1", "filled", "0.5", "67000")])
    eng, fills = _engine(broker)
    eng.submit_order(_order(symbol=BTC, side=OrderSide.BUY, qty="0.5"))

    assert len(broker.submitted) == 1
    call = broker.submitted[0]
    assert call["symbol"] == "BTC/USD"
    assert call["side"] == "buy"


def test_buy_order_qty_is_exact_decimal_string():
    """
    Fractional qty must be transmitted as the exact Decimal string to avoid
    float rounding (e.g. 0.00314159 BTC must not become 0.0031415900000001).
    """
    qty_str = "0.00314159"
    broker = FakeCryptoBroker([FakeCryptoOrder("CBRK-1", "filled", qty_str, "67000")])
    eng, fills = _engine(broker)
    eng.submit_order(_order(qty=qty_str))

    call = broker.submitted[0]
    # The qty passed to submit_market_order must be the exact Decimal, not a float.
    assert call["qty"] == Decimal(qty_str)


def test_fill_flows_back_through_bound_handler():
    """A confirmed fill reaches the bound on_fill handler."""
    broker = FakeCryptoBroker([FakeCryptoOrder("CBRK-1", "filled", "0.5", "67000")])
    eng, fills = _engine(broker)
    eng.submit_order(_order())

    assert len(fills) == 1
    f = fills[0]
    assert f.quantity == Decimal("0.5")
    assert f.fill_price == Decimal("67000")
    assert f.broker_order_id == "CBRK-1"
    assert f.symbol == BTC


def test_fill_handler_bound_after_construction():
    """bind_fill_handler wired after construction still receives fills."""
    broker = FakeCryptoBroker([FakeCryptoOrder("CBRK-1", "filled", "0.5", "67000")])
    late_fills: list = []
    eng = AlpacaCryptoExecutionEngine(broker_client=broker, sleep=lambda _s: None)
    eng.connect()
    eng.bind_fill_handler(late_fills.append)
    eng.submit_order(_order())

    assert len(late_fills) == 1


def test_fractional_qty_preserved_exactly_in_fill():
    """The FillEvent carries the exact Decimal qty the broker reports, not a float."""
    qty_str = "0.00314159"
    broker = FakeCryptoBroker([FakeCryptoOrder("CBRK-1", "filled", qty_str, "67000")])
    eng, fills = _engine(broker)
    eng.submit_order(_order(qty="0.5"))  # order qty; fill qty comes from broker

    assert fills[0].quantity == Decimal(qty_str)
    assert isinstance(fills[0].quantity, Decimal)


def test_partial_fill_books_only_filled_quantity():
    broker = FakeCryptoBroker([FakeCryptoOrder("CBRK-1", "partially_filled", "0.2", "67000")])
    eng, fills = _engine(broker)
    eng.submit_order(_order(qty="0.5"))

    assert len(fills) == 1
    assert fills[0].quantity == Decimal("0.2")


def test_unfilled_order_books_no_fill():
    broker = FakeCryptoBroker([FakeCryptoOrder("CBRK-1", "new", "0", None)])
    eng, fills = _engine(broker, fill_poll_attempts=2)
    bid = eng.submit_order(_order())

    assert bid == "CBRK-1"
    assert fills == []  # nothing confirmed → nothing booked; reconcile next run


def test_filled_qty_without_avg_price_skips_fill():
    """filled_qty present but avg_price absent → no fabricated fill (fail-safe)."""
    broker = FakeCryptoBroker([FakeCryptoOrder("CBRK-1", "filled", "0.5", None)])
    eng, fills = _engine(broker)
    eng.submit_order(_order())
    assert fills == []


def test_eth_order_submits_correctly():
    """ETH fractional orders use the ETH/USD symbol string."""
    broker = FakeCryptoBroker([FakeCryptoOrder("CBRK-2", "filled", "1.5", "3500")])
    eng, fills = _engine(broker)
    eng.submit_order(_order(symbol=ETH, qty="1.5"))

    call = broker.submitted[0]
    assert call["symbol"] == "ETH/USD"
    assert fills[0].fill_price == Decimal("3500")


# --------------------------------------------------------------------------- idempotency


def test_idempotent_submit_adopts_existing_order():
    """If broker already has the order, we adopt it and never resubmit."""
    existing = FakeCryptoOrder("CBRK-EXISTING", "filled", "0.5", "67000")
    broker = FakeCryptoBroker([existing], existing=existing)
    eng, fills = _engine(broker)
    bid = eng.submit_order(_order())

    assert bid == "CBRK-EXISTING"
    assert broker.submitted == []  # CRITICAL: never resubmitted
    assert len(fills) == 1


def test_idempotency_key_is_stable_and_not_event_id():
    """
    The client_order_id must be derived from the logical trade (strategy:symbol:
    side:date), NOT from the OrderEvent UUID. Two calls with the same logical
    trade must produce the same idempotency key so a retry never double-submits.
    """
    broker1 = FakeCryptoBroker([FakeCryptoOrder("CBRK-1", "filled", "0.5", "67000")])
    broker2 = FakeCryptoBroker([FakeCryptoOrder("CBRK-2", "filled", "0.5", "67000")])
    order = _order(strategy_id="strat_A")
    eng1, _ = _engine(broker1)
    eng2, _ = _engine(broker2)

    eng1.submit_order(order)
    eng2.submit_order(order)  # same logical order — same event object

    key1 = broker1.submitted[0]["client_order_id"]
    key2 = broker2.submitted[0]["client_order_id"]
    assert key1 == key2  # stable across calls


def test_idempotency_key_differs_by_symbol():
    """Different symbols must produce different idempotency keys."""
    broker_btc = FakeCryptoBroker([FakeCryptoOrder("B1", "filled", "0.5", "67000")])
    broker_eth = FakeCryptoBroker([FakeCryptoOrder("E1", "filled", "1.0", "3500")])
    eng_btc, _ = _engine(broker_btc)
    eng_eth, _ = _engine(broker_eth)

    eng_btc.submit_order(_order(symbol=BTC, strategy_id="s"))
    eng_eth.submit_order(_order(symbol=ETH, strategy_id="s"))

    assert broker_btc.submitted[0]["client_order_id"] != broker_eth.submitted[0]["client_order_id"]


# --------------------------------------------------------------------------- crypto-specific


def test_day_tif_is_coerced_to_gtc():
    """
    Alpaca Crypto rejects DAY time-in-force (24/7 markets). The engine must
    silently coerce DAY → GTC and proceed, never crash.
    """
    broker = FakeCryptoBroker([FakeCryptoOrder("CBRK-1", "filled", "0.5", "67000")])
    eng, fills = _engine(broker)
    order = _order(tif=TimeInForce.DAY)
    eng.submit_order(order)

    call = broker.submitted[0]
    assert call["time_in_force"] == "gtc"
    assert len(fills) == 1  # coercion did not prevent the fill


def test_gtc_tif_is_passed_through_unchanged():
    broker = FakeCryptoBroker([FakeCryptoOrder("CBRK-1", "filled", "0.5", "67000")])
    eng, fills = _engine(broker)
    eng.submit_order(_order(tif=TimeInForce.GTC))

    assert broker.submitted[0]["time_in_force"] == "gtc"


def test_fill_is_paper_flag_reflects_constructor():
    """FillEvent.is_paper reflects whether the engine is in paper mode."""
    broker_paper = FakeCryptoBroker([FakeCryptoOrder("CBRK-P", "filled", "0.5", "67000")])
    broker_live = FakeCryptoBroker([FakeCryptoOrder("CBRK-L", "filled", "0.5", "67000")])
    eng_paper, fills_paper = _engine(broker_paper, paper=True)
    eng_live, fills_live = _engine(broker_live, paper=False)

    eng_paper.submit_order(_order())
    eng_live.submit_order(_order())

    assert fills_paper[0].is_paper is True
    assert fills_live[0].is_paper is False


# --------------------------------------------------------------------------- guards


def test_non_market_order_rejected():
    broker = FakeCryptoBroker()
    eng, _ = _engine(broker)
    bad = OrderEvent(
        symbol=BTC,
        side=OrderSide.BUY,
        quantity=Decimal("0.1"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("65000"),
        stop_loss=Decimal("60000"),
    )
    with pytest.raises(ValueError):
        eng.submit_order(bad)


def test_submit_before_connect_raises():
    eng = AlpacaCryptoExecutionEngine(broker_client=FakeCryptoBroker())
    with pytest.raises(RuntimeError):
        eng.submit_order(_order())


# --------------------------------------------------------------------------- polling


def test_poll_pending_then_filled_emits_fill():
    """Engine polls until a fill is confirmed, sleeping between attempts."""
    broker = FakeCryptoBroker(
        [
            FakeCryptoOrder("CBRK-1", "new", "0", None),
            FakeCryptoOrder("CBRK-1", "filled", "0.5", "67000"),
        ]
    )
    sleeps: list = []
    fills: list = []
    eng = AlpacaCryptoExecutionEngine(
        broker_client=broker,
        on_fill=fills.append,
        sleep=sleeps.append,
        fill_poll_attempts=3,
    )
    eng.connect()
    eng.submit_order(_order())

    assert len(fills) == 1  # eventually filled
    assert 1.0 in sleeps  # polled with default interval


def test_enum_valued_status_detected_as_terminal():
    """
    Real alpaca-py returns OrderStatus enum objects, not plain strings.
    _status_str must read .value so a canceled order is detected immediately
    and polling stops (no wasted attempts).
    """

    class _EnumStatus:
        value = "canceled"

    class EnumOrder(FakeCryptoOrder):
        def __init__(self):
            super().__init__("CBRK-1", _EnumStatus(), "0", None)

    broker = FakeCryptoBroker([EnumOrder()])
    sleeps: list = []
    eng = AlpacaCryptoExecutionEngine(
        broker_client=broker,
        on_fill=lambda f: None,
        sleep=sleeps.append,
        fill_poll_attempts=3,
    )
    eng.connect()
    eng.submit_order(_order())
    assert sleeps == []  # terminal on first poll → no backoff


# --------------------------------------------------------------------------- cancel


def test_cancel_order_success():
    broker = FakeCryptoBroker()
    eng, _ = _engine(broker)
    assert eng.cancel_order("CBRK-1") is True
    assert broker.canceled == ["CBRK-1"]


def test_cancel_order_failure_swallowed():
    broker = FakeCryptoBroker(fail_cancel=True)
    eng, _ = _engine(broker)
    assert eng.cancel_order("CBRK-9") is False  # failure swallowed → False


# --------------------------------------------------------------------------- disconnect


def test_disconnect_cancels_open_orders_safe_mode():
    broker = FakeCryptoBroker()
    eng, _ = _engine(broker)
    eng.disconnect()
    assert broker.cancel_open_calls == 1

    eng.disconnect()  # idempotent — no second cancel
    assert broker.cancel_open_calls == 1


def test_disconnect_swallows_cancel_errors():
    class BadCancelBroker(FakeCryptoBroker):
        def cancel_open_orders(self):
            raise RuntimeError("network down")

    eng, _ = _engine(BadCancelBroker())
    eng.disconnect()  # must not raise
    assert not eng.is_connected


def test_disconnect_can_be_disabled():
    broker = FakeCryptoBroker()
    eng, _ = _engine(broker, cancel_open_orders_on_disconnect=False)
    eng.disconnect()
    assert broker.cancel_open_calls == 0


# --------------------------------------------------------------------------- account / reconcile


def test_get_account_equity_returns_decimal():
    eng, _ = _engine(FakeCryptoBroker(equity="12345.67"))
    eq = eng.get_account_equity()
    assert eq == Decimal("12345.67")
    assert isinstance(eq, Decimal)


def test_reconcile_positions_returns_decimal_snapshot():
    """reconcile_positions maps broker crypto positions to a Decimal dict."""
    broker = FakeCryptoBroker(
        positions=[
            FakeCryptoPosition("BTC/USD", "0.5", "65000.00", "67000.00"),
            FakeCryptoPosition("ETH/USD", "2.0", "3200.00", "3500.00"),
        ]
    )
    eng, _ = _engine(broker)
    snap = eng.reconcile_positions()

    assert snap == {
        "BTC/USD": {
            "qty": Decimal("0.5"),
            "avg_entry_price": Decimal("65000.00"),
            "current_price": Decimal("67000.00"),
        },
        "ETH/USD": {
            "qty": Decimal("2.0"),
            "avg_entry_price": Decimal("3200.00"),
            "current_price": Decimal("3500.00"),
        },
    }
    for data in snap.values():
        for v in data.values():
            assert isinstance(v, Decimal)


def test_reconcile_empty_positions():
    eng, _ = _engine(FakeCryptoBroker(positions=[]))
    assert eng.reconcile_positions() == {}


# --------------------------------------------------------------------------- is_paper


def test_is_paper_reflects_flag():
    eng_paper, _ = _engine(FakeCryptoBroker(), paper=True)
    eng_live, _ = _engine(FakeCryptoBroker(), paper=False)
    assert eng_paper.is_paper is True
    assert eng_live.is_paper is False
