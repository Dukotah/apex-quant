"""
apex.core.event_bus
==================
The central nervous system. Every module communicates through this queue —
never by calling each other directly. A DataFeed puts a MarketEvent on the bus;
the engine pulls it and routes it. A strategy's signals go on the bus; the risk
manager pulls them. This decoupling is what makes the system testable and safe.

Two usage patterns, both supported:
  1. Queue mode: put() events, get() them in FIFO order (the engine loop uses this).
  2. Pub/sub mode: subscribe(EventType, handler) to be called when events arrive.

Single-threaded by default (backtest determinism). A threading.Lock guards state
so a live feed thread can put() while the engine get()s without corruption.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Callable

from apex.core.events import Event, EventType


class EventBus:
    """
    FIFO event queue with optional pub/sub. The ONLY communication channel
    between modules.

    Never silently drops events. If a subscriber raises, the error is captured
    and re-raised after all other subscribers run, so one bad handler can't
    silently swallow an event (fail loud, not silent).
    """

    def __init__(self) -> None:
        self._queue: deque[Event] = deque()
        self._subscribers: dict[EventType, list[Callable[[Event], None]]] = {}
        self._lock = threading.Lock()
        self._processed_count = 0

    # ---- queue mode -------------------------------------------------------

    def put(self, event: Event) -> None:
        """Enqueue an event. Thread-safe. Rejects None (fail loud)."""
        if event is None:
            raise ValueError("Cannot put None on the event bus")
        with self._lock:
            self._queue.append(event)

    def get(self) -> Event | None:
        """Dequeue the oldest event (FIFO), or None if empty. Thread-safe."""
        with self._lock:
            if not self._queue:
                return None
            self._processed_count += 1
            return self._queue.popleft()

    def is_empty(self) -> bool:
        with self._lock:
            return len(self._queue) == 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def processed_count(self) -> int:
        """Total events dequeued — useful for run summaries and tests."""
        return self._processed_count

    # ---- pub/sub mode -----------------------------------------------------

    def subscribe(self, event_type: EventType, handler: Callable[[Event], None]) -> None:
        """
        Register a handler called whenever an event of this type is dispatched.
        Multiple handlers per type are allowed and called in registration order.
        """
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(handler)

    def dispatch(self, event: Event) -> None:
        """
        Immediately deliver an event to all subscribers of its type (bypasses the
        queue). If any handler raises, remaining handlers still run, then the
        first exception is re-raised — so failures are loud, never swallowed.
        """
        with self._lock:
            handlers = list(self._subscribers.get(event.type, []))
        first_error: Exception | None = None
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # capture, keep going, re-raise after.
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def drain_to_subscribers(self) -> int:
        """
        Pull every queued event and dispatch it to subscribers. Returns the count
        dispatched. Used when running in hybrid queue+subscriber mode.
        """
        count = 0
        while True:
            event = self.get()
            if event is None:
                break
            self.dispatch(event)
            count += 1
        return count

    def clear(self) -> None:
        """Empty the queue (e.g. between backtest runs). Does not clear subscribers."""
        with self._lock:
            self._queue.clear()
