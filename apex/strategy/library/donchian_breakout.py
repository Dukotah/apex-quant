"""
apex.strategy.library.donchian_breakout
========================================
Donchian Channel Breakout (turtle-style) — LONG-ONLY research candidate.

This is the classic Dennis/Eckhardt "turtle" trend entry, stripped to its core:

  - ENTER LONG when price breaks out ABOVE the highest high of the prior
    `entry_period` bars (an N-day high breakout).
  - EXIT (back to flat) when price breaks DOWN through the lowest low of the
    prior `exit_period` bars (an M-day low). The shorter exit channel
    (M < N) gives back less of an established trend than a symmetric channel.

⚠️  UNVALIDATED RESEARCH CANDIDATE. This strategy has NOT been run through the
    Gauntlet (see docs/VALIDATION_GAUNTLET.md). Treat any backtest of it as a
    hypothesis, not an edge. Do NOT allocate capital on it until it has cleared
    walk-forward + Monte-Carlo validation and the mandatory 30-day paper gate.

POSITION-AWARENESS (why this is correct on restart and never pyramids):
  Like `multi_asset_trend`, this strategy holds NO internal long/flat flag.
  Each bar it derives a TARGET STATE purely from the rolling OHLC history, then
  reads what it ACTUALLY holds from the broker-reconciled StrategyContext and
  emits only the DELTA toward that target. The target is: "long iff the most
  recent entry-breakout is more recent than the most recent exit-breakdown."
  Because that decision is recomputed from history every bar (not latched from a
  single fresh cross), a cold start / restart / missed cron cycle re-derives the
  same answer and:
    - enters an already-broken-out trend without needing to witness the cross,
    - never pyramids (already long + still-long target → emits nothing),
    - exits idempotently once the M-day low is taken out.

STOPS: every BUY carries a `suggested_stop_loss`. Preferred basis is an ATR
  stop (close - atr_mult * ATR) using the tested `apex.strategy.indicators.atr`.
  During ATR warmup (insufficient bars) it FALLS BACK to a percentage stop so
  no BUY is ever emitted without a protective stop (Golden Rule 7). The
  RiskManager remains the sole sizer and validates/limits the stop.

No look-ahead: the entry/exit channels are computed from bars STRICTLY PRIOR to
the current bar; the current close is then compared against those prior extremes.

Deterministic, no I/O, stdlib + existing apex.strategy.indicators only — safe on
the free CI runner.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class DonchianBreakoutStrategy(BaseStrategy):
    """
    Long/flat Donchian channel breakout with an ATR-based protective stop.

    Args:
        strategy_id: unique id for this instance.
        symbols: the tradeable universe.
        entry_period: lookback (N) for the entry high channel (default 20).
        exit_period: lookback (M) for the exit low channel (default 10).
            Must satisfy exit_period <= entry_period (turtle convention).
        atr_period: lookback for the ATR used to place the stop (default 20).
        atr_mult: ATR multiples below close for the suggested stop (default 2.0).
        stop_loss_pct: percentage stop used as a FALLBACK while ATR is warming
            up, so every BUY always carries a stop (default 0.05 = 5%).
        strength: conviction (0..1) attached to BUY signals; the RiskManager
            multiplies the position cap by this (default 1.0).
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        entry_period: int = 20,
        exit_period: int = 10,
        atr_period: int = 20,
        atr_mult: Decimal = Decimal("2.0"),
        stop_loss_pct: Decimal = Decimal("0.05"),
        strength: Decimal = Decimal("1.0"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if entry_period < 1 or exit_period < 1:
            raise ValueError("entry_period and exit_period must be >= 1")
        if exit_period > entry_period:
            raise ValueError("exit_period must be <= entry_period")
        if atr_period < 1:
            raise ValueError("atr_period must be >= 1")
        if atr_mult <= 0:
            raise ValueError("atr_mult must be positive")
        if stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be positive")
        if strength <= 0:
            raise ValueError("strength must be positive")
        self.entry_period = entry_period
        self.exit_period = exit_period
        self.atr_period = atr_period
        self.atr_mult = atr_mult
        self.stop_loss_pct = stop_loss_pct
        self.strength = strength

        # Per-symbol rolling OHLC buffers. Like multi_asset_trend, NO internal
        # position flag is kept — the real holding comes from the context each bar.
        self._highs: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._lows: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}

    # ---- donchian channels (private; no parallel ind_* imports) ----------

    @staticmethod
    def _prior_high(highs: list[float], period: int) -> Optional[float]:
        """
        Highest high of the `period` bars STRICTLY BEFORE the latest bar.
        Returns None until `period` prior bars exist (no look-ahead, no garbage).
        """
        # Exclude the current (last) bar -> prior window is highs[-(period+1):-1].
        if len(highs) < period + 1:
            return None
        return max(highs[-(period + 1):-1])

    @staticmethod
    def _prior_low(lows: list[float], period: int) -> Optional[float]:
        """Lowest low of the `period` bars STRICTLY BEFORE the latest bar."""
        if len(lows) < period + 1:
            return None
        return min(lows[-(period + 1):-1])

    # ---- target state ----------------------------------------------------

    def _target_long(self, ticker: str) -> Optional[bool]:
        """
        Derive the desired long/flat state from history alone (restart-safe).

        Walk the bars and, for each, mark an entry-breakout (close > prior N-high)
        or an exit-breakdown (close < prior M-low). The target is "long" iff the
        most recent entry-breakout is strictly more recent than the most recent
        exit-breakdown. Returns None until enough history exists to evaluate the
        entry channel (warmup) — caller treats None as "no opinion -> hold".
        """
        highs = self._highs[ticker]
        lows = self._lows[ticker]
        closes = self._closes[ticker]
        n = len(closes)
        # Need at least entry_period prior bars + 1 current bar to ever fire.
        if n < self.entry_period + 1:
            return None

        last_entry_idx = -1   # most recent index of an N-high breakout
        last_exit_idx = -1    # most recent index of an M-low breakdown
        for i in range(self.entry_period, n):
            # Prior windows END at i-1 (strictly before bar i): no look-ahead.
            entry_high = max(highs[i - self.entry_period: i])
            if closes[i] > entry_high:
                last_entry_idx = i
            # Exit channel only meaningful once exit_period prior bars exist.
            if i >= self.exit_period:
                exit_low = min(lows[i - self.exit_period: i])
                if closes[i] < exit_low:
                    last_exit_idx = i

        if last_entry_idx < 0:
            return False  # never broken out -> flat
        return last_entry_idx > last_exit_idx

    # ---- stop placement --------------------------------------------------

    def _suggested_stop(self, ticker: str, close: Decimal) -> Decimal:
        """
        ATR stop (close - atr_mult * ATR) when ATR is available; otherwise a
        percentage fallback. Guaranteed to return a positive stop below `close`
        so every BUY carries a protective stop.
        """
        atr_val = self._latest_atr(ticker)
        if atr_val is not None and atr_val > 0:
            stop = close - self.atr_mult * Decimal(str(atr_val))
            # Never let an oversized ATR push the stop to/below zero; if it would,
            # fall through to the percentage stop instead (fail closed, but armed).
            if stop > 0:
                return stop
        return close * (Decimal("1") - self.stop_loss_pct)

    def _latest_atr(self, ticker: str) -> Optional[float]:
        """Latest ATR via the tested indicator lib; None during warmup."""
        highs = self._highs[ticker]
        lows = self._lows[ticker]
        closes = self._closes[ticker]
        if len(closes) < self.atr_period + 1:
            return None
        series = ind.atr(highs, lows, closes, self.atr_period)
        return series[-1] if series else None

    # ---- position --------------------------------------------------------

    def _held(self, symbol: Symbol) -> bool:
        """
        True iff we ACTUALLY hold a long position, read from the broker-reconciled
        context. With no context bound (isolated use) we treat ourselves as flat;
        the harness always binds + refreshes the context before dispatch.
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

        self._highs[ticker].append(float(bar.high))
        self._lows[ticker].append(float(bar.low))
        self._closes[ticker].append(float(bar.close))

        # Bound the buffers: need the longest lookback plus ATR's extra prior bar.
        max_len = max(self.entry_period, self.exit_period, self.atr_period) + 2
        if len(self._closes[ticker]) > max_len:
            del self._highs[ticker][:-max_len]
            del self._lows[ticker][:-max_len]
            del self._closes[ticker][:-max_len]

        target = self._target_long(ticker)
        if target is None:
            return []  # warmup — no opinion, hold whatever we have

        held = self._held(bar.symbol)
        signals: List[SignalEvent] = []
        price = bar.close

        if target and not held:
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
                        f"Donchian {self.entry_period}-day high breakout; "
                        f"stop {stop}"
                    ),
                )
            )
        elif held and not target:
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=f"Donchian {self.exit_period}-day low breakdown — exit",
                )
            )

        return signals
