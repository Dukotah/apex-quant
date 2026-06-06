"""
apex.core.models
================
Normalized, asset-agnostic data models. Equities, crypto, and futures all
flow through these same structures. Strategies never see a broker-specific
format — only these.

All models are immutable (frozen) to guarantee that data cannot be mutated
as it passes between modules. An event-driven system depends on this.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional


class AssetClass(str, Enum):
    """Supported asset classes. The core treats them uniformly."""

    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"
    FUTURE = "future"
    FOREX = "forex"
    OPTION = "option"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"  # good till cancelled
    IOC = "ioc"  # immediate or cancel
    FOK = "fok"  # fill or kill


@dataclass(frozen=True)
class Symbol:
    """
    A normalized instrument identifier.

    `ticker` is the canonical symbol (e.g. 'AAPL', 'BTC/USD', 'ESZ4').
    `asset_class` lets the system apply class-specific logic (e.g. futures
    contract multipliers, crypto fractional sizing) without strategies
    needing to know the difference.
    """

    ticker: str
    asset_class: AssetClass
    exchange: Optional[str] = None
    contract_multiplier: Decimal = Decimal("1")  # futures/options
    tick_size: Decimal = Decimal("0.01")
    fractionable: bool = False  # crypto / fractional shares

    def __str__(self) -> str:
        return f"{self.ticker}"


@dataclass(frozen=True)
class Bar:
    """
    A single OHLCV bar. The atomic unit of market data in this system.

    `timeframe` is a string like '1Min', '5Min', '1Hour', '1Day' so the
    same model serves any granularity. All timestamps are UTC, always.
    """

    symbol: Symbol
    timestamp: datetime  # bar close time, UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    timeframe: str = "1Day"

    def __post_init__(self) -> None:
        # Fail fast on malformed data — never let bad bars into the system.
        if self.timestamp.tzinfo is None:
            raise ValueError(f"Bar timestamp must be timezone-aware (UTC): {self}")
        if self.high < self.low:
            raise ValueError(f"Bar high < low: {self}")
        # open/close must sit inside [low, high]. This is the invariant that would have
        # caught the Session-8 data bug at the source (an adjusted close below the raw low,
        # from mixing adjustment bases) instead of letting corrupt bars reach sizing/P&L.
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(f"Bar open/close outside [low, high]: {self}")
        if any(p < 0 for p in (self.open, self.high, self.low, self.close)):
            raise ValueError(f"Bar contains negative price: {self}")
        if self.volume < 0:
            raise ValueError(f"Bar has negative volume: {self}")


@dataclass(frozen=True)
class Tick:
    """A single trade or quote tick, for higher-frequency strategies."""

    symbol: Symbol
    timestamp: datetime
    price: Decimal
    size: Decimal
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("Tick timestamp must be timezone-aware (UTC)")
        if self.price <= 0:
            raise ValueError(f"Tick has non-positive price: {self}")


@dataclass(frozen=True)
class Position:
    """A current holding. Owned and produced by the Portfolio, read-only elsewhere."""

    symbol: Symbol
    quantity: Decimal  # negative = short
    avg_entry_price: Decimal
    current_price: Decimal
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.current_price * self.symbol.contract_multiplier

    @property
    def unrealized_pnl(self) -> Decimal:
        return (
            (self.current_price - self.avg_entry_price)
            * self.quantity
            * self.symbol.contract_multiplier
        )

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0


def utc_now() -> datetime:
    """Single source of truth for 'now' in UTC. Never use datetime.now() directly."""
    return datetime.now(timezone.utc)
