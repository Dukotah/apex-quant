"""
apex.strategy.library.macd_trend
================================
MACD trend-following entry/exit — an UNVALIDATED RESEARCH CANDIDATE.

>>> WARNING <<<
This strategy has NOT been run through the Gauntlet. It has no walk-forward,
Monte-Carlo, or real-data validation behind it. It is a research idea expressed
as code so it CAN be validated — do not deploy it (paper or live) until it has
cleared the validation gates. Treat any backtest of it as exploratory.

THE IDEA (long/flat, single name or per-symbol on a universe):
  - Go LONG when the MACD line is above its signal line AND the histogram is
    positive (macd_line > signal_line). That is the "bullish MACD" regime — the
    classic bullish crossover, held while the regime persists.
  - EXIT (go flat) on a bearish state: MACD line at/below its signal line.

POSITION-AWARENESS (mirrors multi_asset_trend):
  The strategy decides a TARGET STATE (long iff bullish MACD regime) and emits
  the DELTA toward that target against what it ACTUALLY holds, read from the
  broker-reconciled StrategyContext each bar. It holds NO internal long/flat
  flag. This makes it correct on a cold start, a restart, or a missed cron
  cycle: it will enter an already-bullish name without needing to witness a
  fresh crossover in the replay window, and it never pyramids (held + still
  bullish emits nothing). LONG-ONLY: it never emits a short.

STOPS (rule 7 — every BUY carries a stop):
  Each BUY attaches a `suggested_stop_loss`. While ATR is still warming up
  (insufficient bars for a `period`-length true-range average) it falls back to
  a percentage stop, so a BUY ALWAYS carries a protective stop. Once ATR is
  available the stop is `close - atr_multiple * ATR`, clamped to never sit at or
  above the entry price (and never negative). The RiskManager validates/sizes.

DETERMINISM & PURITY:
  No look-ahead — every decision uses only bars seen up to and including the
  current bar (indicators read the final element of the series). No I/O, no
  wall-clock time (the bar's timestamp is passed through), no randomness.
  Indicator math reuses apex.strategy.indicators (macd, atr). Money/price math
  stays Decimal; indicator inputs are floats per the indicators-layer contract.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class MacdTrendStrategy(BaseStrategy):
    """
    Long-only MACD trend follower (UNVALIDATED research candidate).

    Args:
        strategy_id: unique id for this instance.
        symbols: the universe to trade (each name handled independently).
        fast_period: fast EMA lookback for the MACD line (default 12).
        slow_period: slow EMA lookback for the MACD line (default 26).
        signal_period: EMA lookback for the MACD signal line (default 9).
        atr_period: lookback for the ATR-based stop (default 14).
        atr_multiple: ATR multiples below entry for the suggested stop (default 3).
        stop_loss_pct: percentage stop used as a fallback during ATR warmup
            (default 0.05 = 5%).
        strength: signal conviction passed to the RiskManager (default 1.0).
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
        atr_period: int = 14,
        atr_multiple: Decimal = Decimal("3"),
        stop_loss_pct: Decimal = Decimal("0.05"),
        strength: Decimal = Decimal("1.0"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if fast_period <= 0 or slow_period <= 0 or signal_period <= 0:
            raise ValueError("MACD periods must be positive")
        if fast_period >= slow_period:
            raise ValueError("fast_period must be < slow_period")
        if atr_period <= 0:
            raise ValueError("atr_period must be positive")
        if atr_multiple <= 0:
            raise ValueError("atr_multiple must be positive")
        if stop_loss_pct <= 0 or stop_loss_pct >= 1:
            raise ValueError("stop_loss_pct must be in (0, 1)")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period
        self.atr_period = atr_period
        self.atr_multiple = atr_multiple
        self.stop_loss_pct = stop_loss_pct
        self.strength = strength
        # Per-symbol rolling OHLC buffers. NOTE: NO internal long/flat flag — the
        # real position is read from the (broker-reconciled) context each bar so
        # the strategy is correct on restart and never pyramids. See on_bar.
        self._highs: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._lows: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}

    # ---- regime ----------------------------------------------------------

    def _bullish_macd(self, closes: list[float]) -> Optional[bool]:
        """
        Target-state test: True if the latest bar is in a bullish MACD regime
        (macd_line > signal_line, i.e. a positive histogram). Returns None while
        the MACD/signal lines are still warming up (not enough history). No
        look-ahead: only the final element of each series is read.
        """
        macd_line, signal_line, _hist = ind.macd(
            closes, self.fast_period, self.slow_period, self.signal_period
        )
        m = macd_line[-1]
        s = signal_line[-1]
        if m is None or s is None:
            return None
        return m > s

    # ---- stop ------------------------------------------------------------

    def _suggested_stop(self, ticker: str, price: Decimal) -> Decimal:
        """
        Protective stop for a long entry at `price`. Uses an ATR-distance stop
        once ATR is available, otherwise a percentage fallback — so a BUY ALWAYS
        carries a stop (rule 7). The stop is clamped strictly below the entry
        price and above zero.
        """
        pct_stop = price * (Decimal("1") - self.stop_loss_pct)
        atr_series = ind.atr(
            self._highs[ticker],
            self._lows[ticker],
            self._closes[ticker],
            self.atr_period,
        )
        atr_val = atr_series[-1]
        if atr_val is None or atr_val <= 0:
            stop = pct_stop
        else:
            stop = price - self.atr_multiple * Decimal(str(atr_val))
        # Fail closed: never let the stop sit at/above entry or go non-positive.
        if stop >= price:
            stop = pct_stop
        if stop <= 0:
            # Last-resort floor: a tight stop just below entry.
            stop = price * (Decimal("1") - self.stop_loss_pct)
            if stop <= 0:
                stop = price / Decimal("2")
        return stop

    # ---- position --------------------------------------------------------

    def _held(self, symbol: Symbol) -> bool:
        """
        True if we ACTUALLY hold a long position, read from the broker-reconciled
        context. With no context bound (isolated use) we treat ourselves as flat;
        the harness binds and refreshes the context before each dispatch.
        """
        if self.context is None:
            return False
        pos = self.context.get_position(symbol)
        return pos is not None and pos.quantity > 0

    # ---- main hook -------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []  # not a name we trade

        self._highs[ticker].append(float(bar.high))
        self._lows[ticker].append(float(bar.low))
        self._closes[ticker].append(float(bar.close))

        # Keep buffers bounded: MACD needs ~slow_period + signal_period of
        # history; ATR needs atr_period + 1. Keep the max plus slack.
        max_len = max(self.slow_period + self.signal_period, self.atr_period + 1) + 5
        for buf in (self._highs[ticker], self._lows[ticker], self._closes[ticker]):
            if len(buf) > max_len:
                del buf[:-max_len]

        bullish = self._bullish_macd(self._closes[ticker])
        if bullish is None:
            return []  # warmup

        held = self._held(bar.symbol)
        signals: List[SignalEvent] = []
        price = bar.close

        if bullish and not held:
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
                        f"MACD bullish ({self.fast_period}/{self.slow_period}/"
                        f"{self.signal_period}): macd>signal, +histogram"
                    ),
                )
            )
        elif held and not bullish:
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=(
                        f"MACD bearish ({self.fast_period}/{self.slow_period}/"
                        f"{self.signal_period}): macd<=signal, exit long"
                    ),
                )
            )

        return signals
