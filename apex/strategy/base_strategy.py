"""
apex.strategy.base_strategy
===========================
BaseStrategy: the contract every trading strategy implements.

Design philosophy:
  - Strategies are PURE logic. They consume market data and emit signals.
  - They have NO knowledge of the broker, order sizing, or risk limits.
  - They express INTENT ("go long") and CONVICTION (strength 0..1), nothing more.
  - The RiskManager decides if/how that intent becomes a real, sized order.

This separation is what lets Claude (or you) generate new strategies safely:
a buggy or aggressive strategy literally cannot place a dangerous order,
because it can't place orders at all. It can only suggest.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from apex.core.events import MarketEvent, SignalEvent
from apex.core.models import Bar, Symbol, Tick


class StrategyContext:
    """
    Read-only window into current state, injected into strategies.

    Strategies can SEE their positions, equity, and recent bars, but the
    context exposes no methods to mutate anything. Look, don't touch.
    """
    def __init__(self) -> None:
        self._positions: dict = {}
        self._equity = None
        self._bar_history: dict = {}

    def get_position(self, symbol: Symbol):
        """Current position for a symbol, or None if flat."""
        return self._positions.get(symbol.ticker)

    def get_equity(self):
        """Current total account equity (read-only)."""
        return self._equity

    def get_bars(self, symbol: Symbol, lookback: int) -> List[Bar]:
        """Last `lookback` bars for a symbol (for indicator calculation)."""
        return self._bar_history.get(symbol.ticker, [])[-lookback:]

    def sync_state(self, *, positions: Optional[dict] = None, equity=None) -> None:
        """
        Harness-only write seam. The engine / run_once call this before dispatching
        a bar so strategies see their ACTUAL holdings (broker-reconciled) instead of
        a flag rebuilt from a partial replay window. Strategy logic must still treat
        the context as read-only — this is for the runtime, not for strategies.

        `positions` maps ticker -> Position (the portfolio's open_positions); passing
        None leaves the current snapshot untouched.
        """
        if positions is not None:
            self._positions = positions
        if equity is not None:
            self._equity = equity


class BaseStrategy(ABC):
    """
    Abstract base for all strategies.

    Subclasses implement on_bar() and/or on_tick(). When the engine pushes a
    MarketEvent, it calls the matching hook. The hook returns a list of
    SignalEvents (possibly empty). Returning signals is the ONLY way a
    strategy affects the world.

    Subclasses MUST:
      - be deterministic given the same inputs (critical for backtest/live parity)
      - perform NO I/O (no network, no file, no print — use the logger)
      - never hold broker references or attempt order placement
      - handle insufficient-data gracefully (return [] during warmup)
    """

    def __init__(self, strategy_id: str, symbols: List[Symbol]) -> None:
        self.strategy_id = strategy_id
        self.symbols = symbols
        self.context: Optional[StrategyContext] = None
        self._warmed_up: bool = False

    def bind_context(self, context: StrategyContext) -> None:
        """Engine injects the read-only state view here at startup."""
        self.context = context

    @abstractmethod
    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        """
        Called on each new completed bar. Return zero or more SignalEvents.

        Example:
            if fast_ma crosses above slow_ma:
                return [SignalEvent(symbol=bar.symbol, side=OrderSide.BUY,
                                    strength=Decimal('0.8'),
                                    strategy_id=self.strategy_id,
                                    reason='20/50 MA bullish cross')]
            return []
        """
        ...

    def on_tick(self, tick: Tick) -> List[SignalEvent]:
        """
        Called on each tick (for HFT/intraday strategies). Default: no-op.
        Override only if your strategy operates on ticks rather than bars.
        """
        return []

    def on_start(self) -> None:
        """Optional hook: called once before the first bar (setup/warmup)."""
        ...

    def on_finish(self) -> None:
        """Optional hook: called once after the last bar (cleanup/reporting)."""
        ...

    def handle_market_event(self, event: MarketEvent) -> List[SignalEvent]:
        """
        Router the engine calls. Dispatches to on_bar or on_tick.
        Exceptions here are caught by the engine and the strategy is quarantined
        rather than crashing the whole system.
        """
        if event.bar is not None:
            return self.on_bar(event.bar)
        if event.tick is not None:
            return self.on_tick(event.tick)
        return []
