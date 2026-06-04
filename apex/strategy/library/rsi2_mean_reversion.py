"""
apex.strategy.library.rsi2_mean_reversion
=========================================
RSI(2) Mean Reversion (Larry Connors) — THE TACTICAL COMPLEMENT.

WHY: Complements Dual Momentum. Momentum profits from trends; this profits from
short-term dislocations *within* trends. Run them together with capped capital.

THE RULES (long-only):
  Trend filter:  price > 200-day SMA  (only buy dips in confirmed uptrends).
  Entry:         2-period RSI < entry_threshold (default 10) AND trend filter.
  Exit:          close > 5-day SMA, OR a time stop after time_stop_bars days (default 5).
  Universe:      liquid ETFs / large-caps (SPY, QQQ work well).

IMPLEMENTATION NOTES:
  - Read SMA(200), SMA(5), RSI(2) from apex.strategy.indicators (don't recompute).
  - Warmup: need 200+ bars before the trend filter is valid; return [] until then.
  - Emit strength scaled by how deep RSI is (RSI<5 → 1.0, RSI<10 → 0.6).
  - Suggest a stop-loss (fixed % below close) — RiskManager validates.
  - Capital: this is tactical. In live config, cap its allocation to 15-25%.
  - Determinism: pure function of bar history.

PERFORMANCE PRIOR: ~9%/yr on SPY, invested only ~28% of the time, but ~34% max DD
in volatile periods. The vol-filtered variant (rsi2_vol_filtered.py) tames that.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, List, Optional

from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy
from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol

_log = logging.getLogger(__name__)

# Minimum bars required before the 200-day SMA can be computed.
_WARMUP_BARS = 201  # need 200 bars to produce first SMA(200)


class RSI2MeanReversionStrategy(BaseStrategy):
    """
    RSI(2) Mean Reversion (Connors), long-only.

    Args:
        strategy_id:       unique id for this instance.
        symbols:           instruments to trade (typically one or a few liquid ETFs).
        entry_threshold:   RSI(2) value below which a BUY is triggered (default 10).
        sma_trend:         period for the trend-filter SMA (default 200).
        sma_exit:          period for the exit SMA (default 5).
        rsi_period:        RSI lookback (default 2 — Connors).
        time_stop_bars:    close position after this many bars if the SMA exit
                           hasn't fired (default 5). Set to 0 to disable.
        stop_loss_pct:     fixed protective stop below entry close (default 2%).
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        entry_threshold: Decimal = Decimal("10"),
        sma_trend: int = 200,
        sma_exit: int = 5,
        rsi_period: int = 2,
        time_stop_bars: int = 5,
        stop_loss_pct: Decimal = Decimal("0.02"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if sma_trend < sma_exit:
            raise ValueError("sma_trend must be >= sma_exit")
        if rsi_period < 1:
            raise ValueError("rsi_period must be >= 1")
        if stop_loss_pct <= Decimal("0") or stop_loss_pct >= Decimal("1"):
            raise ValueError("stop_loss_pct must be in (0, 1)")

        self.entry_threshold = entry_threshold
        self.sma_trend = sma_trend
        self.sma_exit = sma_exit
        self.rsi_period = rsi_period
        self.time_stop_bars = time_stop_bars
        self.stop_loss_pct = stop_loss_pct

        # Minimum bars before ANY signal is possible.
        self._min_bars: int = sma_trend + 1

        # Per-symbol state.
        self._closes: Dict[str, List[float]] = {s.ticker: [] for s in symbols}
        self._is_long: Dict[str, bool] = {s.ticker: False for s in symbols}
        # Counts bars since entry for the time-stop.
        self._bars_held: Dict[str, int] = {s.ticker: 0 for s in symbols}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_strength(self, rsi_val: float) -> Decimal:
        """Scale conviction: deeper RSI dips get stronger signals."""
        if rsi_val < 5.0:
            return Decimal("1.0")
        # Linear decay from 1.0 at RSI=5 down to 0.6 at RSI=entry_threshold.
        threshold = float(self.entry_threshold)
        if threshold <= 5.0:
            return Decimal("1.0")
        raw = 1.0 - 0.4 * (rsi_val - 5.0) / (threshold - 5.0)
        # Clamp to [0.5, 1.0] to avoid emitting near-zero strength signals.
        clamped = max(0.5, min(1.0, raw))
        return Decimal(str(round(clamped, 4)))

    # ------------------------------------------------------------------
    # BaseStrategy API
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []

        closes = self._closes[ticker]
        closes.append(float(bar.close))

        # Bound the buffer to avoid unbounded memory growth.
        max_len = self.sma_trend + 10
        if len(closes) > max_len:
            del closes[:-max_len]

        signals: List[SignalEvent] = []

        # ----- Exit logic (runs even during warmup extension) -----
        if self._is_long[ticker]:
            self._bars_held[ticker] += 1
            exit_triggered = False
            exit_reason = ""

            # SMA(5) exit: close above exit SMA.
            if len(closes) >= self.sma_exit:
                sma5 = ind.sma(closes, self.sma_exit)
                sma5_val = sma5[-1]
                if sma5_val is not None and float(bar.close) > sma5_val:
                    exit_triggered = True
                    exit_reason = (
                        f"close {bar.close} > SMA{self.sma_exit} {sma5_val:.4f}"
                    )

            # Time-stop.
            if (
                not exit_triggered
                and self.time_stop_bars > 0
                and self._bars_held[ticker] >= self.time_stop_bars
            ):
                exit_triggered = True
                exit_reason = (
                    f"time-stop: {self._bars_held[ticker]} bars held"
                )

            if exit_triggered:
                signals.append(
                    SignalEvent(
                        symbol=bar.symbol,
                        side=OrderSide.SELL,
                        strength=Decimal("1.0"),
                        strategy_id=self.strategy_id,
                        timestamp=bar.timestamp,
                        reason=exit_reason,
                    )
                )
                self._is_long[ticker] = False
                self._bars_held[ticker] = 0
                _log.info(
                    "%s SELL %s — %s",
                    self.strategy_id,
                    ticker,
                    exit_reason,
                )
                return signals

        # ----- Warmup guard -----
        if len(closes) < self._min_bars:
            return signals

        # ----- Indicator computation -----
        sma200 = ind.sma(closes, self.sma_trend)
        sma200_val: Optional[float] = sma200[-1]
        if sma200_val is None:
            return signals

        rsi_vals = ind.rsi(closes, self.rsi_period)
        rsi_val: Optional[float] = rsi_vals[-1]
        if rsi_val is None:
            return signals

        close_f = float(bar.close)

        # ----- Entry logic -----
        if not self._is_long[ticker]:
            trend_ok = close_f > sma200_val
            rsi_ok = rsi_val < float(self.entry_threshold)

            if trend_ok and rsi_ok:
                stop = bar.close * (Decimal("1") - self.stop_loss_pct)
                strength = self._compute_strength(rsi_val)
                signals.append(
                    SignalEvent(
                        symbol=bar.symbol,
                        side=OrderSide.BUY,
                        strength=strength,
                        strategy_id=self.strategy_id,
                        suggested_stop_loss=stop,
                        timestamp=bar.timestamp,
                        reason=(
                            f"RSI{self.rsi_period}={rsi_val:.2f} < {self.entry_threshold} "
                            f"AND close {bar.close} > SMA{self.sma_trend} {sma200_val:.4f}"
                        ),
                    )
                )
                self._is_long[ticker] = True
                self._bars_held[ticker] = 0
                _log.info(
                    "%s BUY %s — RSI(2)=%.2f SMA200=%.4f",
                    self.strategy_id,
                    ticker,
                    rsi_val,
                    sma200_val,
                )

        return signals
