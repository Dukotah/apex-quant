"""
Tests for apex.execution.alpaca_options.

A fake OptionBrokerClient exercises every safety property offline: single-leg
submission, 2-leg vertical submission with one FillEvent per filled leg,
idempotency (a re-submit of the same logical order adopts the existing broker
order rather than double-submitting), and fail-closed behavior (a raising broker
never crashes submit_order and books no fill). No alpaca-py, no network.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from apex.core.models import AssetClass, OrderSide, Symbol
from apex.core.option import (
    OptionContract,
    OptionLeg,
    OptionOrder,
    OptionRight,
    OptionType,
)
from apex.execution.alpaca_options import AlpacaOptionsExecutionEngine

SPY = Symbol("SPY", AssetClass.ETF, contract_multiplier=Decimal("100"))
EXPIRY = date(2024, 9, 20)


def _contract(strike, otype=OptionType.CALL):
    return OptionContract(SPY, EXPIRY, Decimal(str(strike)), otype)


def _single_leg_order(qty=1):
    return OptionOrder(legs=(OptionLeg(_contract(450), OptionRight.BUY),), quantity=qty)


def _vertical_order(qty=1):
    long_leg = OptionLeg(_contract(450), OptionRight.BUY)
    short_leg = OptionLeg(_contract(455), OptionRight.SELL)
    return OptionOrder(legs=(long_leg, short_leg), quantity=qty, limit_price=Decimal("2.50"))


# --------------------------------------------------------------- fake broker


class _Leg:
    def __init__(self, symbol, side, filled_qty="0", filled_avg_price=None):
        self.symbol = symbol
        self.side = side
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price


class _Order:
    def __init__(self, oid, status, legs):
        self.id = oid
        self.status = status
        self.legs = legs


class FakeBroker:
    """Records submissions; returns canned filled orders. Configurable to raise."""

    def __init__(self, *, fill=True, raise_on_submit=False, preexisting=None):
        self.fill = fill
        self.raise_on_submit = raise_on_submit
        self.submitted = []  # list of (legs, qty, client_order_id, tif, limit)
        self.cancelled_open = 0
        self._by_client_id = {}
        if preexisting is not None:
            self._by_client_id[preexisting[0]] = preexisting[1]
        self._counter = 0

    def find_order_by_client_id(self, client_order_id):
        return self._by_client_id.get(client_order_id)

    def submit_option_order(self, legs, qty, client_order_id, time_in_force, limit_price):
        if self.raise_on_submit:
            raise RuntimeError("broker rejected order")
        self.submitted.append((legs, qty, client_order_id, time_in_force, limit_price))
        self._counter += 1
        order = self._make_order(f"brk-{self._counter}", legs, qty)
        self._by_client_id[client_order_id] = order
        return order

    def get_order(self, broker_order_id):
        for order in self._by_client_id.values():
            if order.id == broker_order_id:
                return order
        raise KeyError(broker_order_id)

    def _make_order(self, oid, legs, qty):
        status = "filled" if self.fill else "new"
        order_legs = [
            _Leg(
                symbol=leg["symbol"],
                side=leg["side"],
                filled_qty=str(qty * leg["ratio_qty"]) if self.fill else "0",
                filled_avg_price="5.00" if self.fill else None,
            )
            for leg in legs
        ]
        return _Order(oid, status, order_legs)

    def cancel_order(self, broker_order_id):
        return True

    def cancel_open_orders(self):
        self.cancelled_open += 1


def _engine(broker, **kw):
    eng = AlpacaOptionsExecutionEngine(broker_client=broker, sleep=lambda _s: None, **kw)
    eng.connect()
    return eng


# --------------------------------------------------------------- single leg


def test_submit_single_leg_fills():
    fills = []
    broker = FakeBroker(fill=True)
    eng = _engine(broker, on_fill=fills.append)
    result = eng.submit_order(_single_leg_order(qty=2))

    assert result.ok
    assert result.broker_order_id == "brk-1"
    assert len(broker.submitted) == 1
    assert len(result.fills) == 1
    assert len(fills) == 1
    fill = fills[0]
    assert fill.side == OrderSide.BUY
    assert fill.quantity == Decimal("2")
    assert fill.fill_price == Decimal("5.00")
    assert fill.symbol.asset_class == AssetClass.OPTION
    # The OCC symbol is carried through the FillEvent's ticker slot.
    assert fill.symbol.ticker == _contract(450).occ_symbol


# --------------------------------------------------------------- vertical


def test_submit_vertical_emits_fill_per_leg():
    fills = []
    broker = FakeBroker(fill=True)
    eng = _engine(broker, on_fill=fills.append)
    result = eng.submit_order(_vertical_order(qty=1))

    assert result.ok
    legs_payload = broker.submitted[0][0]
    assert len(legs_payload) == 2  # multi-leg submitted as one order
    assert broker.submitted[0][4] == Decimal("2.50")  # net limit forwarded
    assert len(result.fills) == 2
    sides = {f.side for f in fills}
    assert sides == {OrderSide.BUY, OrderSide.SELL}


# --------------------------------------------------------------- idempotency


def test_idempotent_resubmit_adopts_existing():
    fills = []
    broker = FakeBroker(fill=True)
    eng = _engine(broker, on_fill=fills.append)
    order = _single_leg_order()

    first = eng.submit_order(order)
    second = eng.submit_order(order)  # same logical order, retried

    assert first.ok and second.ok
    assert first.broker_order_id == second.broker_order_id
    # Only ONE actual submission reached the broker; the retry adopted it.
    assert len(broker.submitted) == 1


def test_stable_client_order_id():
    order = _single_leg_order()
    a = AlpacaOptionsExecutionEngine.client_order_id(order)
    b = AlpacaOptionsExecutionEngine.client_order_id(order)
    assert a == b
    assert a != AlpacaOptionsExecutionEngine.client_order_id(_vertical_order())


# --------------------------------------------------------------- fail closed


def test_submit_before_connect_fails_closed():
    broker = FakeBroker()
    eng = AlpacaOptionsExecutionEngine(broker_client=broker, sleep=lambda _s: None)
    result = eng.submit_order(_single_leg_order())  # never connected
    assert not result.ok
    assert "connect" in result.error
    assert result.fills == ()


def test_submit_error_fails_closed_no_crash():
    fills = []
    broker = FakeBroker(raise_on_submit=True)
    eng = _engine(broker, on_fill=fills.append)
    result = eng.submit_order(_single_leg_order())
    assert not result.ok
    assert result.error
    assert result.fills == ()
    assert fills == []  # no fill booked on failure


def test_no_fill_when_not_filled():
    fills = []
    broker = FakeBroker(fill=False)
    eng = _engine(broker, on_fill=fills.append)
    result = eng.submit_order(_single_leg_order())
    assert result.ok  # submission succeeded...
    assert result.fills == ()  # ...but nothing filled yet, so no fill booked
    assert fills == []


# --------------------------------------------------------------- lifecycle


def test_disconnect_cancels_open_orders():
    broker = FakeBroker()
    eng = _engine(broker)
    eng.disconnect()
    assert broker.cancelled_open == 1
    # Idempotent: a second disconnect does nothing more.
    eng.disconnect()
    assert broker.cancelled_open == 1


def test_is_paper_flag():
    assert _engine(FakeBroker(), paper=True).is_paper is True
    assert _engine(FakeBroker(), paper=False).is_paper is False
