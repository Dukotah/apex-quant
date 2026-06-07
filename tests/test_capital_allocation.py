"""
Tests for the live multi-strategy capital allocator (Phase F3.3,
apex.risk.capital_allocation) and its wiring into scripts.run_once._submit_orders.

The allocator must: validate fail-closed, present a correctly-scaled read-only portfolio view,
size ENTRIES through the *real* RiskManager at the sleeve's weight, leave a single-sleeve book
byte-identical to today, and never throttle exits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List

import pytest

from apex.core.events import OrderEvent, SignalEvent
from apex.core.models import AssetClass, OrderSide, Position, Symbol
from apex.risk.capital_allocation import CapitalAllocator, _ScopedPortfolio
from apex.risk.risk_manager import RiskConfig, RiskManager
from scripts.run_once import RunReport, _submit_orders

A = Symbol("AAA", AssetClass.ETF)
_DT = datetime(2024, 6, 1, 14, 30, tzinfo=timezone.utc)


@dataclass
class _FakePortfolio:
    equity: Decimal = Decimal("100000")
    peak_equity: Decimal = Decimal("100000")
    day_start_equity: Decimal = Decimal("100000")
    open_positions: Dict[str, Position] = field(default_factory=dict)
    exposure: Decimal = Decimal("0")
    last_price: Dict[str, Decimal] = field(default_factory=dict)


def _buy(strategy_id: str = "trend") -> SignalEvent:
    return SignalEvent(
        symbol=A,
        side=OrderSide.BUY,
        strength=Decimal("1.0"),
        strategy_id=strategy_id,
        suggested_stop_loss=Decimal("95"),
        timestamp=_DT,
    )


# --------------------------------------------------------------- config validation


def test_rejects_weight_outside_unit_interval():
    with pytest.raises(ValueError):
        CapitalAllocator({"a": Decimal("1.2")})
    with pytest.raises(ValueError):
        CapitalAllocator({"a": Decimal("-0.1")})


def test_rejects_weights_summing_over_one():
    with pytest.raises(ValueError):
        CapitalAllocator({"trend": Decimal("0.8"), "value": Decimal("0.4")})


def test_allows_sum_at_or_below_one():
    # Funded single sleeve (today's live split) and a genuine blend both validate;
    # a sub-1 sum is allowed (unfunded capital may sit in cash).
    assert CapitalAllocator({"trend": Decimal("1")}).weight_for("trend") == Decimal("1")
    blend = CapitalAllocator({"trend": Decimal("0.8"), "value": Decimal("0.2")})
    assert blend.weight_for("value") == Decimal("0.2")
    assert CapitalAllocator({"trend": Decimal("0.3")}).weight_for("trend") == Decimal("0.3")


def test_weight_for_unknown_strategy_is_zero():
    alloc = CapitalAllocator.single("trend")
    assert alloc.weight_for("value") == Decimal("0")  # unallocated -> no capital (fail closed)


def test_from_live_weights_converts_floats_to_decimal():
    alloc = CapitalAllocator.from_live_weights({"trend": 1.0, "value": 0.0})
    assert alloc.weight_for("trend") == Decimal("1")
    assert isinstance(alloc.weight_for("trend"), Decimal)
    assert alloc.weight_for("value") == Decimal("0")


def test_single_is_full_weight_to_one_sleeve():
    assert CapitalAllocator.single("trend").weights == {"trend": Decimal("1")}


# --------------------------------------------------------------- scoped view


def test_scoped_scales_equity_and_passes_through_the_rest():
    positions = {"XYZ": object()}
    pf = _FakePortfolio(
        equity=Decimal("100000"),
        peak_equity=Decimal("120000"),
        day_start_equity=Decimal("110000"),
        exposure=Decimal("0.30"),
        open_positions=positions,  # type: ignore[arg-type]
        last_price={"AAA": Decimal("100")},
    )
    view = CapitalAllocator({"value": Decimal("0.25")}).scoped(pf, "value")
    assert isinstance(view, _ScopedPortfolio)
    # equity scalars scale by the weight...
    assert view.equity == Decimal("25000")
    assert view.peak_equity == Decimal("30000")
    assert view.day_start_equity == Decimal("27500")
    # ...everything else passes straight through (same objects/values).
    assert view.open_positions is positions
    assert view.exposure == Decimal("0.30")
    assert view.last_price == {"AAA": Decimal("100")}


# --------------------------------------------------------------- sizing through the real RiskManager


def test_weight_one_sizes_identically_to_no_allocator():
    """A single-sleeve allocator must be a no-op: the deployed trend bot is unaffected."""
    rm = RiskManager(RiskConfig())
    pf = _FakePortfolio(last_price={"AAA": Decimal("100")})
    raw = rm.evaluate(_buy(), pf)
    scoped = rm.evaluate(_buy(), CapitalAllocator.single("trend").scoped(pf, "trend"))
    assert raw is not None and scoped is not None
    assert scoped.quantity == raw.quantity


def test_half_weight_halves_entry_size():
    """The whole point: a 50%-weighted sleeve sizes against half the book — via the real RM."""
    rm = RiskManager(RiskConfig())
    pf = _FakePortfolio(last_price={"AAA": Decimal("100")})
    full = rm.evaluate(_buy(), pf)
    half = rm.evaluate(_buy(), CapitalAllocator({"trend": Decimal("0.5")}).scoped(pf, "trend"))
    assert full is not None and half is not None
    assert half.quantity > 0
    assert half.quantity * 2 == full.quantity


def test_zero_weight_blocks_the_entry():
    """An unfunded/unallocated sleeve gets zero capital -> no order (fail closed)."""
    rm = RiskManager(RiskConfig())
    pf = _FakePortfolio(last_price={"AAA": Decimal("100")})
    order = rm.evaluate(_buy(), CapitalAllocator({"trend": Decimal("0")}).scoped(pf, "trend"))
    assert order is None


# --------------------------------------------------------------- run_once wiring


class _RecordingEngine:
    """Minimal execution engine that records submitted orders."""

    def __init__(self) -> None:
        self.orders: List[OrderEvent] = []

    def submit_order(self, order: OrderEvent) -> None:
        self.orders.append(order)


def _report() -> RunReport:
    return RunReport(timestamp=_DT, mode="paper", equity=0.0, num_positions=0)


def test_submit_orders_funded_sleeve_trades():
    rm = RiskManager(RiskConfig())
    pf = _FakePortfolio(last_price={"AAA": Decimal("100")})
    engine = _RecordingEngine()
    report = _report()
    _submit_orders(
        [_buy("trend")], [], rm, pf, engine, report, allocator=CapitalAllocator.single("trend")
    )
    assert len(engine.orders) == 1
    assert report.orders_submitted == 1


def test_submit_orders_unfunded_sleeve_is_blocked_live():
    """End-to-end: a strategy the allocator zeroes places no live order, even with a valid signal."""
    rm = RiskManager(RiskConfig())
    pf = _FakePortfolio(last_price={"AAA": Decimal("100")})
    engine = _RecordingEngine()
    report = _report()
    # value sleeve present but funded=False would yield weight 0 here.
    alloc = CapitalAllocator({"trend": Decimal("1"), "value": Decimal("0")})
    _submit_orders([_buy("value")], [], rm, pf, engine, report, allocator=alloc)
    assert engine.orders == []
    assert report.orders_submitted == 0


def test_submit_orders_without_allocator_is_unchanged():
    """No allocator -> sizing path identical to before F3.3 (the default deployed behaviour)."""
    rm = RiskManager(RiskConfig())
    pf = _FakePortfolio(last_price={"AAA": Decimal("100")})
    engine = _RecordingEngine()
    report = _report()
    _submit_orders([_buy("trend")], [], rm, pf, engine, report)
    assert len(engine.orders) == 1
