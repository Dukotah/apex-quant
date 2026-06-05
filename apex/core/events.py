"""
apex.core.events
================
The event taxonomy. Modules communicate ONLY by emitting and consuming these.
A strategy never calls the broker; it emits a SignalEvent. The risk manager
never calls a strategy; it consumes SignalEvents and emits OrderEvents.

Event flow:  MarketEvent → SignalEvent → OrderEvent → FillEvent

All events are frozen. An event, once created, is an immutable historical fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from uuid import uuid4

from apex.core.models import (
    Bar,
    OrderSide,
    OrderType,
    Symbol,
    Tick,
    TimeInForce,
)


class EventType(str, Enum):
    MARKET = "market"
    SIGNAL = "signal"
    ORDER = "order"
    FILL = "fill"
    HALT = "halt"        # emitted by risk manager to stop the system


@dataclass(frozen=True)
class Event:
    """Base for all events. Carries a unique id and creation timestamp."""
    type: EventType = field(init=False)
    event_id: str = field(default_factory=lambda: str(uuid4()), init=False)


@dataclass(frozen=True)
class MarketEvent(Event):
    """
    New market data arrived. Emitted by a DataFeed, consumed by strategies
    and the portfolio (to mark positions to market).
    """
    bar: Optional[Bar] = None
    tick: Optional[Tick] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", EventType.MARKET)
        if self.bar is None and self.tick is None:
            raise ValueError("MarketEvent must carry either a bar or a tick")


@dataclass(frozen=True)
class SignalEvent(Event):
    """
    A strategy's INTENT. Not an order — a request. "I want exposure to AAPL."
    The risk manager decides whether this becomes a real order, and at what size.

    Strategies express direction and conviction; they do NOT decide quantity.
    Position sizing is the risk manager's job, by design.
    """
    symbol: Symbol = None
    side: OrderSide = None
    strength: Decimal = Decimal("1.0")      # 0..1 conviction, informs sizing
    strategy_id: str = ""
    suggested_stop_loss: Optional[Decimal] = None    # strategy's idea; risk mgr may override
    suggested_take_profit: Optional[Decimal] = None
    timestamp: Optional[datetime] = None
    reason: str = ""                         # human-readable why (for audit/AI)

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", EventType.SIGNAL)
        if self.symbol is None or self.side is None:
            raise ValueError("SignalEvent requires symbol and side")


@dataclass(frozen=True)
class OrderEvent(Event):
    """
    An APPROVED, SIZED order. Only the RiskManager creates these. Consumed by
    the execution engine. By the time an order exists, it has already passed
    every risk check — execution engines trust OrderEvents implicitly.
    """
    symbol: Symbol = None
    side: OrderSide = None
    quantity: Decimal = Decimal("0")
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None      # attached protective stop (mandatory)
    take_profit: Optional[Decimal] = None
    time_in_force: TimeInForce = TimeInForce.DAY
    strategy_id: str = ""
    signal_id: str = ""                      # links back to originating signal
    timestamp: Optional[datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", EventType.ORDER)
        if self.quantity <= 0:
            raise ValueError("OrderEvent quantity must be positive")


@dataclass(frozen=True)
class FillEvent(Event):
    """
    A confirmed execution. Emitted by the execution engine, consumed by the
    portfolio to update positions/cash. Carries actual fill price and costs.
    """
    symbol: Symbol = None
    side: OrderSide = None
    quantity: Decimal = Decimal("0")
    fill_price: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    order_id: str = ""
    broker_order_id: str = ""
    timestamp: Optional[datetime] = None
    is_paper: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", EventType.FILL)


@dataclass(frozen=True)
class HaltEvent(Event):
    """
    Emitted by the RiskManager when a hard limit (e.g. max drawdown) is
    breached. Consumed by the engine to stop all new order flow immediately.
    """
    reason: str = ""
    triggered_by: str = ""    # which rule
    timestamp: Optional[datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", EventType.HALT)
