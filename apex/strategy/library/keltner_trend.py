"""
apex.strategy.library.keltner_trend
===================================
Keltner Channel breakout trend follower (LONG-ONLY).  ** UNVALIDATED RESEARCH
CANDIDATE — this strategy has NOT been through the Gauntlet. Do not allocate real
capital to it; it exists to be backtested and falsified. **

THE IDEA (a volatility-breakout trend entry):
  A Keltner Channel is an EMA "middle line" wrapped by bands set a multiple of the
  Average True Range away:

      middle = EMA(close, ema_period)
      upper  = middle + atr_mult * ATR(high, low, close, atr_period)
      lower  = middle - atr_mult * ATR(...)          (unused here — long-only)

  We go long when price thrusts ABOVE the upper band (a volatility-confirmed
  breakout: the move is large relative to recent true range, not noise), and we
  exit when price falls back BELOW the middle EMA (the trend has lost its footing).

WHY POSITION-AWARE (mirrors multi_asset_trend):
  The strategy holds NO internal long/flat flag. Each bar it computes a TARGET
  state — "long iff we are above the upper band OR already long and still above the
  middle line" — and emits only the DELTA against what it ACTUALLY holds, read from
  the broker-reconciled StrategyContext. That makes it correct on a cold start, a
  restart, or a missed cron cycle (it re-enters an established breakout instead of
  waiting for a fresh band-cross it will never see in a partial replay window), and
  it can NEVER pyramid (held + still-in-trend emits nothing). See on_bar +
  StrategyContext.sync_state.

  The target uses HYSTERESIS: the ENTRY trigger is close > upper band, but once
  long the EXIT trigger is close < middle EMA. The band between middle and upper is
  a hold zone, so we don't churn on every poke of the upper band.

STOPS (golden rule 7 — every BUY carries a stop):
  Every BUY attaches a `suggested_stop_loss`. Preferred stop is ATR-based
  (entry - atr_stop_mult * ATR); during the ATR warmup window (before ATR exists)
  it falls back to a fixed percentage stop (entry * (1 - stop_loss_pct)). Either
  way a stop is ALWAYS present, so the RiskManager never rejects for a missing stop.

Indicators: EMA + ATR come from apex.strategy.indicators (the one tested source of
truth). No look-ahead — every value is computed from closed bars only, and the band
compared on bar t uses the EMA/ATR through bar t. Deterministic, no I/O, stdlib +
existing-apex only — safe on the free CI runner.

NOTE: indicators work in float (comparative math); money/prices the strategy hands
back to the risk layer (the suggested stop) stay Decimal, per the layer convention.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class KeltnerTrendStrategy(BaseStrategy):
    """
    Long-only Keltner Channel breakout trend follower (research candidate).

    Args:
        strategy_id: unique id for this instance.
        symbols: the tradeable universe.
        ema_period: lookback for the middle-line EMA (default 20).
        atr_period: lookback for the ATR that sets the band width (default 10).
        atr_mult: band half-width as a multiple of ATR (default 2.0). Entry is
            close > middle + atr_mult * ATR.
        atr_stop_mult: ATR multiple for the protective stop (default 2.0). The
            preferred stop is entry - atr_stop_mult * ATR.
        stop_loss_pct: fixed-percentage stop used as a fallback during ATR warmup
            (default 0.05 = 5%). Guarantees every BUY carries a stop.
        strength: conviction (0..1) reported on every BUY (default 1.0); the
            RiskManager multiplies the position cap by this.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        ema_period: int = 20,
        atr_period: int = 10,
        atr_mult: float = 2.0,
        atr_stop_mult: float = 2.0,
        stop_loss_pct: Decimal = Decimal("0.05"),
        strength: Decimal = Decimal("1.0"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if ema_period < 1:
            raise ValueError("ema_period must be >= 1")
        if atr_period < 1:
            raise ValueError("atr_period must be >= 1")
        if atr_mult <= 0:
            raise ValueError("atr_mult must be positive")
        if atr_stop_mult <= 0:
            raise ValueError("atr_stop_mult must be positive")
        if not (Decimal("0") < stop_loss_pct < Decimal("1")):
            raise ValueError("stop_loss_pct must be in (0, 1)")
        if not (Decimal("0") < strength <= Decimal("1")):
            raise ValueError("strength must be in (0, 1]")
        self.ema_period = ema_period
        self.atr_period = atr_period
        self.atr_mult = atr_mult
        self.atr_stop_mult = atr_stop_mult
        self.stop_loss_pct = stop_loss_pct
        self.strength = strength
        # Per-symbol rolling OHLC buffers. NO internal long/flat flag — the real
        # holding is read from the broker-reconciled context each bar.
        self._highs: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._lows: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}

    # ---- channel ----------------------------------------------------------

    def _keltner(
        self, ticker: str
    ) -> tuple[Optional[float], Optional[float]]:
        """
        Latest (middle EMA, upper band) for `ticker`, computed from closed bars.
        Returns (None, None) until both the EMA and ATR have warmed up. `upper`
        is None whenever ATR is still warming (EMA alone is not enough to trade).
        """
        closes = self._closes[ticker]
        middle_series = ind.ema(closes, self.ema_period)
        middle = middle_series[-1] if middle_series else None
        if middle is None:
            return None, None

        atr_series = ind.atr(
            self._highs[ticker], self._lows[ticker], closes, self.atr_period
        )
        atr_val = atr_series[-1] if atr_series else None
        if atr_val is None:
            return middle, None

        upper = middle + self.atr_mult * atr_val
        return middle, upper

    def _latest_atr(self, ticker: str) -> Optional[float]:
        atr_series = ind.atr(
            self._highs[ticker], self._lows[ticker], self._closes[ticker],
            self.atr_period,
        )
        return atr_series[-1] if atr_series else None

    def _suggested_stop(self, ticker: str, entry: Decimal) -> Decimal:
        """
        ATR-based protective stop (entry - atr_stop_mult * ATR), with a fixed
        percentage fallback during the ATR warmup. NEVER returns a non-positive
        stop: a too-wide ATR stop is floored to the percentage stop.
        """
        pct_stop = entry * (Decimal("1") - self.stop_loss_pct)
        atr_val = self._latest_atr(ticker)
        if atr_val is None or atr_val <= 0:
            return pct_stop
        atr_stop = entry - Decimal(str(self.atr_stop_mult * atr_val))
        if atr_stop <= 0:
            return pct_stop
        return atr_stop

    # ---- position ---------------------------------------------------------

    def _held(self, symbol: Symbol) -> bool:
        """
        True if we ACTUALLY hold a long position, read from the broker-reconciled
        context. With no context bound (isolated use) we treat ourselves as flat —
        the harness always binds and refreshes the context before dispatch.
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

        self._highs[ticker].append(float(bar.high))
        self._lows[ticker].append(float(bar.low))
        self._closes[ticker].append(float(bar.close))

        # Keep the buffers bounded: EMA needs ema_period and ATR needs
        # atr_period + 1 bars; a little slack covers seeding.
        max_len = max(self.ema_period, self.atr_period + 1) + 5
        for buf in (self._highs[ticker], self._lows[ticker], self._closes[ticker]):
            if len(buf) > max_len:
                del buf[:-max_len]

        middle, upper = self._keltner(ticker)
        if middle is None or upper is None:
            return []  # warmup: need both EMA and ATR to define the channel

        close = float(bar.close)
        held = self._held(bar.symbol)

        # TARGET state with hysteresis:
        #   - entry trigger: close breaks above the upper band,
        #   - while long: stay long until close falls below the middle EMA.
        if held:
            want_long = close >= middle
        else:
            want_long = close > upper

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
                        f"close {close:.4f} broke above Keltner upper "
                        f"{upper:.4f} (EMA{self.ema_period}+{self.atr_mult}*ATR"
                        f"{self.atr_period})"
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
                        f"close {close:.4f} fell below middle EMA{self.ema_period} "
                        f"{middle:.4f} (trend break exit)"
                    ),
                )
            )

        return signals
