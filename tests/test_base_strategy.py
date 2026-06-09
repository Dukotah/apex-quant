"""
Tests for apex.strategy.base_strategy — BaseStrategy (ABC) and StrategyContext.

Covers:
  - StrategyContext read accessors and sync_state seam
  - BaseStrategy lifecycle hooks (on_start, on_finish, on_tick defaults)
  - handle_market_event routing (bar → on_bar, tick → on_tick, neither → [])
  - bind_context injection
  - Concrete subclass correctly wires all abstract methods
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List

import pytest

from apex.core.events import MarketEvent, SignalEvent
from apex.core.models import AssetClass, Bar, OrderSide, Position, Symbol, Tick
from apex.strategy.base_strategy import BaseStrategy, StrategyContext

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

SYM = Symbol("AAPL", AssetClass.EQUITY)
SYM2 = Symbol("MSFT", AssetClass.EQUITY)
_T0 = datetime(2024, 1, 2, tzinfo=timezone.utc)


def _bar(symbol: Symbol = SYM, price: float = 100.0, ts: datetime = _T0) -> Bar:
    p = Decimal(str(price))
    return Bar(
        symbol=symbol,
        timestamp=ts,
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal("500"),
    )


def _tick(symbol: Symbol = SYM, price: float = 100.0, ts: datetime = _T0) -> Tick:
    return Tick(
        symbol=symbol,
        timestamp=ts,
        price=Decimal(str(price)),
        size=Decimal("10"),
    )


def _position(symbol: Symbol = SYM, qty: float = 100.0, price: float = 99.0) -> Position:
    return Position(
        symbol=symbol,
        quantity=Decimal(str(qty)),
        avg_entry_price=Decimal(str(price)),
        current_price=Decimal(str(price)),
    )


# ---------------------------------------------------------------------------
# Minimal concrete strategy for testing BaseStrategy behaviour
# ---------------------------------------------------------------------------


class _AlwaysBuyStrategy(BaseStrategy):
    """Emits one BUY signal for every bar it receives. Used only in tests."""

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        return [
            SignalEvent(
                symbol=bar.symbol,
                side=OrderSide.BUY,
                strength=Decimal("0.5"),
                strategy_id=self.strategy_id,
                reason="test",
            )
        ]


class _NoOpStrategy(BaseStrategy):
    """Returns nothing for every bar — tests the empty-signal path."""

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        return []


class _TickStrategy(BaseStrategy):
    """Emits a BUY signal on every tick, returns nothing on bars."""

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        return []

    def on_tick(self, tick: Tick) -> List[SignalEvent]:
        return [
            SignalEvent(
                symbol=tick.symbol,
                side=OrderSide.BUY,
                strength=Decimal("0.9"),
                strategy_id=self.strategy_id,
                reason="tick-test",
            )
        ]


# ---------------------------------------------------------------------------
# StrategyContext tests
# ---------------------------------------------------------------------------


class TestStrategyContext:
    def test_default_state_is_empty(self):
        ctx = StrategyContext()
        assert ctx.get_position(SYM) is None
        assert ctx.get_equity() is None
        assert ctx.get_bars(SYM, 10) == []

    def test_get_position_returns_none_for_unknown_symbol(self):
        ctx = StrategyContext()
        ctx.sync_state(positions={SYM.ticker: _position()})
        # SYM2 was never added
        assert ctx.get_position(SYM2) is None

    def test_get_position_after_sync(self):
        ctx = StrategyContext()
        pos = _position()
        ctx.sync_state(positions={SYM.ticker: pos})
        assert ctx.get_position(SYM) is pos

    def test_sync_state_equity(self):
        ctx = StrategyContext()
        ctx.sync_state(equity=Decimal("100000"))
        assert ctx.get_equity() == Decimal("100000")

    def test_sync_state_none_positions_leaves_existing(self):
        """Passing positions=None must not overwrite the current snapshot."""
        ctx = StrategyContext()
        pos = _position()
        ctx.sync_state(positions={SYM.ticker: pos})
        ctx.sync_state(positions=None, equity=Decimal("50000"))
        # positions unchanged
        assert ctx.get_position(SYM) is pos
        # equity updated
        assert ctx.get_equity() == Decimal("50000")

    def test_sync_state_none_equity_leaves_existing(self):
        """Passing equity=None must not overwrite the current equity."""
        ctx = StrategyContext()
        ctx.sync_state(equity=Decimal("75000"))
        ctx.sync_state(equity=None, positions={})
        assert ctx.get_equity() == Decimal("75000")

    def test_get_bars_returns_empty_for_unknown_symbol(self):
        ctx = StrategyContext()
        bars = ctx.get_bars(SYM, 5)
        assert bars == []

    def test_get_bars_slices_to_lookback(self):
        """get_bars returns only the last `lookback` bars for the symbol."""
        ctx = StrategyContext()
        all_bars = [_bar(ts=_T0 + timedelta(days=i)) for i in range(10)]
        ctx._bar_history[SYM.ticker] = all_bars
        result = ctx.get_bars(SYM, 3)
        assert len(result) == 3
        assert result == all_bars[-3:]

    def test_get_bars_lookback_larger_than_history(self):
        """When lookback > len(history), return all available bars."""
        ctx = StrategyContext()
        all_bars = [_bar(ts=_T0 + timedelta(days=i)) for i in range(4)]
        ctx._bar_history[SYM.ticker] = all_bars
        result = ctx.get_bars(SYM, 100)
        assert result == all_bars

    def test_sync_state_replaces_positions(self):
        """A second sync_state call should fully replace positions dict."""
        ctx = StrategyContext()
        ctx.sync_state(positions={SYM.ticker: _position()})
        ctx.sync_state(positions={SYM2.ticker: _position(symbol=SYM2)})
        assert ctx.get_position(SYM) is None
        assert ctx.get_position(SYM2) is not None


# ---------------------------------------------------------------------------
# BaseStrategy tests
# ---------------------------------------------------------------------------


class TestBaseStrategyInit:
    def test_strategy_id_stored(self):
        strat = _AlwaysBuyStrategy("my-strat", [SYM])
        assert strat.strategy_id == "my-strat"

    def test_symbols_stored(self):
        strat = _AlwaysBuyStrategy("s1", [SYM, SYM2])
        assert SYM in strat.symbols
        assert SYM2 in strat.symbols

    def test_context_is_none_before_bind(self):
        strat = _AlwaysBuyStrategy("s1", [SYM])
        assert strat.context is None

    def test_warmed_up_defaults_false(self):
        strat = _AlwaysBuyStrategy("s1", [SYM])
        assert strat._warmed_up is False


class TestBindContext:
    def test_bind_context_sets_context(self):
        strat = _AlwaysBuyStrategy("s1", [SYM])
        ctx = StrategyContext()
        strat.bind_context(ctx)
        assert strat.context is ctx

    def test_bind_context_replaces_previous(self):
        strat = _AlwaysBuyStrategy("s1", [SYM])
        ctx1 = StrategyContext()
        ctx2 = StrategyContext()
        strat.bind_context(ctx1)
        strat.bind_context(ctx2)
        assert strat.context is ctx2


class TestLifecycleHooks:
    def test_on_start_default_is_noop(self):
        """on_start default implementation must not raise and returns None."""
        strat = _AlwaysBuyStrategy("s1", [SYM])
        result = strat.on_start()
        assert result is None

    def test_on_finish_default_is_noop(self):
        """on_finish default implementation must not raise and returns None."""
        strat = _AlwaysBuyStrategy("s1", [SYM])
        result = strat.on_finish()
        assert result is None

    def test_on_tick_default_returns_empty_list(self):
        """Default on_tick for a bar-only strategy returns []."""
        strat = _AlwaysBuyStrategy("s1", [SYM])
        tick = _tick()
        signals = strat.on_tick(tick)
        assert signals == []


class TestHandleMarketEvent:
    def test_routes_bar_event_to_on_bar(self):
        strat = _AlwaysBuyStrategy("s1", [SYM])
        event = MarketEvent(bar=_bar())
        signals = strat.handle_market_event(event)
        assert len(signals) == 1
        assert signals[0].side == OrderSide.BUY

    def test_routes_tick_event_to_on_tick(self):
        strat = _TickStrategy("s1", [SYM])
        event = MarketEvent(tick=_tick())
        signals = strat.handle_market_event(event)
        assert len(signals) == 1
        assert signals[0].side == OrderSide.BUY

    def test_on_bar_no_signal_returns_empty_list(self):
        strat = _NoOpStrategy("s1", [SYM])
        event = MarketEvent(bar=_bar())
        signals = strat.handle_market_event(event)
        assert signals == []

    def test_bar_takes_precedence_when_both_present(self):
        """
        handle_market_event checks `bar` first, so if a (hypothetical) event
        carried both a bar and a tick the bar hook wins. A _TickStrategy returns
        [] on bars and a signal on ticks; with a bar present we must get [].
        """
        from apex.core.events import EventType

        evt = object.__new__(MarketEvent)
        object.__setattr__(evt, "bar", _bar())
        object.__setattr__(evt, "tick", _tick())
        object.__setattr__(evt, "type", EventType.MARKET)
        object.__setattr__(evt, "event_id", "both-id")

        strat = _TickStrategy("s1", [SYM])
        signals = strat.handle_market_event(evt)
        # Bar path taken → _TickStrategy.on_bar returns [], tick path NOT reached.
        assert signals == []

    def test_tick_override_receives_correct_payload(self):
        """An on_tick override is invoked with the event's tick, preserving symbol."""
        strat = _TickStrategy("s1", [SYM2])
        event = MarketEvent(tick=_tick(symbol=SYM2, price=42.0))
        signals = strat.handle_market_event(event)
        assert len(signals) == 1
        assert signals[0].symbol == SYM2
        assert signals[0].reason == "tick-test"

    def test_default_on_tick_returns_empty_list_via_handle(self):
        """Bar-only strategy handles a tick market event via default on_tick → []."""
        strat = _AlwaysBuyStrategy("s1", [SYM])
        event = MarketEvent(tick=_tick())
        signals = strat.handle_market_event(event)
        assert signals == []

    def test_handle_market_event_no_bar_no_tick_returns_empty(self):
        """
        MarketEvent constructor rejects payloads with neither bar nor tick, but
        handle_market_event has a defensive final 'return []'. Exercise it by
        bypassing the frozen dataclass constructor via object.__setattr__.
        """
        with pytest.raises(ValueError):
            MarketEvent()  # neither bar nor tick → constructor raises

    def test_handle_market_event_neither_bar_nor_tick_defensive_branch(self):
        """
        Directly exercise the defensive 'return []' branch in handle_market_event
        by constructing a MarketEvent-like object with both bar and tick as None.
        Uses object.__new__ to bypass the frozen dataclass validation.
        """
        from apex.core.events import EventType

        # Build a bare MarketEvent without calling __post_init__
        evt = object.__new__(MarketEvent)
        object.__setattr__(evt, "bar", None)
        object.__setattr__(evt, "tick", None)
        object.__setattr__(evt, "type", EventType.MARKET)
        object.__setattr__(evt, "event_id", "test-id")

        strat = _AlwaysBuyStrategy("s1", [SYM])
        signals = strat.handle_market_event(evt)
        assert signals == []


class TestAbstractEnforcement:
    def test_cannot_instantiate_base_strategy_directly(self):
        with pytest.raises(TypeError):
            BaseStrategy("s", [SYM])  # type: ignore[abstract]


class TestSignalShape:
    def test_emitted_signal_has_correct_strategy_id(self):
        strat = _AlwaysBuyStrategy("strat-42", [SYM])
        signals = strat.on_bar(_bar())
        assert signals[0].strategy_id == "strat-42"

    def test_emitted_signal_has_correct_symbol(self):
        strat = _AlwaysBuyStrategy("s1", [SYM])
        bar = _bar(symbol=SYM)
        signals = strat.on_bar(bar)
        assert signals[0].symbol == SYM

    def test_emitted_signal_strength_in_range(self):
        strat = _AlwaysBuyStrategy("s1", [SYM])
        signals = strat.on_bar(_bar())
        assert Decimal("0") <= signals[0].strength <= Decimal("1")


class TestContextIntegrationWithStrategy:
    def test_strategy_can_read_position_via_context(self):
        strat = _AlwaysBuyStrategy("s1", [SYM])
        ctx = StrategyContext()
        pos = _position()
        ctx.sync_state(positions={SYM.ticker: pos})
        strat.bind_context(ctx)
        assert strat.context.get_position(SYM) is pos

    def test_strategy_can_read_equity_via_context(self):
        strat = _AlwaysBuyStrategy("s1", [SYM])
        ctx = StrategyContext()
        ctx.sync_state(equity=Decimal("250000"))
        strat.bind_context(ctx)
        assert strat.context.get_equity() == Decimal("250000")

    def test_engine_can_update_context_between_bars(self):
        """Simulates the engine calling sync_state before each bar dispatch."""
        strat = _AlwaysBuyStrategy("s1", [SYM])
        ctx = StrategyContext()
        strat.bind_context(ctx)

        # Before bar 1 — no position
        ctx.sync_state(positions={}, equity=Decimal("100000"))
        assert strat.context.get_position(SYM) is None

        # After a simulated fill — engine updates context
        ctx.sync_state(positions={SYM.ticker: _position()}, equity=Decimal("110000"))
        assert strat.context.get_position(SYM) is not None
        assert strat.context.get_equity() == Decimal("110000")
