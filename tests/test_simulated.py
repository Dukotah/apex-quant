"""
Tests for apex.execution.simulated.SimulatedExecutionEngine.

All assertions use exact hand-computed Decimal values — no floats, no
approximations. The engine's determinism guarantee is verified by running
the same sequence twice and checking that broker_order_ids match.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List

import pytest

from apex.core.events import FillEvent, OrderEvent
from apex.core.models import AssetClass, OrderSide, OrderType, Symbol
from apex.execution.simulated import SimulatedExecutionEngine

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

SYM = Symbol("AAPL", AssetClass.EQUITY)
SYM_B = Symbol("MSFT", AssetClass.EQUITY)

REF_PRICE = Decimal("100")
SLIPPAGE_PCT = Decimal("0.001")  # 0.1 %
COMMISSION = Decimal("0.005")  # $0.005 per share
QUANTITY = Decimal("10")


def _make_order(
    side: OrderSide,
    symbol: Symbol = SYM,
    quantity: Decimal = QUANTITY,
    order_type: OrderType = OrderType.MARKET,
) -> OrderEvent:
    return OrderEvent(
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        stop_loss=Decimal("90"),  # mandatory stop (required by OrderEvent rules)
    )


def _engine_with_price(
    price: Decimal = REF_PRICE,
    slippage_pct: Decimal = SLIPPAGE_PCT,
    commission_per_share: Decimal = Decimal("0"),
) -> SimulatedExecutionEngine:
    eng = SimulatedExecutionEngine(
        slippage_pct=slippage_pct,
        commission_per_share=commission_per_share,
    )
    eng.update_price(SYM.ticker, price)
    return eng


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_initial_state(self):
        eng = SimulatedExecutionEngine()
        assert not eng.is_connected

    def test_connect_sets_connected(self):
        eng = SimulatedExecutionEngine()
        eng.connect()
        assert eng.is_connected

    def test_disconnect_clears_connected(self):
        eng = SimulatedExecutionEngine()
        eng.connect()
        eng.disconnect()
        assert not eng.is_connected

    def test_disconnect_is_idempotent(self):
        eng = SimulatedExecutionEngine()
        eng.disconnect()  # already disconnected — must not raise
        assert not eng.is_connected

    def test_context_manager(self):
        eng = SimulatedExecutionEngine()
        with eng as e:
            assert e.is_connected
        assert not eng.is_connected

    def test_is_paper_always_true(self):
        assert SimulatedExecutionEngine().is_paper is True


# ---------------------------------------------------------------------------
# Price registration
# ---------------------------------------------------------------------------


class TestUpdatePrice:
    def test_update_price_stores_value(self):
        eng = SimulatedExecutionEngine()
        eng.update_price("AAPL", Decimal("123.45"))
        # Submit a BUY to prove the price was stored (no ValueError).
        eng.submit_order(_make_order(OrderSide.BUY))

    def test_update_price_overwrites(self):
        eng = SimulatedExecutionEngine()
        eng.update_price("AAPL", Decimal("50"))
        eng.update_price("AAPL", Decimal("200"))
        fills: List[FillEvent] = []
        eng.bind_fill_handler(fills.append)
        eng.submit_order(_make_order(OrderSide.BUY))
        # fill_price should be based on 200, not 50
        expected = Decimal("200") * (Decimal("1") + SLIPPAGE_PCT)
        assert fills[0].fill_price == expected


# ---------------------------------------------------------------------------
# BUY fill math
# ---------------------------------------------------------------------------


class TestBuyFill:
    def test_buy_fill_price_above_ref_by_slippage(self):
        """BUY fills at ref * (1 + slippage_pct) — buyer pays more."""
        eng = _engine_with_price()
        fills: List[FillEvent] = []
        eng.bind_fill_handler(fills.append)

        eng.submit_order(_make_order(OrderSide.BUY))

        # Hand-computed: 100 * 1.001 = 100.1
        expected_fill_price = Decimal("100") * (Decimal("1") + Decimal("0.001"))
        assert fills[0].fill_price == expected_fill_price

    def test_buy_fill_price_exact_value(self):
        """Explicit exact value check: ref=100, slip=0.1% → fill=100.1"""
        eng = _engine_with_price(price=Decimal("100"), slippage_pct=Decimal("0.001"))
        fills: List[FillEvent] = []
        eng.bind_fill_handler(fills.append)
        eng.submit_order(_make_order(OrderSide.BUY, quantity=Decimal("1")))
        assert fills[0].fill_price == Decimal("100.1")

    def test_buy_fill_price_is_above_ref(self):
        eng = _engine_with_price()
        fills: List[FillEvent] = []
        eng.bind_fill_handler(fills.append)
        eng.submit_order(_make_order(OrderSide.BUY))
        assert fills[0].fill_price > REF_PRICE


# ---------------------------------------------------------------------------
# SELL fill math
# ---------------------------------------------------------------------------


class TestSellFill:
    def test_sell_fill_price_below_ref_by_slippage(self):
        """SELL fills at ref * (1 - slippage_pct) — seller receives less."""
        eng = _engine_with_price()
        fills: List[FillEvent] = []
        eng.bind_fill_handler(fills.append)

        eng.submit_order(_make_order(OrderSide.SELL))

        # Hand-computed: 100 * (1 - 0.001) = 99.9
        expected_fill_price = Decimal("100") * (Decimal("1") - Decimal("0.001"))
        assert fills[0].fill_price == expected_fill_price

    def test_sell_fill_price_exact_value(self):
        """Explicit exact value check: ref=100, slip=0.1% → fill=99.9"""
        eng = _engine_with_price(price=Decimal("100"), slippage_pct=Decimal("0.001"))
        fills: List[FillEvent] = []
        eng.bind_fill_handler(fills.append)
        eng.submit_order(_make_order(OrderSide.SELL, quantity=Decimal("1")))
        assert fills[0].fill_price == Decimal("99.9")

    def test_sell_fill_price_is_below_ref(self):
        eng = _engine_with_price()
        fills: List[FillEvent] = []
        eng.bind_fill_handler(fills.append)
        eng.submit_order(_make_order(OrderSide.SELL))
        assert fills[0].fill_price < REF_PRICE


# ---------------------------------------------------------------------------
# Slippage amount
# ---------------------------------------------------------------------------


class TestSlippageAmount:
    def test_buy_slippage_amount(self):
        """
        slippage_amount = abs(fill_price - ref) * qty
        ref=100, slip_pct=0.001, qty=10
        fill_price = 100.1
        slippage_amount = (100.1 - 100) * 10 = 0.1 * 10 = 1.0
        """
        eng = _engine_with_price(price=Decimal("100"), slippage_pct=Decimal("0.001"))
        fills: List[FillEvent] = []
        eng.bind_fill_handler(fills.append)
        eng.submit_order(_make_order(OrderSide.BUY, quantity=Decimal("10")))
        assert fills[0].slippage == Decimal("1.0")

    def test_sell_slippage_amount(self):
        """
        ref=100, slip_pct=0.001, qty=10
        fill_price = 99.9
        slippage_amount = abs(99.9 - 100) * 10 = 0.1 * 10 = 1.0
        """
        eng = _engine_with_price(price=Decimal("100"), slippage_pct=Decimal("0.001"))
        fills: List[FillEvent] = []
        eng.bind_fill_handler(fills.append)
        eng.submit_order(_make_order(OrderSide.SELL, quantity=Decimal("10")))
        assert fills[0].slippage == Decimal("1.0")

    def test_zero_slippage_pct(self):
        eng = _engine_with_price(slippage_pct=Decimal("0"))
        fills: List[FillEvent] = []
        eng.bind_fill_handler(fills.append)
        eng.submit_order(_make_order(OrderSide.BUY, quantity=Decimal("10")))
        assert fills[0].slippage == Decimal("0")
        assert fills[0].fill_price == REF_PRICE


# ---------------------------------------------------------------------------
# Commission
# ---------------------------------------------------------------------------


class TestCommission:
    def test_commission_computed_correctly(self):
        """
        commission_per_share=0.005, qty=10 → total commission = 0.05
        """
        eng = _engine_with_price(commission_per_share=Decimal("0.005"))
        fills: List[FillEvent] = []
        eng.bind_fill_handler(fills.append)
        eng.submit_order(_make_order(OrderSide.BUY, quantity=Decimal("10")))
        assert fills[0].commission == Decimal("0.05")

    def test_zero_commission_default(self):
        """Default commission_per_share is 0 → commission is 0."""
        eng = _engine_with_price()
        fills: List[FillEvent] = []
        eng.bind_fill_handler(fills.append)
        eng.submit_order(_make_order(OrderSide.BUY, quantity=Decimal("10")))
        assert fills[0].commission == Decimal("0")

    def test_commission_scales_with_quantity(self):
        """commission = commission_per_share * quantity (linear)."""
        comm = Decimal("0.01")
        eng = _engine_with_price(commission_per_share=comm)
        fills: List[FillEvent] = []
        eng.bind_fill_handler(fills.append)
        qty = Decimal("7")
        eng.submit_order(_make_order(OrderSide.BUY, quantity=qty))
        assert fills[0].commission == comm * qty


# ---------------------------------------------------------------------------
# on_fill callback
# ---------------------------------------------------------------------------


class TestOnFillCallback:
    def test_callback_receives_fill_event(self):
        received: List[FillEvent] = []
        eng = SimulatedExecutionEngine(on_fill=received.append)
        eng.update_price(SYM.ticker, REF_PRICE)
        eng.submit_order(_make_order(OrderSide.BUY))
        assert len(received) == 1
        assert isinstance(received[0], FillEvent)

    def test_callback_fill_has_correct_symbol(self):
        received: List[FillEvent] = []
        eng = SimulatedExecutionEngine(on_fill=received.append)
        eng.update_price(SYM.ticker, REF_PRICE)
        eng.submit_order(_make_order(OrderSide.BUY))
        assert received[0].symbol == SYM

    def test_callback_fill_has_correct_side(self):
        received: List[FillEvent] = []
        eng = SimulatedExecutionEngine(on_fill=received.append)
        eng.update_price(SYM.ticker, REF_PRICE)
        eng.submit_order(_make_order(OrderSide.SELL))
        assert received[0].side == OrderSide.SELL

    def test_callback_fill_is_paper(self):
        received: List[FillEvent] = []
        eng = SimulatedExecutionEngine(on_fill=received.append)
        eng.update_price(SYM.ticker, REF_PRICE)
        eng.submit_order(_make_order(OrderSide.BUY))
        assert received[0].is_paper is True

    def test_no_callback_does_not_raise(self):
        """Submitting with no on_fill bound is legal — just no delivery."""
        eng = SimulatedExecutionEngine()
        eng.update_price(SYM.ticker, REF_PRICE)
        eng.submit_order(_make_order(OrderSide.BUY))  # must not raise

    def test_bind_fill_handler_post_init(self):
        """bind_fill_handler() wires a handler after construction."""
        received: List[FillEvent] = []
        eng = SimulatedExecutionEngine()
        eng.bind_fill_handler(received.append)
        eng.update_price(SYM.ticker, REF_PRICE)
        eng.submit_order(_make_order(OrderSide.BUY))
        assert len(received) == 1


# ---------------------------------------------------------------------------
# Deterministic broker_order_id
# ---------------------------------------------------------------------------


class TestBrokerOrderId:
    def test_first_order_is_sim_1(self):
        eng = _engine_with_price()
        bid = eng.submit_order(_make_order(OrderSide.BUY))
        assert bid == "SIM-1"

    def test_ids_increment(self):
        eng = _engine_with_price()
        id1 = eng.submit_order(_make_order(OrderSide.BUY))
        id2 = eng.submit_order(_make_order(OrderSide.SELL))
        id3 = eng.submit_order(_make_order(OrderSide.BUY))
        assert id1 == "SIM-1"
        assert id2 == "SIM-2"
        assert id3 == "SIM-3"

    def test_ids_are_deterministic_across_instances(self):
        """Same submission sequence on two fresh engines → same ids."""

        def _run_sequence() -> List[str]:
            eng = _engine_with_price()
            return [
                eng.submit_order(_make_order(OrderSide.BUY)),
                eng.submit_order(_make_order(OrderSide.SELL)),
                eng.submit_order(_make_order(OrderSide.BUY)),
            ]

        assert _run_sequence() == _run_sequence()

    def test_fill_event_carries_broker_order_id(self):
        fills: List[FillEvent] = []
        eng = _engine_with_price()
        eng.bind_fill_handler(fills.append)
        bid = eng.submit_order(_make_order(OrderSide.BUY))
        assert fills[0].broker_order_id == bid

    def test_fill_event_carries_order_id(self):
        """FillEvent.order_id must match the originating OrderEvent.event_id."""
        fills: List[FillEvent] = []
        eng = _engine_with_price()
        eng.bind_fill_handler(fills.append)
        order = _make_order(OrderSide.BUY)
        eng.submit_order(order)
        assert fills[0].order_id == order.event_id


# ---------------------------------------------------------------------------
# Fail-closed: no price → ValueError
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_no_price_raises_value_error(self):
        eng = SimulatedExecutionEngine()
        with pytest.raises(ValueError, match="no reference price"):
            eng.submit_order(_make_order(OrderSide.BUY))

    def test_unknown_ticker_raises(self):
        eng = SimulatedExecutionEngine()
        eng.update_price("OTHER", Decimal("50"))
        # SYM.ticker = "AAPL" — price registered for "OTHER", not "AAPL"
        with pytest.raises(ValueError):
            eng.submit_order(_make_order(OrderSide.BUY, symbol=SYM))

    def test_non_market_order_raises(self):
        eng = _engine_with_price()
        with pytest.raises(ValueError, match="not supported"):
            eng.submit_order(_make_order(OrderSide.BUY, order_type=OrderType.LIMIT))


# ---------------------------------------------------------------------------
# cancel_order / get_account_equity / reconcile_positions
# ---------------------------------------------------------------------------


class TestAncillaryMethods:
    def test_cancel_order_returns_true(self):
        eng = SimulatedExecutionEngine()
        assert eng.cancel_order("SIM-1") is True

    def test_get_account_equity_returns_decimal_zero(self):
        result = SimulatedExecutionEngine().get_account_equity()
        assert isinstance(result, Decimal)
        assert result == Decimal("0")

    def test_reconcile_positions_returns_empty_dict(self):
        result = SimulatedExecutionEngine().reconcile_positions()
        assert result == {}

    def test_cancel_open_orders_does_not_raise(self):
        """
        cancel_open_orders() is a safe no-op on the simulator: the simulator fills
        every order synchronously so there are never resting orders. The method must
        not raise regardless of connection state.
        """
        eng = SimulatedExecutionEngine()
        eng.cancel_open_orders()  # disconnected — must not raise

        eng.connect()
        eng.cancel_open_orders()  # connected — must not raise

    def test_cancel_open_orders_is_idempotent(self):
        """Multiple calls are safe — no state is corrupted."""
        eng = SimulatedExecutionEngine()
        eng.cancel_open_orders()
        eng.cancel_open_orders()
        eng.cancel_open_orders()
        # No assertion needed beyond "did not raise"; verify no side effects.
        assert not eng.is_connected  # state unchanged

    def test_cancel_open_orders_satisfies_base_contract(self):
        """
        The SimulatedExecutionEngine inherits from BaseExecutionEngine and its
        cancel_open_orders() must satisfy the base contract: callable at any time
        without crashing, and the set of resting orders afterward is empty
        (trivially true in simulation since no orders are ever resting).
        """
        from apex.execution.base_execution import BaseExecutionEngine

        eng = SimulatedExecutionEngine()
        assert isinstance(eng, BaseExecutionEngine)
        # Calling the method is the assertion — it must not raise.
        eng.cancel_open_orders()


# ---------------------------------------------------------------------------
# Full round-trip: connect → update_price → submit → fill
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_full_buy_round_trip(self):
        """
        Exact scenario:
          ref=200, slip_pct=0.002, commission_per_share=0.01, qty=5

          fill_price  = 200 * 1.002          = 200.4
          slippage    = (200.4 - 200) * 5    = 0.4 * 5 = 2.0
          commission  = 0.01 * 5             = 0.05
        """
        fills: List[FillEvent] = []
        eng = SimulatedExecutionEngine(
            slippage_pct=Decimal("0.002"),
            commission_per_share=Decimal("0.01"),
            on_fill=fills.append,
        )
        eng.connect()
        eng.update_price("AAPL", Decimal("200"))
        order = OrderEvent(
            symbol=SYM,
            side=OrderSide.BUY,
            quantity=Decimal("5"),
            order_type=OrderType.MARKET,
            stop_loss=Decimal("190"),
        )
        bid = eng.submit_order(order)

        assert bid == "SIM-1"
        f = fills[0]
        assert f.fill_price == Decimal("200.4")
        assert f.slippage == Decimal("2.0")
        assert f.commission == Decimal("0.05")
        assert f.is_paper is True
        assert f.symbol == SYM
        assert f.side == OrderSide.BUY
        assert f.quantity == Decimal("5")

    def test_full_sell_round_trip(self):
        """
        Exact scenario:
          ref=200, slip_pct=0.002, commission_per_share=0.01, qty=5

          fill_price  = 200 * (1 - 0.002)   = 200 * 0.998 = 199.6
          slippage    = abs(199.6 - 200) * 5 = 0.4 * 5 = 2.0
          commission  = 0.01 * 5             = 0.05
        """
        fills: List[FillEvent] = []
        eng = SimulatedExecutionEngine(
            slippage_pct=Decimal("0.002"),
            commission_per_share=Decimal("0.01"),
            on_fill=fills.append,
        )
        eng.connect()
        eng.update_price("AAPL", Decimal("200"))
        order = OrderEvent(
            symbol=SYM,
            side=OrderSide.SELL,
            quantity=Decimal("5"),
            order_type=OrderType.MARKET,
            stop_loss=Decimal("190"),
        )
        eng.submit_order(order)

        f = fills[0]
        assert f.fill_price == Decimal("199.6")
        assert f.slippage == Decimal("2.0")
        assert f.commission == Decimal("0.05")
