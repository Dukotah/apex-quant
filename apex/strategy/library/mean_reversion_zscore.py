"""
apex.strategy.library.mean_reversion_zscore
============================================
Z-Score Mean Reversion (LONG-ONLY) — UNVALIDATED RESEARCH CANDIDATE.

>>> THIS STRATEGY HAS NOT BEEN THROUGH THE GAUNTLET. <<<
It is a research idea, not a deployable edge. Do NOT route real capital through it
until it clears the validation gates (see docs/VALIDATION_GAUNTLET.md). It is
provided so the idea can be backtested and stress-tested like any candidate.

THE IDEA:
  Prices that stretch far below their own recent mean tend to snap back. We measure
  "how stretched" with the rolling z-score of the close:

      z = (close - rolling_mean) / rolling_stdev      (population stdev)

  - Deeply negative z (oversold) → the target state is LONG.
  - z back near zero (reverted to fair value) → the target state is FLAT.

  There is NO shorting: a positive z-score is simply "not a long", never a short.

THE RULES (long/flat, per symbol):
  - Maintain a rolling close buffer per symbol.
  - Each bar compute z over `lookback` closes (need `lookback` points; stdev must be
    > 0 or there is no usable signal and we stay flat).
  - want_long becomes True when z <= entry_z (e.g. -2.0, deeply oversold).
  - want_long becomes False when z >= exit_z (e.g. -0.5, near the mean) — the
    position has reverted, so exit.
  - Between entry_z and exit_z we HOLD whatever the target state already was
    (hysteresis): this prevents flip-flopping in the dead band and means the target
    is a latched state, not a per-bar reading.

POSITION AWARENESS (correct on restart, never pyramids):
  Like multi_asset_trend, this strategy holds NO internal long/flat flag. It reads
  its ACTUAL holding from the broker-reconciled StrategyContext each bar and emits
  only the DELTA toward the target state:
    - target LONG  and currently flat → BUY  (with a stop attached)
    - target FLAT  and currently long → SELL (full exit)
    - otherwise → no signal (idempotent; a held + still-oversold name emits nothing,
      so it never pyramids, and a cold start into an established setup still acts).

STOP-LOSS (always attached):
  Every BUY carries a `suggested_stop_loss`. We prefer an ATR-based stop
  (entry - atr_mult * ATR) for a volatility-aware distance; during the ATR warmup
  (before `atr_period`+1 bars) we fall back to a fixed percentage stop
  (entry * (1 - stop_loss_pct)). Fail closed: the stop is never None on a BUY.

Determinism: pure function of the bars seen; no I/O, no wall-clock, no randomness.
Indicator reuse: ATR comes from apex.strategy.indicators; the z-score is a private
calculation in this module (no parallel ind_* imports). Stats use float to match
the indicator layer's convention; the suggested stop is returned as Decimal.
"""
from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class MeanReversionZScoreStrategy(BaseStrategy):
    """
    Long-only rolling-z-score mean reversion. UNVALIDATED research candidate.

    Args:
        strategy_id: unique id for this instance.
        symbols: the universe to trade (each symbol is independent).
        lookback: window (in closes) for the rolling mean/stdev (default 20).
        entry_z: enter long when z <= this (deeply oversold; default -2.0).
        exit_z: exit when z >= this (reverted toward the mean; default -0.5).
        atr_period: ATR lookback for the volatility-aware stop (default 14).
        atr_mult: stop distance = atr_mult * ATR below entry (default 2.0).
        stop_loss_pct: percentage stop used during the ATR warmup (default 0.05).
        strength: conviction for entries, passed through to the RiskManager.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        lookback: int = 20,
        entry_z: float = -2.0,
        exit_z: float = -0.5,
        atr_period: int = 14,
        atr_mult: Decimal = Decimal("2.0"),
        stop_loss_pct: Decimal = Decimal("0.05"),
        strength: Decimal = Decimal("1.0"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if lookback < 2:
            raise ValueError("lookback must be >= 2")
        if entry_z >= exit_z:
            raise ValueError("entry_z must be < exit_z (entry deeper/lower than exit)")
        if atr_period < 1:
            raise ValueError("atr_period must be >= 1")
        if atr_mult <= 0:
            raise ValueError("atr_mult must be positive")
        if not (Decimal("0") < stop_loss_pct < Decimal("1")):
            raise ValueError("stop_loss_pct must be in (0, 1)")
        self.lookback = lookback
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.atr_period = atr_period
        self.atr_mult = atr_mult
        self.stop_loss_pct = stop_loss_pct
        self.strength = strength
        # Per-symbol rolling OHLC buffers. Closes drive the z-score; high/low/close
        # feed ATR for the stop. NO internal long/flat flag — the target state is
        # latched per symbol (hysteresis) and the held/flat decision is read from
        # the broker-reconciled context each bar. See on_bar + StrategyContext.
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._highs: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._lows: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        # Latched target state per symbol: True=want long, False=want flat.
        self._want_long: Dict[str, bool] = {s.ticker: False for s in symbols}

    # ---- z-score (private, this module only) -----------------------------

    def _zscore(self, closes: list[float]) -> Optional[float]:
        """
        Rolling z-score of the latest close over the last `lookback` closes:
            z = (close - mean) / pstdev.
        Returns None during warmup (fewer than `lookback` closes) or when stdev is
        zero (a flat window carries no usable mean-reversion signal). Never garbage.
        """
        if len(closes) < self.lookback:
            return None
        window = closes[-self.lookback:]
        mean = statistics.fmean(window)
        sd = statistics.pstdev(window)
        if sd <= 0.0:
            return None
        return (window[-1] - mean) / sd

    # ---- stop-loss (always attached) -------------------------------------

    def _suggested_stop(self, ticker: str, price: Decimal) -> Decimal:
        """
        ATR-based stop (price - atr_mult * ATR) when ATR is available, otherwise a
        percentage fallback during the ATR warmup. Never returns None — every BUY
        must carry a stop (golden rule 7), and the stop is clamped strictly below
        the entry price so it is always protective.
        """
        atr_val: Optional[float] = None
        highs = self._highs[ticker]
        if len(highs) >= self.atr_period + 1:
            atr_series = ind.atr(highs, self._lows[ticker], self._closes[ticker], self.atr_period)
            atr_val = atr_series[-1]

        if atr_val is not None and atr_val > 0.0:
            stop = price - self.atr_mult * Decimal(str(atr_val))
        else:
            stop = price * (Decimal("1") - self.stop_loss_pct)

        # Fail closed: a non-positive or above-entry stop is never protective.
        if stop <= 0 or stop >= price:
            stop = price * (Decimal("1") - self.stop_loss_pct)
        return stop

    # ---- position --------------------------------------------------------

    def _held(self, symbol: Symbol) -> bool:
        """
        True if we ACTUALLY hold a long position in `symbol`, read from the
        broker-reconciled context. With no context bound (isolated use) we treat
        ourselves as flat — the harness always binds and refreshes the context.
        """
        if self.context is None:
            return False
        pos = self.context.get_position(symbol)
        return pos is not None and pos.quantity > 0

    # ---- main hook -------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []  # not a symbol we trade

        self._closes[ticker].append(float(bar.close))
        self._highs[ticker].append(float(bar.high))
        self._lows[ticker].append(float(bar.low))

        # Keep buffers bounded: z-score needs `lookback`, ATR needs atr_period+1.
        max_len = max(self.lookback, self.atr_period + 1) + 5
        for buf in (self._closes[ticker], self._highs[ticker], self._lows[ticker]):
            if len(buf) > max_len:
                del buf[:-max_len]

        z = self._zscore(self._closes[ticker])
        if z is None:
            return []  # warmup or flat window — stay flat, decide nothing

        # Latched target state with a hysteresis dead band: enter on a deep oversold
        # reading, exit once reverted near the mean, hold otherwise. This makes the
        # target a STATE, not a per-bar flip, which the delta logic below acts on.
        if z <= self.entry_z:
            self._want_long[ticker] = True
        elif z >= self.exit_z:
            self._want_long[ticker] = False
        want_long = self._want_long[ticker]

        held = self._held(bar.symbol)
        signals: List[SignalEvent] = []
        price = bar.close

        if want_long and not held:
            stop = self._suggested_stop(ticker, price)
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.BUY,
                    strength=self.strength,
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=stop,
                    timestamp=bar.timestamp,
                    reason=(
                        f"z={z:.2f} <= entry_z={self.entry_z} (oversold, "
                        f"{self.lookback}-bar mean reversion)"
                    ),
                )
            )
        elif held and not want_long:
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=f"z={z:.2f} >= exit_z={self.exit_z} (reverted to mean)",
                )
            )

        return signals
