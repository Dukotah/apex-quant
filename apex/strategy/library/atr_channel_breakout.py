"""
apex.strategy.library.atr_channel_breakout
===========================================
ATR Channel Breakout — a LONG-ONLY volatility-channel trend strategy.

RESEARCH CANDIDATE — UNVALIDATED. This strategy has NOT been run through the
Gauntlet (no walk-forward, no Monte-Carlo edge-vs-luck check, no cost stress).
Treat it as an idea to backtest, not a deployable edge. Do not allocate real
capital to it until it clears the validation gates (CLAUDE.md rule 17).

THE IDEA
  Build a volatility-adaptive channel around a simple moving average of the
  close. The channel half-width is `channel_mult` ATRs:

      mid   = SMA(close, sma_period)
      upper = mid + channel_mult * ATR(period)
      lower = mid - channel_mult * ATR(period)

  Go (or stay) LONG when the close is ABOVE the upper channel band — a breakout
  out of the volatility envelope, the classic Keltner-style trend trigger. Exit
  back to flat when the close crosses back INSIDE the channel (close <= mid),
  i.e. momentum has decayed to the mean. Wider ATR ⇒ wider channel ⇒ the trigger
  auto-adapts to the instrument's current volatility regime.

POSITION AWARENESS (correct on restart, never pyramids)
  Like `multi_asset_trend`, this strategy holds NO internal long/flat flag. Each
  bar it computes a TARGET state (long iff close > upper band) and reads the
  ACTUAL holding from the broker-reconciled StrategyContext, then emits only the
  DELTA toward the target:
      - want long, currently flat  -> BUY  (single entry; a second in-channel
                                            breakout bar emits nothing, so it
                                            cannot pyramid)
      - currently long, want flat  -> SELL (full exit on cross back inside)
  This makes it idempotent: on a cold start mid-breakout it enters the existing
  move (no fresh crossing bar required), and a missed cron cycle self-heals.

STOP-LOSS (every BUY always carries one — CLAUDE.md rule 7)
  The suggested stop is volatility-based: `stop = price - stop_atr_mult * ATR`.
  During ATR warmup (before `period`+1 bars exist) ATR is None, so we FALL BACK
  to a fixed percentage stop `price * (1 - stop_loss_pct)`. Either way a BUY is
  never emitted without a protective stop. The RiskManager still validates and
  may tighten it — the strategy only suggests.

DETERMINISM / PURITY
  No look-ahead: every decision uses only closes/highs/lows up to and including
  the current bar. No I/O, no wall-clock time, no randomness. SMA and ATR come
  from the shared, tested `apex.strategy.indicators`; the channel arithmetic is
  the only math implemented locally (privately, below). Indicator math runs in
  float to match the indicators layer; all emitted prices/stops are Decimal.
"""
from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class ATRChannelBreakoutStrategy(BaseStrategy):
    """
    Long-only ATR-channel breakout (UNVALIDATED research candidate).

    Args:
        strategy_id: unique id for this instance.
        symbols: the universe this instance trades.
        sma_period: lookback for the channel midline SMA (default 20).
        atr_period: lookback for the ATR channel width (default 14).
        channel_mult: channel half-width in ATRs for the breakout band (default 2.0).
        stop_atr_mult: protective stop distance in ATRs below entry (default 2.0).
        stop_loss_pct: percentage stop used ONLY as the ATR-warmup fallback
            (default 0.05 = 5%).
        strength: conviction reported on every BUY (0..1; informs RiskManager sizing).
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        sma_period: int = 20,
        atr_period: int = 14,
        channel_mult: float = 2.0,
        stop_atr_mult: float = 2.0,
        stop_loss_pct: Decimal = Decimal("0.05"),
        strength: Decimal = Decimal("1.0"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if sma_period < 1:
            raise ValueError("sma_period must be >= 1")
        if atr_period < 1:
            raise ValueError("atr_period must be >= 1")
        if channel_mult <= 0:
            raise ValueError("channel_mult must be > 0")
        if stop_atr_mult <= 0:
            raise ValueError("stop_atr_mult must be > 0")
        if stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be > 0")
        self.sma_period = sma_period
        self.atr_period = atr_period
        self.channel_mult = channel_mult
        self.stop_atr_mult = stop_atr_mult
        self.stop_loss_pct = stop_loss_pct
        self.strength = strength
        # Per-symbol rolling OHLC buffers. NOTE: like multi_asset_trend, this
        # strategy holds NO internal long/flat flag — the held state is read from
        # the broker-reconciled context each bar (see _held + on_bar).
        self._highs: dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._lows: dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._closes: dict[str, list[float]] = {s.ticker: [] for s in symbols}

    # ---- channel math (private; not imported from any sibling ind_* file) ----

    def _channel(
        self, highs: list[float], lows: list[float], closes: list[float]
    ) -> Optional[tuple[float, float, Optional[float]]]:
        """
        Latest (mid, upper, atr_value) for the volatility channel, or None during
        SMA warmup. `atr_value` is None while ATR itself is still warming up; in
        that case `upper` falls back to the bare midline (no breakout possible
        until ATR exists, which fails closed — no entry without a real channel).
        Computed from the trailing window only — no look-ahead.
        """
        mid_series = ind.sma(closes, self.sma_period)
        mid = mid_series[-1]
        if mid is None:
            return None
        atr_series = ind.atr(highs, lows, closes, self.atr_period)
        atr_value = atr_series[-1]
        if atr_value is None:
            # ATR not ready: no band width yet → upper == mid is unreachable by a
            # strict close>upper test only when close==mid, so this fails closed.
            return mid, mid, None
        upper = mid + self.channel_mult * atr_value
        return mid, upper, atr_value

    # ---- position ------------------------------------------------------------

    def _held(self, symbol: Symbol) -> bool:
        """
        True if we ACTUALLY hold a long position, read from the broker-reconciled
        context. With no context bound (isolated use) we treat ourselves as flat —
        the harness binds and refreshes the context before each dispatch.
        """
        if self.context is None:
            return False
        pos = self.context.get_position(symbol)
        return pos is not None and pos.quantity > 0

    def _suggested_stop(self, price: Decimal, atr_value: Optional[float]) -> Decimal:
        """
        ATR-based protective stop `price - stop_atr_mult * ATR`, with a percentage
        fallback during ATR warmup so EVERY BUY carries a stop. The stop is always
        clamped strictly below price (and above zero) so the RiskManager accepts it.
        """
        if atr_value is not None and atr_value > 0:
            stop = price - Decimal(str(self.stop_atr_mult)) * Decimal(str(atr_value))
        else:
            stop = price * (Decimal("1") - self.stop_loss_pct)
        # Fail closed: a stop at/above price is meaningless — pull it just below.
        if stop >= price:
            stop = price * (Decimal("1") - self.stop_loss_pct)
        if stop <= 0:
            stop = price * (Decimal("1") - self.stop_loss_pct)
        return stop

    # ---- main hook -----------------------------------------------------------

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

        # Bound the buffers: need sma_period for the midline and atr_period+1 bars
        # for ATR, plus a little slack.
        max_len = max(self.sma_period, self.atr_period + 1) + 5
        if len(closes) > max_len:
            del highs[:-max_len]
            del lows[:-max_len]
            del closes[:-max_len]

        channel = self._channel(highs, lows, closes)
        if channel is None:
            return []  # SMA warmup — not enough history, don't trade
        mid, upper, atr_value = channel

        close = closes[-1]
        # TARGET state, not the cross EVENT. Long iff the close has broken out
        # ABOVE the upper band; flat iff it has fallen back to/under the midline.
        # In the band interior (mid < close <= upper) we HOLD whatever we have:
        # no fresh entry there (avoids whipsaw), no premature exit.
        held = self._held(bar.symbol)
        signals: List[SignalEvent] = []
        price = bar.close

        if close > upper and not held:
            stop = self._suggested_stop(price, atr_value)
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.BUY,
                    strength=self.strength,
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=stop,
                    timestamp=bar.timestamp,
                    reason=(
                        f"close {close:.4f} > upper ATR band {upper:.4f} "
                        f"(SMA{self.sma_period}+{self.channel_mult}*ATR{self.atr_period}) breakout"
                    ),
                )
            )
        elif held and close <= mid:
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=(
                        f"close {close:.4f} crossed back inside channel "
                        f"(<= midline {mid:.4f}); exit to flat"
                    ),
                )
            )

        return signals
