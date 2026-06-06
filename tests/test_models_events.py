"""
Tests for apex.core.models and apex.core.events.
The data integrity layer — bad data must be rejected at construction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from apex.core.events import (
    EventType,
    MarketEvent,
    OrderEvent,
    SignalEvent,
)
from apex.core.models import (
    AssetClass,
    Bar,
    OrderSide,
    Position,
    Symbol,
    Tick,
)


def _sym(t="AAPL", ac=AssetClass.EQUITY):
    return Symbol(t, ac)


def _ts():
    return datetime.now(timezone.utc)


# ---- Bar validation ----


def test_bar_rejects_naive_timestamp():
    with pytest.raises(ValueError):
        Bar(
            symbol=_sym(),
            timestamp=datetime(2024, 1, 1),
            open=Decimal("1"),
            high=Decimal("2"),
            low=Decimal("1"),
            close=Decimal("1.5"),
            volume=Decimal("100"),
        )


def test_bar_rejects_high_below_low():
    with pytest.raises(ValueError):
        Bar(
            symbol=_sym(),
            timestamp=_ts(),
            open=Decimal("1"),
            high=Decimal("0.5"),
            low=Decimal("1"),
            close=Decimal("0.8"),
            volume=Decimal("100"),
        )


def test_bar_rejects_close_above_high():
    # The Session-8-catching invariant: open/close must lie inside [low, high].
    with pytest.raises(ValueError):
        Bar(
            symbol=_sym(),
            timestamp=_ts(),
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("12"),  # adjusted close below/above the bar's range — corrupt
            volume=Decimal("100"),
        )


def test_bar_rejects_open_below_low():
    with pytest.raises(ValueError):
        Bar(
            symbol=_sym(),
            timestamp=_ts(),
            open=Decimal("8"),  # open beneath the low
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
            volume=Decimal("100"),
        )


def test_bar_rejects_negative_price():
    with pytest.raises(ValueError):
        Bar(
            symbol=_sym(),
            timestamp=_ts(),
            open=Decimal("-1"),
            high=Decimal("2"),
            low=Decimal("-1"),
            close=Decimal("1"),
            volume=Decimal("100"),
        )


def test_bar_rejects_negative_volume():
    with pytest.raises(ValueError):
        Bar(
            symbol=_sym(),
            timestamp=_ts(),
            open=Decimal("1"),
            high=Decimal("2"),
            low=Decimal("1"),
            close=Decimal("1.5"),
            volume=Decimal("-5"),
        )


def test_valid_bar_constructs():
    b = Bar(
        symbol=_sym(),
        timestamp=_ts(),
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("0.5"),
        close=Decimal("1.5"),
        volume=Decimal("100"),
    )
    assert b.close == Decimal("1.5")


def test_bar_is_frozen():
    b = Bar(
        symbol=_sym(),
        timestamp=_ts(),
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("1"),
        close=Decimal("1.5"),
        volume=Decimal("100"),
    )
    with pytest.raises(Exception):
        b.close = Decimal("99")  # frozen → immutable


# ---- Tick validation ----


def test_tick_rejects_nonpositive_price():
    with pytest.raises(ValueError):
        Tick(symbol=_sym(), timestamp=_ts(), price=Decimal("0"), size=Decimal("1"))


# ---- Position math ----


def test_position_unrealized_pnl_long():
    p = Position(
        symbol=_sym(),
        quantity=Decimal("10"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("110"),
    )
    assert p.unrealized_pnl == Decimal("100")  # 10 shares * $10 gain
    assert p.is_long and not p.is_short


def test_position_unrealized_pnl_short():
    p = Position(
        symbol=_sym(),
        quantity=Decimal("-10"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("90"),
    )
    assert p.unrealized_pnl == Decimal("100")  # short gained as price fell
    assert p.is_short


def test_position_market_value_with_multiplier():
    fut = Symbol("ES", AssetClass.FUTURE, contract_multiplier=Decimal("50"))
    p = Position(
        symbol=fut,
        quantity=Decimal("2"),
        avg_entry_price=Decimal("5000"),
        current_price=Decimal("5010"),
    )
    # 2 contracts * 5010 * 50 multiplier
    assert p.market_value == Decimal("501000")


# ---- Events ----


def test_market_event_requires_bar_or_tick():
    with pytest.raises(ValueError):
        MarketEvent()  # neither bar nor tick


def test_market_event_type_set():
    b = Bar(
        symbol=_sym(),
        timestamp=_ts(),
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("1"),
        close=Decimal("1.5"),
        volume=Decimal("100"),
    )
    e = MarketEvent(bar=b)
    assert e.type == EventType.MARKET
    assert e.event_id  # has a uuid


def test_signal_event_requires_symbol_and_side():
    with pytest.raises(ValueError):
        SignalEvent()


def test_signal_event_valid():
    sig = SignalEvent(
        symbol=_sym(), side=OrderSide.BUY, strength=Decimal("0.8"), strategy_id="s1", reason="test"
    )
    assert sig.type == EventType.SIGNAL
    assert sig.side == OrderSide.BUY


def test_order_event_rejects_nonpositive_qty():
    with pytest.raises(ValueError):
        OrderEvent(symbol=_sym(), side=OrderSide.BUY, quantity=Decimal("0"))


def test_events_have_unique_ids():
    b = Bar(
        symbol=_sym(),
        timestamp=_ts(),
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("1"),
        close=Decimal("1.5"),
        volume=Decimal("100"),
    )
    e1 = MarketEvent(bar=b)
    e2 = MarketEvent(bar=b)
    assert e1.event_id != e2.event_id  # each event is a distinct fact
