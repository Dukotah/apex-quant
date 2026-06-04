"""
Tests for apex.execution.alpaca (AlpacaExecutionEngine) and its factory wiring.

The alpaca-py SDK is replaced by a FakeBroker implementing the BrokerClient seam,
so every safety property is verified offline: idempotent submits (no double-send),
broker-truth fills (only confirmed quantity booked), partial fills, fill polling
with backoff, disconnect safe-mode (cancel working orders), and startup
reconciliation. No SDK, no network, no real keys.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apex.core.config import AppConfig, Broker, ExecutionMode
from apex.core.events import OrderEvent
from apex.core.models import AssetClass, OrderSide, OrderType, Symbol
from apex.execution.alpaca import AlpacaExecutionEngine
from apex.execution.factory import make_execution_engine

NVDA = Symbol("NVDA", AssetClass.EQUITY)


# ------------------------------------------------------------------- fakes

class FakeOrder:
    def __init__(self, id="BRK-1", status="filled", filled_qty="0", filled_avg_price=None):
        self.id = id
        self.status = status
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price


class FakePosition:
    def __init__(self, symbol, qty, avg_entry_price, current_price):
        self.symbol = symbol
        self.qty = qty
        self.avg_entry_price = avg_entry_price
        self.current_price = current_price


class FakeBroker:
    """Implements the BrokerClient protocol with scripted poll states."""

    def __init__(self, poll_states=None, existing=None, equity="100000",
                 positions=None, fail_cancel=False):
        self._states = list(poll_states or [FakeOrder(status="filled", filled_qty="10",
                                                       filled_avg_price="100.50")])
        self._i = 0
        self.submitted = []
        self.canceled = []
        self.cancel_open_calls = 0
        self._existing = existing
        self._equity = equity
        self._positions = positions or []
        self._fail_cancel = fail_cancel

    def find_order_by_client_id(self, client_order_id):
        return self._existing

    def submit_market_order(self, symbol, qty, side, client_order_id, time_in_force):
        self.submitted.append((symbol, qty, side, client_order_id, time_in_force))
        return self._states[0]

    def get_order(self, broker_order_id):
        st = self._states[min(self._i, len(self._states) - 1)]
        self._i += 1
        return st

    def cancel_order(self, broker_order_id):
        if self._fail_cancel:
            raise RuntimeError("broker rejected cancel")
        self.canceled.append(broker_order_id)
        return True

    def cancel_open_orders(self):
        self.cancel_open_calls += 1

    def get_account_equity(self):
        return self._equity

    def list_positions(self):
        return self._positions


def _order(side=OrderSide.BUY, qty="10"):
    return OrderEvent(symbol=NVDA, side=side, quantity=Decimal(qty),
                      order_type=OrderType.MARKET, stop_loss=Decimal("90"))


def _engine(broker, **kw):
    fills = []
    eng = AlpacaExecutionEngine(broker_client=broker, on_fill=fills.append,
                                sleep=lambda _s: None, **kw)
    eng.connect()
    return eng, fills


# ------------------------------------------------------------------- fills

def test_filled_order_emits_broker_truth_fill():
    broker = FakeBroker([FakeOrder("BRK-1", "filled", "10", "100.50")])
    eng, fills = _engine(broker)
    bid = eng.submit_order(_order(qty="10"))

    assert bid == "BRK-1"
    assert len(broker.submitted) == 1
    assert len(fills) == 1
    f = fills[0]
    assert f.quantity == Decimal("10")
    assert f.fill_price == Decimal("100.50")   # broker's price, not an estimate
    assert f.broker_order_id == "BRK-1"


def test_partial_fill_books_only_filled_quantity():
    broker = FakeBroker([FakeOrder("BRK-1", "partially_filled", "4", "100.00")])
    eng, fills = _engine(broker)
    eng.submit_order(_order(qty="10"))
    assert len(fills) == 1
    assert fills[0].quantity == Decimal("4")    # only what actually filled


def test_unfilled_order_books_no_fill():
    broker = FakeBroker([FakeOrder("BRK-1", "new", "0", None)])
    eng, fills = _engine(broker, fill_poll_attempts=2)
    bid = eng.submit_order(_order())
    assert bid == "BRK-1"
    assert fills == []          # nothing confirmed → nothing booked (reconcile next run)


def test_poll_pending_then_filled_emits_fill():
    broker = FakeBroker([
        FakeOrder("BRK-1", "new", "0", None),
        FakeOrder("BRK-1", "filled", "10", "100.0"),
    ])
    sleeps = []
    eng = AlpacaExecutionEngine(broker_client=broker, on_fill=lambda f: sleeps.append("fill"),
                                sleep=lambda s: sleeps.append(s), fill_poll_attempts=3)
    eng.connect()
    eng.submit_order(_order())
    assert "fill" in sleeps          # eventually filled
    assert 1.0 in sleeps             # polled with the default interval at least once


def test_enum_valued_status_is_detected_as_terminal():
    # Real alpaca-py returns an OrderStatus enum, not a plain string. _status_str
    # must read .value so a canceled order (filled_qty 0) is seen as terminal and
    # polling stops immediately instead of burning the whole budget.
    class _EnumStatus:
        value = "canceled"

    class EnumOrder(FakeOrder):
        def __init__(self):
            super().__init__("BRK-1", _EnumStatus(), "0", None)

    broker = FakeBroker([EnumOrder()])
    sleeps = []
    eng = AlpacaExecutionEngine(broker_client=broker, on_fill=lambda f: None,
                                sleep=sleeps.append, fill_poll_attempts=3)
    eng.connect()
    eng.submit_order(_order())
    assert sleeps == []          # terminal on first poll → no backoff sleeps


def test_filled_qty_without_avg_price_skips_fill():
    broker = FakeBroker([FakeOrder("BRK-1", "filled", "10", None)])
    eng, fills = _engine(broker)
    eng.submit_order(_order())
    assert fills == []               # fail-safe: no price → no fabricated fill


# -------------------------------------------------------------- idempotency

def test_idempotent_submit_adopts_existing_order():
    existing = FakeOrder("BRK-EXISTING", "filled", "10", "100.0")
    broker = FakeBroker([existing], existing=existing)
    eng, fills = _engine(broker)
    bid = eng.submit_order(_order())

    assert bid == "BRK-EXISTING"
    assert broker.submitted == []    # CRITICAL: never resubmitted
    assert len(fills) == 1           # adopted existing fill


# ----------------------------------------------------------------- guards

def test_non_market_order_rejected():
    broker = FakeBroker()
    eng, _ = _engine(broker)
    bad = OrderEvent(symbol=NVDA, side=OrderSide.BUY, quantity=Decimal("1"),
                     order_type=OrderType.LIMIT, limit_price=Decimal("100"),
                     stop_loss=Decimal("90"))
    with pytest.raises(ValueError):
        eng.submit_order(bad)


def test_submit_before_connect_raises():
    eng = AlpacaExecutionEngine(broker_client=FakeBroker())
    with pytest.raises(RuntimeError):
        eng.submit_order(_order())


# ----------------------------------------------------------------- cancel

def test_cancel_order_success_and_failure():
    broker = FakeBroker()
    eng, _ = _engine(broker)
    assert eng.cancel_order("BRK-1") is True
    assert broker.canceled == ["BRK-1"]

    broker2 = FakeBroker(fail_cancel=True)
    eng2, _ = _engine(broker2)
    assert eng2.cancel_order("BRK-9") is False     # failure swallowed → False


# ------------------------------------------------------------- disconnect

def test_disconnect_cancels_open_orders_safe_mode():
    broker = FakeBroker()
    eng, _ = _engine(broker)
    eng.disconnect()
    assert broker.cancel_open_calls == 1
    eng.disconnect()                                # idempotent — no second cancel
    assert broker.cancel_open_calls == 1


def test_disconnect_swallows_cancel_errors():
    class BadCancelBroker(FakeBroker):
        def cancel_open_orders(self):
            raise RuntimeError("network down")
    eng, _ = _engine(BadCancelBroker())
    eng.disconnect()        # must not raise
    assert not eng.is_connected


def test_disconnect_can_be_disabled():
    broker = FakeBroker()
    eng, _ = _engine(broker, cancel_open_orders_on_disconnect=False)
    eng.disconnect()
    assert broker.cancel_open_calls == 0


# ----------------------------------------------------------- account / reconcile

def test_get_account_equity_returns_decimal():
    eng, _ = _engine(FakeBroker(equity="123456.78"))
    eq = eng.get_account_equity()
    assert eq == Decimal("123456.78")
    assert isinstance(eq, Decimal)


def test_reconcile_positions_returns_decimal_snapshot():
    broker = FakeBroker(positions=[FakePosition("NVDA", "10", "95.0", "100.0")])
    eng, _ = _engine(broker)
    snap = eng.reconcile_positions()
    assert snap == {"NVDA": {"qty": Decimal("10"),
                             "avg_entry_price": Decimal("95.0"),
                             "current_price": Decimal("100.0")}}


def test_is_paper_reflects_flag():
    eng_paper, _ = _engine(FakeBroker(), paper=True)
    eng_live, _ = _engine(FakeBroker(), paper=False)
    assert eng_paper.is_paper is True
    assert eng_live.is_paper is False


# ------------------------------------------------------------------ factory

def test_factory_paper_alpaca_builds_paper_engine():
    cfg = AppConfig(mode=ExecutionMode.PAPER, broker=Broker.ALPACA,
                    alpaca_key="k", alpaca_secret="s")
    eng = make_execution_engine(cfg)
    assert isinstance(eng, AlpacaExecutionEngine)
    assert eng.is_paper is True


def test_factory_live_alpaca_builds_live_engine():
    cfg = AppConfig(mode=ExecutionMode.LIVE, broker=Broker.ALPACA,
                    alpaca_key="k", alpaca_secret="s")
    eng = make_execution_engine(cfg)
    assert isinstance(eng, AlpacaExecutionEngine)
    assert eng.is_paper is False
