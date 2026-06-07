"""
apex.strategy.library.volatility_breakout
==========================================
Volatility-Breakout (ATR channel) — LONG-ONLY research candidate.

THE IDEA (classic volatility breakout, Wilder/turtle lineage):
  A bar's close that pushes ABOVE the *prior* close plus a multiple of recent
  volatility (ATR) is treated as a momentum thrust worth riding. The breakout
  threshold floats with volatility, so the same logic adapts across calm and
  wild regimes instead of using a fixed price distance.

      breakout_level = prior_close + k * ATR(atr_period)
      go long  iff  close > breakout_level

  The position is exited the EARLIER of:
    - a TIME stop: held for `max_hold_bars` bars (breakout momentum is meant to
      be short-lived; we don't want to marry a stale trade), or
    - an ATR/trail give-back: the close falls a multiple of ATR below the highest
      close seen since entry (close <= peak_close - exit_atr_mult * ATR). This is
      a volatility-scaled trailing exit, distinct from the protective hard stop
      the RiskManager enforces on the BUY's suggested_stop_loss.

  EVERY BUY carries a `suggested_stop_loss`: an ATR-based stop while ATR is warm
  (entry_close - stop_atr_mult * ATR), with a fixed PERCENTAGE fallback during
  the ATR warmup so no order is ever emitted without a stop (golden rule 7).

POSITION-AWARENESS (why this is restart-safe, like multi_asset_trend):
  The strategy holds NO authoritative long/flat flag. Each bar it reads its
  ACTUAL holding from the broker-reconciled StrategyContext and emits the DELTA
  toward a target state:
      flat  + breakout      -> BUY  (enter)
      long  + exit signal   -> SELL (full exit, never partial, never pyramids)
      long  + breakout      -> nothing (already long; do not add)
  Because the decision is "what do I hold vs. what should I hold", a cold start,
  a restart, or a missed cron cycle can never double-enter or strand a position.
  An internal `_entry_idx` tracks the entry bar for the TIME stop, but it is only
  an OPTIMISATION: if we find ourselves long with no recorded entry (e.g. the
  process restarted mid-trade), the time stop is simply skipped and the ATR
  trailing exit still protects the position. We fail toward "stay protected,"
  never toward a bogus immediate exit.

NO LOOK-AHEAD: the breakout test compares the just-closed bar against the PRIOR
close and an ATR computed only from bars up to and including the current one.
Indicators come from apex.strategy.indicators (ATR); the small amount of extra
bookkeeping (prior close, peak-since-entry, bar counting) is kept PRIVATE here.

STATUS: UNVALIDATED RESEARCH CANDIDATE. This strategy has NOT been run through
the Validation Gauntlet. Volatility-breakout systems are notoriously regime- and
cost-sensitive (whipsaw in chop, slippage on the thrust bar). Do not allocate
capital to it before it clears the gates and the mandatory 30-day paper gate.

Deterministic, no I/O, stdlib + existing apex modules only.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class VolatilityBreakoutStrategy(BaseStrategy):
    """
    ATR-channel volatility breakout, long/flat per symbol.

    Args:
        strategy_id: unique id for this instance.
        symbols: the universe to trade.
        atr_period: lookback for ATR (default 14). Needs atr_period+1 bars warm.
        k: breakout multiple — long iff close > prior_close + k*ATR (default 1.0).
        max_hold_bars: TIME stop; force exit after holding this many bars
            (counted from the entry bar). Set <= 0 to disable the time stop.
        exit_atr_mult: ATR-trailing give-back from the peak close since entry that
            triggers an exit (default 2.0). Set <= 0 to disable the trailing exit.
        stop_atr_mult: ATR multiple below entry close for the suggested_stop_loss
            attached to every BUY (default 2.0).
        stop_loss_pct: PERCENTAGE fallback stop distance used when ATR is still
            warming up, so every BUY always carries a stop (default 0.05 = 5%).
        strength: conviction reported on entry signals (informs RiskManager sizing).
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        atr_period: int = 14,
        k: float = 1.0,
        max_hold_bars: int = 10,
        exit_atr_mult: float = 2.0,
        stop_atr_mult: float = 2.0,
        stop_loss_pct: Decimal = Decimal("0.05"),
        strength: Decimal = Decimal("1.0"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if atr_period < 1:
            raise ValueError("atr_period must be >= 1")
        if k <= 0:
            raise ValueError("k must be positive")
        if stop_atr_mult <= 0:
            raise ValueError("stop_atr_mult must be positive")
        if stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be positive")
        self.atr_period = atr_period
        self.k = k
        self.max_hold_bars = max_hold_bars
        self.exit_atr_mult = exit_atr_mult
        self.stop_atr_mult = stop_atr_mult
        self.stop_loss_pct = stop_loss_pct
        self.strength = strength

        tickers = [s.ticker for s in symbols]
        # Per-symbol rolling OHLC buffers (for ATR + the prior-close compare).
        self._highs: Dict[str, list[float]] = {t: [] for t in tickers}
        self._lows: Dict[str, list[float]] = {t: [] for t in tickers}
        self._closes: Dict[str, list[float]] = {t: [] for t in tickers}
        # Bar counter per symbol and the entry bar index (for the TIME stop).
        # _entry_idx is best-effort and may be None after a restart; see module docs.
        self._bar_idx: Dict[str, int] = {t: 0 for t in tickers}
        self._entry_idx: Dict[str, Optional[int]] = {t: None for t in tickers}
        # Highest close observed since entry (for the ATR trailing exit).
        self._peak_close: Dict[str, Optional[float]] = {t: None for t in tickers}

    # ---- volatility ------------------------------------------------------

    def _current_atr(self, ticker: str) -> Optional[float]:
        """Latest ATR value from the buffers, or None during warmup."""
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
        True iff we ACTUALLY hold a long position in `symbol`, read from the
        broker-reconciled context. With no context bound (isolated use) we treat
        ourselves as flat — the harness always binds and refreshes the context
        before dispatch.
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

        self._bar_idx[ticker] += 1
        idx = self._bar_idx[ticker]

        highs = self._highs[ticker]
        lows = self._lows[ticker]
        closes = self._closes[ticker]

        prior_close = closes[-1] if closes else None  # PRIOR bar's close (no look-ahead)

        highs.append(float(bar.high))
        lows.append(float(bar.low))
        closes.append(float(bar.close))

        # Keep buffers bounded: ATR needs atr_period+1 bars; keep a little slack.
        max_len = self.atr_period + 5
        for buf in (highs, lows, closes):
            if len(buf) > max_len:
                del buf[:-max_len]

        held = self._held(bar.symbol)

        # Maintain peak-since-entry only while we believe we are long.
        if held:
            close_f = float(bar.close)
            peak = self._peak_close[ticker]
            self._peak_close[ticker] = close_f if peak is None else max(peak, close_f)
        else:
            # Flat: clear any stale per-trade bookkeeping.
            self._entry_idx[ticker] = None
            self._peak_close[ticker] = None

        atr_val = self._current_atr(ticker)

        if not held:
            return self._maybe_enter(bar, ticker, idx, prior_close, atr_val)
        return self._maybe_exit(bar, ticker, idx, atr_val)

    # ---- entry / exit decomposed -----------------------------------------

    def _maybe_enter(
        self,
        bar: Bar,
        ticker: str,
        idx: int,
        prior_close: Optional[float],
        atr_val: Optional[float],
    ) -> List[SignalEvent]:
        # Need a prior close and a warm ATR to define the breakout level.
        if prior_close is None or atr_val is None or atr_val <= 0:
            return []
        close_f = float(bar.close)
        breakout_level = prior_close + self.k * atr_val
        if close_f <= breakout_level:
            return []

        # Breakout: enter long. Record entry bookkeeping for the time/trail exits.
        self._entry_idx[ticker] = idx
        self._peak_close[ticker] = close_f

        stop = self._suggested_stop(bar.close, atr_val)
        return [
            SignalEvent(
                symbol=bar.symbol,
                side=OrderSide.BUY,
                strength=self.strength,
                strategy_id=self.strategy_id,
                suggested_stop_loss=stop,
                timestamp=bar.timestamp,
                reason=(
                    f"close {close_f:.4f} > prior_close+{self.k}*ATR "
                    f"({breakout_level:.4f}); ATR breakout entry"
                ),
            )
        ]

    def _maybe_exit(
        self,
        bar: Bar,
        ticker: str,
        idx: int,
        atr_val: Optional[float],
    ) -> List[SignalEvent]:
        reason = self._exit_reason(ticker, idx, float(bar.close), atr_val)
        if reason is None:
            return []
        # Exiting: clear per-trade bookkeeping (also reset on the next flat bar).
        self._entry_idx[ticker] = None
        self._peak_close[ticker] = None
        return [
            SignalEvent(
                symbol=bar.symbol,
                side=OrderSide.SELL,
                strength=Decimal("1.0"),
                strategy_id=self.strategy_id,
                timestamp=bar.timestamp,
                reason=reason,
            )
        ]

    def _exit_reason(
        self,
        ticker: str,
        idx: int,
        close_f: float,
        atr_val: Optional[float],
    ) -> Optional[str]:
        """Return a human-readable exit reason, or None to stay long."""
        # TIME stop (skipped if we have no recorded entry, e.g. after a restart).
        entry = self._entry_idx[ticker]
        if self.max_hold_bars > 0 and entry is not None:
            if idx - entry >= self.max_hold_bars:
                return f"time stop: held {idx - entry} bars >= {self.max_hold_bars}"

        # ATR trailing give-back from the peak close since entry.
        if self.exit_atr_mult > 0 and atr_val is not None and atr_val > 0:
            peak = self._peak_close[ticker]
            if peak is not None:
                trail_level = peak - self.exit_atr_mult * atr_val
                if close_f <= trail_level:
                    return (
                        f"ATR trail exit: close {close_f:.4f} <= peak-"
                        f"{self.exit_atr_mult}*ATR ({trail_level:.4f})"
                    )
        return None

    # ---- stop sizing -----------------------------------------------------

    def _suggested_stop(self, entry_close: Decimal, atr_val: Optional[float]) -> Decimal:
        """
        ATR-based protective stop attached to every BUY:
            stop = entry_close - stop_atr_mult * ATR
        Falls back to a fixed percentage stop while ATR is warming up so a BUY is
        NEVER emitted without a stop. The stop is clamped above zero (fail closed:
        a non-positive stop would be rejected by the RiskManager anyway).
        """
        if atr_val is not None and atr_val > 0:
            stop = entry_close - Decimal(str(self.stop_atr_mult)) * Decimal(str(atr_val))
        else:
            stop = entry_close * (Decimal("1") - self.stop_loss_pct)
        if stop <= 0:
            # Degenerate ATR vs. price: fall back to the percentage stop.
            stop = entry_close * (Decimal("1") - self.stop_loss_pct)
        return stop
