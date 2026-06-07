"""
apex.strategy.library.stochastic_reversal
==========================================
Stochastic oscillator mean-reversion — an UNVALIDATED RESEARCH CANDIDATE.

WARNING: This strategy has NOT been through the Gauntlet. It is a research idea,
not a deployable edge. Do not allocate real capital to it. It exists to be
backtested, walk-forward validated, and Monte-Carlo'd before it earns the right
to be considered for paper trading (CLAUDE.md rule 17).

THE IDEA (long/flat, single-name mean reversion):
  The Stochastic oscillator measures where the close sits inside its recent
  high-low range. A low reading (oversold) means price is pinned near the bottom
  of its range — a classic snap-back setup. This candidate goes LONG when the
  fast line (%K) crosses UP through the slow line (%D) while BOTH were oversold,
  and exits when the oscillator reaches overbought.

  - %K = 100 * (close - lowest_low) / (highest_high - lowest_low)   over k_period
  - %D = SMA(%K, d_period)                                          (the slow line)
  - ENTER LONG: %K crosses above %D, and the prior %K (or %D) was <= oversold.
  - EXIT:       %K reaches >= overbought (take the mean-reversion profit).

POSITION-AWARENESS (mirrors multi_asset_trend, and for the same reason):
  The strategy holds NO internal long/flat flag. Each bar it computes a TARGET
  state (long iff a fresh oversold cross fired and we are flat; flat iff
  overbought) and emits the DELTA against what it ACTUALLY holds, read from the
  broker-reconciled StrategyContext. This makes it correct on a cold start, a
  restart, or a missed cron cycle, and it can NEVER pyramid: held + another
  oversold cross emits nothing.

  One nuance vs. a pure trend filter: an ENTRY here is a discrete EVENT (a cross
  out of oversold), not a persistent state. So entries are gated on "we are flat
  AND a fresh cross fired on THIS bar"; the EXIT is a persistent state
  (overbought), so on a cold start mid-trade we still exit correctly the moment
  the oscillator is overbought.

STOPS (CLAUDE.md rule 7 — every order carries a stop):
  Every BUY attaches a suggested_stop_loss. Preferred: an ATR-based stop at
  close - atr_mult * ATR (volatility-aware). During the ATR warmup (not enough
  bars yet) it falls back to a fixed percentage stop, so a BUY is NEVER emitted
  without a stop. The RiskManager still validates/overrides — this is a suggestion.

CONVENTIONS:
  - Indicator/oscillator math is float (matches apex.strategy.indicators); money
    (prices/stops) stays Decimal.
  - No look-ahead: every value at bar i uses only data up to and including i.
  - Stdlib + existing apex modules only. Reuses apex.strategy.indicators.atr;
    the Stochastic itself is implemented PRIVATELY here (not in the shared lib).
  - Deterministic, no I/O — safe on the free CI runner.
"""
from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class StochasticReversalStrategy(BaseStrategy):
    """
    Long-only Stochastic mean reversion (UNVALIDATED research candidate).

    Args:
        strategy_id: unique id for this instance.
        symbols: the universe this instance trades (each handled independently).
        k_period: lookback for the raw %K range (default 14).
        d_period: SMA smoothing for %D, the slow line (default 3).
        oversold: %K/%D level (0..100) that qualifies a setup as oversold (default 20).
        overbought: %K level (0..100) that triggers the exit (default 80).
        atr_period: lookback for the ATR used to place the stop (default 14).
        atr_mult: ATR multiple below the close for the protective stop (default 2.0).
        stop_loss_pct: fixed fallback stop distance during ATR warmup (default 5%).
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        k_period: int = 14,
        d_period: int = 3,
        oversold: float = 20.0,
        overbought: float = 80.0,
        atr_period: int = 14,
        atr_mult: float = 2.0,
        stop_loss_pct: Decimal = Decimal("0.05"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if k_period < 1:
            raise ValueError("k_period must be >= 1")
        if d_period < 1:
            raise ValueError("d_period must be >= 1")
        if not (0.0 <= oversold < overbought <= 100.0):
            raise ValueError("require 0 <= oversold < overbought <= 100")
        if atr_period < 1:
            raise ValueError("atr_period must be >= 1")
        if atr_mult <= 0:
            raise ValueError("atr_mult must be positive")
        if stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be positive")
        self.k_period = k_period
        self.d_period = d_period
        self.oversold = float(oversold)
        self.overbought = float(overbought)
        self.atr_period = atr_period
        self.atr_mult = float(atr_mult)
        self.stop_loss_pct = stop_loss_pct
        # Per-symbol rolling OHLC buffers. Like multi_asset_trend, there is NO
        # internal long/flat flag here — holding is read from the broker-reconciled
        # context each bar (see _held + StrategyContext.sync_state).
        self._highs: dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._lows: dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._closes: dict[str, list[float]] = {s.ticker: [] for s in symbols}

    # ---- stochastic oscillator (private; not in the shared indicator lib) ----

    def _raw_k(self, highs: list[float], lows: list[float], closes: list[float]) -> list[Optional[float]]:
        """
        Fast %K over the rolling high-low range. Same length as input, None until
        `k_period` bars exist. A flat range (high == low) maps to 50.0 (neutral),
        which avoids a divide-by-zero and is the standard degenerate-range handling.
        No look-ahead: index i uses only bars [i-k_period+1 .. i].
        """
        n = len(closes)
        out: list[Optional[float]] = [None] * n
        for i in range(self.k_period - 1, n):
            window_hi = max(highs[i - self.k_period + 1: i + 1])
            window_lo = min(lows[i - self.k_period + 1: i + 1])
            rng = window_hi - window_lo
            if rng <= 0:
                out[i] = 50.0
            else:
                out[i] = 100.0 * (closes[i] - window_lo) / rng
        return out

    def _stochastic(
        self, highs: list[float], lows: list[float], closes: list[float]
    ) -> tuple[list[Optional[float]], list[Optional[float]]]:
        """Return (%K, %D) where %D = SMA(%K, d_period). Both same length as input."""
        k = self._raw_k(highs, lows, closes)
        n = len(closes)
        d: list[Optional[float]] = [None] * n
        # SMA over the contiguous tail of non-None %K values, mapped back to index.
        valid = [(i, v) for i, v in enumerate(k) if v is not None]
        if len(valid) >= self.d_period:
            k_vals = [v for _, v in valid]
            smoothed = ind.sma(k_vals, self.d_period)
            for (orig_i, _), s in zip(valid, smoothed):
                d[orig_i] = s
        return k, d

    # ---- stop placement ---------------------------------------------------

    def _suggested_stop(
        self, highs: list[float], lows: list[float], closes: list[float], price: Decimal
    ) -> Decimal:
        """
        ATR-based protective stop (close - atr_mult * ATR). Falls back to a fixed
        percentage stop during the ATR warmup, so every BUY carries a stop. The
        stop is clamped to stay strictly positive (fail-safe for tiny prices).
        """
        atr_series = ind.atr(highs, lows, closes, self.atr_period)
        latest_atr = atr_series[-1] if atr_series else None
        if latest_atr is not None and latest_atr > 0:
            stop = price - Decimal(str(self.atr_mult)) * Decimal(str(latest_atr))
        else:
            stop = price * (Decimal("1") - self.stop_loss_pct)
        if stop <= 0:
            stop = price * (Decimal("1") - self.stop_loss_pct)
        return stop

    # ---- position ---------------------------------------------------------

    def _held(self, symbol: Symbol) -> bool:
        """
        True iff we ACTUALLY hold a long position, read from the broker-reconciled
        context. With no context bound (isolated use) we treat ourselves as flat;
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

        highs = self._highs[ticker]
        lows = self._lows[ticker]
        closes = self._closes[ticker]
        highs.append(float(bar.high))
        lows.append(float(bar.low))
        closes.append(float(bar.close))

        # Bound the buffers: we need k_period + d_period for the oscillator and
        # atr_period + 1 for the stop, plus a little slack.
        max_len = max(self.k_period + self.d_period, self.atr_period + 1) + 5
        if len(closes) > max_len:
            del highs[:-max_len]
            del lows[:-max_len]
            del closes[:-max_len]

        k, d = self._stochastic(highs, lows, closes)
        k_now, k_prev = k[-1], (k[-2] if len(k) >= 2 else None)
        d_now, d_prev = d[-1], (d[-2] if len(d) >= 2 else None)

        held = self._held(bar.symbol)
        signals: List[SignalEvent] = []
        price = bar.close

        # ---- EXIT (persistent state: overbought) --------------------------
        # Checked first so a cold start mid-trade exits as soon as it's overbought,
        # regardless of whether a cross is computable.
        if held:
            if k_now is not None and k_now >= self.overbought:
                signals.append(
                    SignalEvent(
                        symbol=bar.symbol,
                        side=OrderSide.SELL,
                        strength=Decimal("1.0"),
                        strategy_id=self.strategy_id,
                        timestamp=bar.timestamp,
                        reason=(
                            f"Stoch %K {k_now:.1f} >= overbought {self.overbought:.0f}; "
                            f"mean-reversion exit"
                        ),
                    )
                )
            return signals

        # ---- ENTRY (discrete event: fresh oversold %K-over-%D cross) ------
        if None in (k_now, k_prev, d_now, d_prev):
            return signals  # oscillator still warming up

        crossed_up = k_prev <= d_prev and k_now > d_now
        was_oversold = min(k_prev, d_prev) <= self.oversold
        if crossed_up and was_oversold:
            stop = self._suggested_stop(highs, lows, closes, price)
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.BUY,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=stop,
                    timestamp=bar.timestamp,
                    reason=(
                        f"Stoch %K crossed above %D from oversold "
                        f"(%K {k_prev:.1f}->{k_now:.1f}, %D {d_prev:.1f}->{d_now:.1f})"
                    ),
                )
            )
        return signals
