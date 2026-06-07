"""
apex.strategy.library.roc_momentum
==================================
Rate-of-Change (ROC) absolute-momentum strategy — LONG / FLAT.

RESEARCH CANDIDATE — UNVALIDATED. This strategy has NOT been run through the
Validation Gauntlet (docs/VALIDATION_GAUNTLET.md). It is an idea, not an edge.
Do not allocate capital to it before it clears the gates and the mandatory 30-day
paper gate (CLAUDE.md rule 17). Treat every claim here as a hypothesis.

THE HYPOTHESIS:
  Absolute (time-series) momentum: an instrument that has risen meaningfully over
  a recent lookback tends to keep rising over the near term. We measure the
  trailing total return ("rate of change") over `roc_period` bars and go LONG when
  it exceeds an entry threshold; we go FLAT when it falls back below an exit
  threshold. This is the single-asset cousin of the dual-momentum and trend
  sleeves already in the library — same "ride strength, sidestep weakness" idea,
  expressed through a return threshold rather than a moving-average cross.

  Entry/exit use SEPARATE thresholds (exit <= entry) to create a deliberate
  hysteresis band: once long, we don't bail the instant ROC dips a hair below the
  entry trigger, which cuts whipsaw churn around a flat-but-noisy ROC.

POSITION-AWARENESS (why this is safe on restart):
  Like multi_asset_trend, this strategy holds NO internal long/flat flag. Each bar
  it computes a TARGET state ("long iff ROC >= entry, stay long while ROC > exit")
  and emits only the DELTA against what it ACTUALLY holds, read from the
  broker-reconciled StrategyContext. Consequences:
    - Cold start / restart / missed cron cycle: it enters an already-strong
      instrument with no need to witness a fresh threshold crossing.
    - It NEVER pyramids: held + still-strong emits nothing.
  The strategy is idempotent given the same context + bar history.

STOPS (mandatory — CLAUDE.md rule 7):
  Every BUY carries a `suggested_stop_loss`. Preferred stop is ATR-based
  (entry - atr_mult * ATR), which adapts the stop distance to recent volatility.
  During the ATR warmup (before `atr_period`+1 bars exist) we FALL BACK to a fixed
  percentage stop so a BUY is never emitted without a protective stop. The
  RiskManager still validates/!overrides the stop — this is a suggestion.

CONVENTIONS:
  - Indicator/threshold math is comparative, so it runs in float (matching
    apex.strategy.indicators). Prices and the suggested stop are Decimal (money).
  - Reuses apex.strategy.indicators: `rolling_return` (the ROC) and `atr`.
  - Any other calculation is implemented privately in this module; it does NOT
    import the parallel ind_* research files.
  - Deterministic, no I/O, no wall-clock time, stdlib + existing apex only.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class ROCMomentumStrategy(BaseStrategy):
    """
    Long when rate-of-change over a lookback exceeds a threshold; flat otherwise.

    LONG-ONLY and POSITION-AWARE: targets a state each bar and emits the delta
    against the broker-reconciled holding, so it is correct on a cold start and
    never pyramids.

    Args:
        strategy_id: unique id for this instance.
        symbols: the universe this instance trades.
        roc_period: lookback (in bars) for the rate-of-change (default 20).
        entry_threshold: ROC (fraction, e.g. 0.05 = +5%) at/above which we go long.
        exit_threshold: ROC at/below which we exit (must be <= entry_threshold,
            creating a hysteresis band). Default 0.0 (exit when momentum turns
            non-positive).
        atr_period: lookback for the ATR used to size the protective stop.
        atr_mult: stop distance in ATRs below the entry price.
        stop_loss_pct: percentage stop used as a fallback during ATR warmup.
        strength: fixed conviction for entries (0..1). Absolute momentum here is a
            binary regime call, so conviction is constant; the RiskManager sizes.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        roc_period: int = 20,
        entry_threshold: float = 0.05,
        exit_threshold: float = 0.0,
        atr_period: int = 14,
        atr_mult: float = 2.0,
        stop_loss_pct: Decimal = Decimal("0.05"),
        strength: Decimal = Decimal("1.0"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if roc_period < 1:
            raise ValueError("roc_period must be >= 1")
        if atr_period < 1:
            raise ValueError("atr_period must be >= 1")
        if atr_mult <= 0:
            raise ValueError("atr_mult must be positive")
        if exit_threshold > entry_threshold:
            raise ValueError("exit_threshold must be <= entry_threshold")
        if not (Decimal("0") < strength <= Decimal("1")):
            raise ValueError("strength must be in (0, 1]")
        if not (Decimal("0") < stop_loss_pct < Decimal("1")):
            raise ValueError("stop_loss_pct must be in (0, 1)")
        self.roc_period = roc_period
        self.entry_threshold = float(entry_threshold)
        self.exit_threshold = float(exit_threshold)
        self.atr_period = atr_period
        self.atr_mult = float(atr_mult)
        self.stop_loss_pct = stop_loss_pct
        self.strength = strength
        # Per-symbol rolling OHLC buffers. No long/flat flag is kept here — the
        # real holding is read from the (broker-reconciled) context each bar.
        # See on_bar + StrategyContext.sync_state.
        self._highs: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._lows: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}

    # ---- stop sizing ------------------------------------------------------

    def _suggested_stop(self, ticker: str, price: Decimal) -> Decimal:
        """
        Protective stop below the entry price. Prefers an ATR stop
        (price - atr_mult * ATR); falls back to a fixed-percentage stop during the
        ATR warmup. Always returns a strictly-positive stop below `price` so every
        BUY carries a valid protective stop (CLAUDE.md rule 7).
        """
        pct_stop = price * (Decimal("1") - self.stop_loss_pct)
        atr_series = ind.atr(
            self._highs[ticker],
            self._lows[ticker],
            self._closes[ticker],
            self.atr_period,
        )
        latest_atr = atr_series[-1] if atr_series else None
        if latest_atr is None or latest_atr <= 0:
            return pct_stop
        atr_stop = price - (Decimal(str(latest_atr)) * Decimal(str(self.atr_mult)))
        # Fail closed: an ATR stop at/below zero (extreme vol vs. price) is unusable,
        # so fall back to the percentage stop rather than emit a nonsensical stop.
        if atr_stop <= 0:
            return pct_stop
        return atr_stop

    # ---- position ---------------------------------------------------------

    def _held(self, symbol: Symbol) -> bool:
        """
        True if we ACTUALLY hold a long position in `symbol`, read from the
        broker-reconciled context. With no context bound (isolated use) we treat
        ourselves as flat — the harness binds and refreshes the context before
        dispatch.
        """
        if self.context is None:
            return False
        pos = self.context.get_position(symbol)
        return pos is not None and pos.quantity > 0

    # ---- main hook --------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []  # not a symbol we trade

        highs = self._highs[ticker]
        lows = self._lows[ticker]
        closes = self._closes[ticker]
        highs.append(float(bar.high))
        lows.append(float(bar.low))
        closes.append(float(bar.close))

        # Keep buffers bounded: need roc_period+1 closes for ROC and atr_period+1
        # bars for ATR, plus a little slack.
        max_len = max(self.roc_period, self.atr_period) + 5
        if len(closes) > max_len:
            del highs[:-max_len]
            del lows[:-max_len]
            del closes[:-max_len]

        # Warmup: need roc_period+1 closes to compute the rate of change.
        roc = ind.rolling_return(closes, self.roc_period)[-1]
        if roc is None:
            return []

        # Decide on STATE, not on a threshold-crossing EVENT. The hysteresis band:
        #   - if flat:  target long iff ROC >= entry_threshold
        #   - if held:  stay long while ROC > exit_threshold, else exit
        # Emitting the delta against the actual holding makes this idempotent: it
        # enters an already-strong instrument on a cold start and never pyramids.
        held = self._held(bar.symbol)
        if held:
            want_long = roc > self.exit_threshold
        else:
            want_long = roc >= self.entry_threshold

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
                    reason=(f"ROC{self.roc_period}={roc:.4f} >= entry {self.entry_threshold:.4f}"),
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
                    reason=(f"ROC{self.roc_period}={roc:.4f} <= exit {self.exit_threshold:.4f}"),
                )
            )

        return signals
