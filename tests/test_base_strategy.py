"""
Tests for apex.strategy.base_strategy — StrategyContext and BaseStrategy ABC.

Exercises the context accessor methods, sync_state branches, the default
on_tick / on_start / on_finish hooks, and the handle_market_event router
(bar path and tick path).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List

from apex.core.events import MarketEvent, SignalEvent
from apex.core.models import AssetClass, Bar, Symbol, Tick
from apex.strategy.base_strategy import BaseStrategy, StrategyContext

SYM = Symbol("STRAT", AssetClass.EQUITY)
T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)


def _bar(i: int = 0, price: float = 100.0) -> Bar:
    p = Decimal(str(price))
    return Bar(
        symbol=SYM,
        timestamp=T0 + timedelta(days=i),
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal("1000"),
    )


def _tick(price: float = 100.0) -> Tick:
    return Tick(symbol=SYM, timestamp=T0, price=Decimal(str(price)), size=Decimal("100"))


class _NopStrategy(BaseStrategy):
    """Minimal concrete strategy: always returns []."""

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        return []


# ---- StrategyContext --------------------------------------------------------


def test_get_equity_returns_synced_value():
    ctx = StrategyContext()
    ctx.sync_state(equity=Decimal("150000"))
    assert ctx.get_equity() == Decimal("150000")


def test_get_equity_none_before_sync():
    ctx = StrategyContext()
    assert ctx.get_equity() is None


def test_get_bars_returns_last_n():
    ctx = StrategyContext()
    bars = [_bar(i) for i in range(5)]
    ctx._bar_history[SYM.ticker] = bars
    result = ctx.get_bars(SYM, 3)
    assert result == bars[-3:]


def test_get_bars_empty_when_no_history():
    ctx = StrategyContext()
    assert ctx.get_bars(SYM, 10) == []


def test_get_position_none_when_flat():
    ctx = StrategyContext()
    assert ctx.get_position(SYM) is None


def test_sync_state_positions_none_leaves_prior_state():
    ctx = StrategyContext()
    sentinel: dict = {"X": "pos"}
    ctx._positions = sentinel
    ctx.sync_state(positions=None)
    assert ctx._positions is sentinel


def test_sync_state_positions_not_none_updates():
    ctx = StrategyContext()
    new_pos = {SYM.ticker: "a_position"}
    ctx.sync_state(positions=new_pos)
    assert ctx._positions is new_pos
    assert ctx.get_position(SYM) == "a_position"


def test_sync_state_equity_not_none_updates():
    ctx = StrategyContext()
    ctx.sync_state(equity=Decimal("200000"))
    assert ctx.get_equity() == Decimal("200000")


# ---- BaseStrategy hooks -----------------------------------------------------


def test_bind_context_wires_context():
    s = _NopStrategy("s", [SYM])
    ctx = StrategyContext()
    s.bind_context(ctx)
    assert s.context is ctx


def test_on_start_and_finish_are_no_ops():
    s = _NopStrategy("s", [SYM])
    s.on_start()
    s.on_finish()
    # reaching here without raising = pass


def test_on_tick_default_returns_empty_list():
    s = _NopStrategy("s", [SYM])
    result = s.on_tick(_tick())
    assert result == []


# ---- handle_market_event routing --------------------------------------------


def test_handle_market_event_bar_path_calls_on_bar():
    captured: list = []

    class _CapturingStrategy(BaseStrategy):
        def on_bar(self, bar: Bar) -> List[SignalEvent]:
            captured.append(bar)
            return []

    s = _CapturingStrategy("s", [SYM])
    bar = _bar()
    result = s.handle_market_event(MarketEvent(bar=bar))
    assert captured == [bar]
    assert result == []


def test_handle_market_event_tick_path_calls_on_tick():
    captured: list = []

    class _TickStrategy(BaseStrategy):
        def on_bar(self, bar: Bar) -> List[SignalEvent]:
            return []

        def on_tick(self, tick: Tick) -> List[SignalEvent]:
            captured.append(tick)
            return []

    s = _TickStrategy("s", [SYM])
    tick = _tick()
    result = s.handle_market_event(MarketEvent(tick=tick))
    assert captured == [tick]
    assert result == []
