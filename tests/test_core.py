"""
Tests for apex.core.event_bus and apex.core.clock.
The plumbing everything else runs on — must be rock solid.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apex.core.clock import RealClock, SimulatedClock
from apex.core.event_bus import EventBus
from apex.core.events import EventType, MarketEvent
from apex.core.models import AssetClass, Bar, Symbol


def _bar(ts: datetime) -> Bar:
    s = Symbol("TEST", AssetClass.EQUITY)
    return Bar(
        symbol=s,
        timestamp=ts,
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("1"),
        close=Decimal("1.5"),
        volume=Decimal("100"),
    )


# ---- EventBus ----


def test_fifo_order():
    bus = EventBus()
    t = datetime.now(timezone.utc)
    e1 = MarketEvent(bar=_bar(t))
    e2 = MarketEvent(bar=_bar(t + timedelta(days=1)))
    bus.put(e1)
    bus.put(e2)
    assert bus.get() is e1  # first in, first out
    assert bus.get() is e2
    assert bus.get() is None  # empty


def test_is_empty_and_len():
    bus = EventBus()
    assert bus.is_empty()
    bus.put(MarketEvent(bar=_bar(datetime.now(timezone.utc))))
    assert not bus.is_empty()
    assert len(bus) == 1


def test_put_none_rejected():
    bus = EventBus()
    with pytest.raises(ValueError):
        bus.put(None)


def test_processed_count():
    bus = EventBus()
    bus.put(MarketEvent(bar=_bar(datetime.now(timezone.utc))))
    bus.get()
    assert bus.processed_count == 1


def test_pubsub_dispatch():
    bus = EventBus()
    received = []
    bus.subscribe(EventType.MARKET, lambda e: received.append(e))
    evt = MarketEvent(bar=_bar(datetime.now(timezone.utc)))
    bus.dispatch(evt)
    assert received == [evt]


def test_dispatch_reraises_handler_error_but_runs_all():
    bus = EventBus()
    ran = []

    def bad(e):
        ran.append("bad")
        raise RuntimeError("boom")

    def good(e):
        ran.append("good")

    bus.subscribe(EventType.MARKET, bad)
    bus.subscribe(EventType.MARKET, good)
    with pytest.raises(RuntimeError):
        bus.dispatch(MarketEvent(bar=_bar(datetime.now(timezone.utc))))
    # Both ran despite the first raising — failure is loud, not silent.
    assert "good" in ran and "bad" in ran


def test_drain_to_subscribers():
    bus = EventBus()
    count = []
    bus.subscribe(EventType.MARKET, lambda e: count.append(1))
    for _ in range(3):
        bus.put(MarketEvent(bar=_bar(datetime.now(timezone.utc))))
    dispatched = bus.drain_to_subscribers()
    assert dispatched == 3
    assert len(count) == 3
    assert bus.is_empty()


# ---- Clock ----


def test_real_clock_is_utc():
    c = RealClock()
    assert c.now().tzinfo is not None


def test_simulated_clock_returns_set_time():
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    c = SimulatedClock(start=t)
    assert c.now() == t


def test_simulated_clock_monotonic():
    c = SimulatedClock(start=datetime(2024, 1, 2, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        c.set_time(datetime(2024, 1, 1, tzinfo=timezone.utc))  # backward → reject


def test_simulated_clock_advances():
    c = SimulatedClock(start=datetime(2024, 1, 1, tzinfo=timezone.utc))
    c.set_time(datetime(2024, 1, 5, tzinfo=timezone.utc))
    assert c.now() == datetime(2024, 1, 5, tzinfo=timezone.utc)


def test_simulated_clock_requires_tz():
    with pytest.raises(ValueError):
        SimulatedClock(start=datetime(2024, 1, 1))  # naive → reject


def test_simulated_clock_unset_raises():
    c = SimulatedClock()
    with pytest.raises(RuntimeError):
        c.now()
