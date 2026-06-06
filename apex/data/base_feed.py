"""
apex.data.base_feed
===================
BaseDataFeed: the abstract contract every data source must fulfill.

Whether data comes from a CSV file (backtest), a websocket (live), or a REST
poll (paper), the rest of the system only ever sees normalized MarketEvents
on the bus. Concrete feeds translate their source's format into Bar/Tick
models and emit MarketEvents.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator, List, Optional

from apex.core.events import MarketEvent
from apex.core.models import Symbol


class BaseDataFeed(ABC):
    """
    Abstract base for all market data sources.

    Lifecycle:
        feed = SomeFeed(symbols=[...])
        feed.connect()
        for event in feed.stream():     # yields MarketEvents
            event_bus.put(event)
        feed.disconnect()

    Implementations MUST:
      - normalize raw data into apex.core.models.Bar / Tick
      - emit timestamps in UTC, in strictly non-decreasing order
      - never raise inside stream() for a single bad bar; log and skip
    """

    def __init__(self, symbols: List[Symbol], timeframe: str = "1Day") -> None:
        self.symbols = symbols
        self.timeframe = timeframe
        self._connected: bool = False

    @abstractmethod
    def connect(self) -> None:
        """
        Establish the connection / open the file / authenticate.
        Must set self._connected = True on success.
        Raise ConnectionError on failure (caller handles retry/backoff).
        """
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Clean up: close sockets, files, sessions. Idempotent."""
        ...

    @abstractmethod
    def stream(self) -> Iterator[MarketEvent]:
        """
        Yield MarketEvents one at a time.

        For HistoricalDataFeed this replays stored bars in chronological order
        then stops (StopIteration ends the backtest).

        For LiveDataFeed this blocks waiting for the next real-time bar/tick
        and yields indefinitely until disconnect() is called.
        """
        ...

    @abstractmethod
    def get_latest_bar(self, symbol: Symbol) -> Optional["Bar"]:  # noqa: F821
        """Return the most recent bar seen for a symbol, or None. Used for warmup."""
        ...

    @property
    def is_connected(self) -> bool:
        return self._connected

    def __enter__(self) -> "BaseDataFeed":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()
