"""
apex.strategy.library.bollinger_breakout
=========================================
Bollinger Band breakout (volatility breakout), LONG-ONLY — RESEARCH CANDIDATE.

⚠️  UNVALIDATED RESEARCH CANDIDATE. This strategy has NOT been run through the
    Gauntlet (docs/VALIDATION_GAUNTLET.md). It is a hypothesis, not a deployable
    edge. Do not allocate capital until it clears the validation gates and the
    mandatory 30-day paper gate (CLAUDE.md rule 17). Breakout entries are
    notoriously prone to whipsaw and overfitting; treat the numbers with
    suspicion.

THE HYPOTHESIS:
  A close that breaks ABOVE the upper Bollinger Band marks an expansion out of a
  quiet range — momentum that may continue. We go long on that break and give up
  the position when price mean-reverts back to the middle band (the SMA), which we
  treat as the trade's exhaustion point. Long/flat only; no shorting.

THE RULES (long/flat, per symbol):
  - Maintain a rolling close buffer (and high/low buffers for ATR) per symbol.
  - Compute Bollinger Bands (middle = SMA(period), bands = num_std population
    stdev) from apex.strategy.indicators.bollinger_bands.
  - TARGET STATE each bar:
        want_long = close > upper_band                 (a fresh break, or still
                    OR (already held AND close > middle) extended above the mean)
    i.e. enter when price pierces the upper band; stay long while it holds above
    the middle band; exit (go flat) once it falls back to / below the middle band.
  - Emit the DELTA toward that target against the ACTUAL broker-reconciled holding
    read from the StrategyContext — exactly like multi_asset_trend. This makes it
    correct on a cold start / restart / missed cron cycle and means it NEVER
    pyramids (held + still-want-long emits nothing).
  - Every BUY carries a suggested_stop_loss: ATR-based once ATR has warmed up
    (entry - atr_mult * ATR), with a percentage fallback (entry * (1 - stop_loss_pct))
    during ATR warmup so no order is ever stop-less (CLAUDE.md rule 7).

WHY STATE, NOT EVENT: deciding "what should I hold now" rather than "did I just
see a band-cross in this replay window" is what survives restarts. On a fresh
process that boots mid-breakout with price still above the band and a flat book,
the strategy re-enters; with price back below the middle and a long on the books,
it exits. No reliance on having observed the crossing tick.

Determinism: no wall-clock time (bar timestamps are used/echoed), no randomness,
pure float indicator math (money/stops stay Decimal per the layer convention).
No I/O. Stdlib + existing apex modules only.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class BollingerBreakoutStrategy(BaseStrategy):
    """
    Long-only Bollinger Band breakout — UNVALIDATED research candidate.

    Args:
        strategy_id: unique id for this instance.
        symbols: the universe to trade.
        period: Bollinger / SMA lookback (default 20).
        num_std: band width in population standard deviations (default 2.0).
        atr_period: ATR lookback for the protective stop (default 14).
        atr_mult: ATR multiple for the stop distance (default 2.0).
        stop_loss_pct: percentage stop fallback used during ATR warmup (default 5%).
        strength: signal conviction passed to the RiskManager for sizing.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        period: int = 20,
        num_std: float = 2.0,
        atr_period: int = 14,
        atr_mult: Decimal = Decimal("2.0"),
        stop_loss_pct: Decimal = Decimal("0.05"),
        strength: Decimal = Decimal("1.0"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if period < 2:
            raise ValueError("period must be >= 2")
        if num_std <= 0:
            raise ValueError("num_std must be positive")
        if atr_period < 1:
            raise ValueError("atr_period must be >= 1")
        if atr_mult <= 0:
            raise ValueError("atr_mult must be positive")
        if not (Decimal("0") < stop_loss_pct < Decimal("1")):
            raise ValueError("stop_loss_pct must be in (0, 1)")
        self.period = period
        self.num_std = num_std
        self.atr_period = atr_period
        self.atr_mult = atr_mult
        self.stop_loss_pct = stop_loss_pct
        self.strength = strength
        # Per-symbol rolling OHLC buffers. NOTE: like multi_asset_trend, this
        # strategy holds NO internal long/flat flag — it reads its real position
        # from the (broker-reconciled) context each bar, so it is correct across
        # cold starts and restarts. See on_bar + StrategyContext.sync_state.
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._highs: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._lows: Dict[str, list[float]] = {s.ticker: [] for s in symbols}

    # ---- stop loss --------------------------------------------------------

    def _suggested_stop(self, ticker: str, entry: Decimal) -> Decimal:
        """
        Protective stop for a BUY at `entry`. ATR-based once ATR has warmed up
        (entry - atr_mult * ATR), else a percentage fallback. ALWAYS returns a
        stop strictly below entry so every order carries one (CLAUDE.md rule 7).
        """
        atr_val = self._latest_atr(ticker)
        if atr_val is not None and atr_val > 0:
            stop = entry - self.atr_mult * Decimal(str(atr_val))
            # Guard against a pathological ATR that would push the stop to/above
            # entry (or non-positive): fall back to the percentage stop.
            if stop > 0 and stop < entry:
                return stop
        return entry * (Decimal("1") - self.stop_loss_pct)

    def _latest_atr(self, ticker: str) -> Optional[float]:
        """Most recent ATR value for `ticker`, or None during warmup."""
        highs = self._highs[ticker]
        lows = self._lows[ticker]
        closes = self._closes[ticker]
        if len(closes) < self.atr_period + 1:
            return None
        series = ind.atr(highs, lows, closes, self.atr_period)
        return series[-1] if series else None

    # ---- position ---------------------------------------------------------

    def _held(self, symbol: Symbol) -> bool:
        """
        True if we ACTUALLY hold a long position in `symbol`, read from the
        broker-reconciled context. With no context bound (isolated use) we treat
        ourselves as flat — the harness always binds/refreshes context first.
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

        closes = self._closes[ticker]
        highs = self._highs[ticker]
        lows = self._lows[ticker]
        closes.append(float(bar.close))
        highs.append(float(bar.high))
        lows.append(float(bar.low))

        # Keep buffers bounded: need `period` for the bands and atr_period+1 for
        # ATR, plus a little slack.
        max_len = max(self.period, self.atr_period + 1) + 5
        if len(closes) > max_len:
            del closes[:-max_len]
            del highs[:-max_len]
            del lows[:-max_len]

        # Warmup: need `period` closes for the Bollinger Bands.
        if len(closes) < self.period:
            return []

        upper, middle, _lower = ind.bollinger_bands(closes, self.period, self.num_std)
        up = upper[-1]
        mid = middle[-1]
        if up is None or mid is None:
            return []

        close_f = float(bar.close)
        held = self._held(bar.symbol)

        # TARGET STATE (state, not event):
        #   - enter on a break above the upper band;
        #   - while already long, hold as long as price stays above the middle band;
        #   - exit (go flat) once price returns to / below the middle band.
        if held:
            want_long = close_f > mid
        else:
            want_long = close_f > up

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
                        f"close {close_f:.4f} broke above upper BB "
                        f"({self.period},{self.num_std}) {up:.4f}"
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
                    reason=(
                        f"close {close_f:.4f} reverted to/below middle BB "
                        f"{mid:.4f}"
                    ),
                )
            )

        return signals
