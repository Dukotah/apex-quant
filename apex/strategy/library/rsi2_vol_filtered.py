"""
apex.strategy.library.rsi2_vol_filtered
=======================================
Volatility-Filtered RSI(2) — THE IMPROVEMENT over plain RSI(2).

THE UPGRADE: take RSI(2) signals ONLY when ATR(14) is within ~1 standard
deviation of its 100-day mean. Skips entries during volatility spikes where
mean-reversion fails spectacularly.

Documented effect: ~20% fewer trades, profit factor +0.3.

BUILD APPROACH: subclasses RSI2MeanReversionStrategy and adds one extra gate
in on_bar:
    atr_vals = ATR(14)
    atr_mean = mean(last atr_lookback valid ATR values)
    atr_std  = std(last atr_lookback valid ATR values)
    if abs(atr[-1] - atr_mean) > atr_std_mult * atr_std:
        return []   # too volatile, skip entry (exits still fire)
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List

from apex.core.events import SignalEvent
from apex.core.models import Bar, Symbol
from apex.strategy import indicators as ind
from apex.strategy.library.rsi2_mean_reversion import RSI2MeanReversionStrategy

_log = logging.getLogger(__name__)


class RSI2VolFilteredStrategy(RSI2MeanReversionStrategy):
    """
    RSI(2) Mean Reversion with an ATR(14) volatility filter.

    Inherits all base RSI(2) logic and adds one gate: a BUY is suppressed when
    the current ATR(14) deviates more than `atr_std_mult` standard deviations
    from its trailing `atr_lookback`-period mean.  Exit signals are NEVER
    suppressed — the volatility filter applies to ENTRY only.

    Args:
        strategy_id:    unique id for this instance.
        symbols:        instruments to trade.
        atr_period:     ATR lookback (default 14).
        atr_lookback:   rolling window to compute ATR mean/std (default 100).
        atr_std_mult:   how many std devs define a spike (default 1.0).
        **kwargs:       forwarded to RSI2MeanReversionStrategy.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        atr_period: int = 14,
        atr_lookback: int = 100,
        atr_std_mult: float = 1.0,
        **kwargs,
    ) -> None:
        super().__init__(strategy_id, symbols, **kwargs)
        if atr_period < 1:
            raise ValueError("atr_period must be >= 1")
        if atr_lookback < 2:
            raise ValueError("atr_lookback must be >= 2")
        if atr_std_mult <= 0:
            raise ValueError("atr_std_mult must be positive")

        self.atr_period = atr_period
        self.atr_lookback = atr_lookback
        self.atr_std_mult = atr_std_mult

        # Per-symbol full Bar buffers (need high/low for ATR).
        # We keep a bounded deque-like list.
        self._bars_buf: Dict[str, List[Bar]] = {s.ticker: [] for s in symbols}
        # Max bars to retain — enough for warmup + lookback.
        self._buf_max: int = self.sma_trend + atr_lookback + atr_period + 20

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _atr_filter_ok(self, ticker: str) -> bool:
        """
        Return True when the current ATR(14) is within `atr_std_mult` std devs
        of the trailing `atr_lookback`-period ATR mean (i.e. NOT a spike).
        Return False during warmup — fail closed.
        """
        buf = self._bars_buf[ticker]
        n = len(buf)
        # Need at least atr_period+1 bars for ATR, then atr_lookback valid ATR values.
        min_needed = self.atr_period + self.atr_lookback + 1
        if n < min_needed:
            return False

        highs = [float(b.high) for b in buf]
        lows = [float(b.low) for b in buf]
        closes = [float(b.close) for b in buf]

        atr_vals = ind.atr(highs, lows, closes, self.atr_period)

        # Collect the `atr_lookback` most-recent non-None ATR values.
        valid: List[float] = [v for v in atr_vals if v is not None]
        if len(valid) < self.atr_lookback:
            return False

        recent = valid[-self.atr_lookback:]
        current_atr: float = recent[-1]

        mean = sum(recent) / len(recent)
        variance = sum((x - mean) ** 2 for x in recent) / len(recent)
        std = math.sqrt(variance)

        if std == 0.0:
            # All ATR values are identical — no volatility regime, allow entry.
            return True

        deviation = abs(current_atr - mean)
        ok = deviation <= self.atr_std_mult * std
        if not ok:
            _log.debug(
                "%s ATR filter BLOCKED %s — ATR=%.4f mean=%.4f std=%.4f",
                self.strategy_id,
                ticker,
                current_atr,
                mean,
                std,
            )
        return ok

    # ------------------------------------------------------------------
    # BaseStrategy API
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._bars_buf:
            return []

        # Maintain the bar buffer.
        buf = self._bars_buf[ticker]
        buf.append(bar)
        if len(buf) > self._buf_max:
            del buf[: len(buf) - self._buf_max]

        # Delegate to the parent for all base RSI(2) logic.
        signals = super().on_bar(bar)

        # Filter: suppress BUY signals when ATR spikes above the band.
        # SELL signals (exits) always pass through.
        filtered: List[SignalEvent] = []
        for sig in signals:
            from apex.core.models import OrderSide
            if sig.side == OrderSide.BUY:
                if self._atr_filter_ok(ticker):
                    filtered.append(sig)
                else:
                    _log.info(
                        "%s BUY suppressed by ATR vol-filter for %s",
                        self.strategy_id,
                        ticker,
                    )
                    # Roll back the long-state the parent just set, so the
                    # parent doesn't think we're in a position.
                    self._is_long[ticker] = False
                    self._bars_held[ticker] = 0
            else:
                filtered.append(sig)

        return filtered
