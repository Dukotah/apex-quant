"""
apex.strategy.library.sma_crossover
===================================
SMA Crossover — the REFERENCE STRATEGY. Complete, working, tested.

Its job is to validate the entire signal pipeline end-to-end: bars in →
indicator computed → signal emitted → (risk manager sizes) → order. Every other
strategy follows this exact shape.

THE RULES (long/flat, trend-following):
  - Maintain a rolling buffer of closing prices.
  - When the fast SMA crosses ABOVE the slow SMA → BUY signal (enter long).
  - When the fast SMA crosses BELOW the slow SMA → SELL signal (exit long).
  - Suggests a protective stop-loss; the RiskManager validates and sizes.

This strategy keeps its OWN price buffer (rather than relying on the context's
history) so it is fully self-contained and unit-testable in isolation.

It is intentionally simple — it's a pipeline test and a teaching example, NOT a
strategy to deploy capital on. For real edges, see dual_momentum / rsi2.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class SMACrossoverStrategy(BaseStrategy):
    """
    Fast/slow SMA crossover. Long when fast > slow (just crossed), flat otherwise.

    Args:
        strategy_id: unique id for this instance.
        symbols: the instruments to trade (typically one).
        fast_period: lookback for the fast SMA (default 20).
        slow_period: lookback for the slow SMA (default 50).
        stop_loss_pct: protective stop distance suggested to the RiskManager.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        fast_period: int = 20,
        slow_period: int = 50,
        stop_loss_pct: Decimal = Decimal("0.05"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if fast_period >= slow_period:
            raise ValueError("fast_period must be < slow_period")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.stop_loss_pct = stop_loss_pct
        # Per-symbol rolling close buffers and current long/flat state.
        self._closes: dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._is_long: dict[str, bool] = {s.ticker: False for s in symbols}

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            # Not a symbol we trade; ignore.
            return []

        closes = self._closes[ticker]
        closes.append(float(bar.close))

        # Keep the buffer bounded (only need slow_period + 2 for a cross check).
        max_len = self.slow_period + 5
        if len(closes) > max_len:
            del closes[:-max_len]

        # Warmup: need at least slow_period + 1 points to detect a cross.
        if len(closes) < self.slow_period + 1:
            return []

        fast = ind.sma(closes, self.fast_period)
        slow = ind.sma(closes, self.slow_period)

        crossed_up = ind.crosses_above(fast, slow)
        crossed_down = ind.crosses_below(fast, slow)

        signals: List[SignalEvent] = []
        price = bar.close

        # Bullish cross and we're flat → go long.
        if crossed_up[-1] and not self._is_long[ticker]:
            stop = price * (Decimal("1") - self.stop_loss_pct)
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.BUY,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=stop,
                    timestamp=bar.timestamp,
                    reason=f"SMA{self.fast_period} crossed above SMA{self.slow_period}",
                )
            )
            self._is_long[ticker] = True

        # Bearish cross and we're long → exit.
        elif crossed_down[-1] and self._is_long[ticker]:
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=f"SMA{self.fast_period} crossed below SMA{self.slow_period}",
                )
            )
            self._is_long[ticker] = False

        return signals
