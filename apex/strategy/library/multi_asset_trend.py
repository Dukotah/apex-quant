"""
apex.strategy.library.multi_asset_trend
========================================
Multi-Asset Trend Following with INVERSE-VOLATILITY (risk-parity) weighting.

This is the deployable edge from Session 9 promoted to a proper strategy class.
It applies a 200-day trend filter across five uncorrelated asset classes
(US equities, intl equities, long Treasuries, gold, broad commodities) and was
validated through the real-data Gauntlet at 6/7 gates (OOS Sharpe 1.12, MC
p=0.002, survives 2x costs).

WHAT CHANGED vs. the equal-weight `sma_crossover` baseline:
  The trend ENTRY/EXIT timing is identical (fast/slow SMA cross, default 20/200).
  The ONLY difference is SIZING. Equal-weight gave every sleeve the same 20%,
  which lets the wildest sleeves (equities, commodities) dominate the portfolio's
  risk and drove a 57% realistic max drawdown. This strategy instead expresses
  conviction by INVERSE VOLATILITY:

      strength_i = min_vol / vol_i        (clamped to (0, 1])

  where vol_i is the sleeve's realized volatility and min_vol is the lowest vol
  among the sleeves. The calmest in-trend sleeve (typically TLT/GLD) earns the
  full position cap (strength 1.0); wilder sleeves are scaled DOWN. The risk
  manager then sizes `equity * max_position_size_pct * strength`, so calmer
  sleeves carry proportionally more dollars — the standard managed-futures
  risk-parity tilt that equalizes risk contribution and cuts tail drawdown.

ARCHITECTURE NOTE: weighting is expressed ONLY through signal `strength`. The
strategy never sizes a position or touches the broker — it just says "long this,
and here's how much conviction." The RiskManager remains the sole sizer. This is
the intended seam (risk_manager._size_position multiplies the cap by strength).

THE RULES (long/flat trend per sleeve):
  - Maintain a rolling close buffer per symbol.
  - Each bar, update the sleeve's realized volatility (stdev of close-to-close
    returns over `vol_window`).
  - Fast SMA crosses ABOVE slow SMA, and flat → BUY, strength = inverse-vol weight.
  - Fast SMA crosses BELOW slow SMA, and long → SELL (full exit, strength 1.0).
  - Suggests a protective stop; the RiskManager validates and sizes.

Deterministic, no I/O, stdlib-only math — safe on the free CI runner.
"""
from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class MultiAssetTrendStrategy(BaseStrategy):
    """
    Trend-following across an asset-class universe with inverse-vol sizing.

    Args:
        strategy_id: unique id for this instance.
        symbols: the sleeve universe (e.g. SPY/EFA/TLT/GLD/DBC).
        fast_period: lookback for the fast SMA (default 20).
        slow_period: lookback for the slow SMA (default 200).
        vol_window: lookback (in returns) for realized volatility (default 60).
        stop_loss_pct: protective stop distance suggested to the RiskManager.
        min_strength: floor so a very wild sleeve still gets a tradeable size.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        fast_period: int = 20,
        slow_period: int = 200,
        vol_window: int = 60,
        stop_loss_pct: Decimal = Decimal("0.05"),
        min_strength: Decimal = Decimal("0.10"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if fast_period >= slow_period:
            raise ValueError("fast_period must be < slow_period")
        if vol_window < 2:
            raise ValueError("vol_window must be >= 2")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.vol_window = vol_window
        self.stop_loss_pct = stop_loss_pct
        self.min_strength = min_strength
        # Per-symbol rolling close buffers and latest realized vol. NOTE: this
        # strategy holds NO internal long/flat flag — it reads its real position
        # from the (broker-reconciled) context each bar. That is what makes it
        # correct on a cold start, a restart, or a missed cron cycle: the decision
        # is "target state vs. what I actually hold", not "did I see a cross in the
        # replay window". See on_bar + StrategyContext.sync_state.
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._vol: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}

    # ---- volatility ------------------------------------------------------

    def _realized_vol(self, closes: list[float]) -> Optional[float]:
        """Stdev of the last `vol_window` close-to-close simple returns."""
        if len(closes) < self.vol_window + 1:
            return None
        window = closes[-(self.vol_window + 1):]
        rets = [
            (window[i] - window[i - 1]) / window[i - 1]
            for i in range(1, len(window))
            if window[i - 1] != 0
        ]
        if len(rets) < 2:
            return None
        return statistics.pstdev(rets)

    def _inverse_vol_strength(self, ticker: str) -> Decimal:
        """
        Inverse-volatility weight for `ticker`, relative to the calmest sleeve:
            strength = min_vol / vol_ticker        (clamped to [min_strength, 1]).
        The calmest sleeve gets 1.0 (full cap); wilder sleeves scale down. Falls
        back to full conviction while volatility is still warming up.
        """
        own = self._vol.get(ticker)
        if own is None or own <= 0:
            return Decimal("1.0")
        live = [v for v in self._vol.values() if v is not None and v > 0]
        if not live:
            return Decimal("1.0")
        ratio = min(live) / own            # <= 1 by construction (own >= min)
        strength = Decimal(str(ratio))
        if strength > Decimal("1"):
            strength = Decimal("1")
        if strength < self.min_strength:
            strength = self.min_strength
        return strength

    # ---- position ---------------------------------------------------------

    def _held(self, symbol: Symbol) -> bool:
        """
        True if we ACTUALLY hold a long position in `symbol`, read from the
        broker-reconciled context. With no context bound (isolated use) we treat
        ourselves as flat — the harness (engine / run_once) always binds and
        refreshes the context before dispatch.
        """
        if self.context is None:
            return False
        pos = self.context.get_position(symbol)
        return pos is not None and pos.quantity > 0

    # ---- main hook -------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []  # not a sleeve we trade

        closes = self._closes[ticker]
        closes.append(float(bar.close))

        # Keep the buffer bounded: need slow_period for the trend filter + the
        # vol_window of returns for volatility, plus a little slack.
        max_len = max(self.slow_period, self.vol_window) + 5
        if len(closes) > max_len:
            del closes[:-max_len]

        # Update this sleeve's realized volatility every bar (used for weighting).
        self._vol[ticker] = self._realized_vol(closes)

        # Warmup: need slow_period points for the 200-day trend filter.
        if len(closes) < self.slow_period:
            return []

        fast = ind.sma(closes, self.fast_period)[-1]
        slow = ind.sma(closes, self.slow_period)[-1]
        if fast is None or slow is None:
            return []

        # Decide on STATE, not the cross EVENT: target = long iff in an uptrend,
        # then emit the delta against what we actually hold. This is idempotent —
        # it enters an established trend on a cold start (no fresh cross needed) and
        # never pyramids (held + still-uptrend emits nothing).
        want_long = fast > slow
        held = self._held(bar.symbol)
        signals: List[SignalEvent] = []
        price = bar.close

        if want_long and not held:
            strength = self._inverse_vol_strength(ticker)
            stop = price * (Decimal("1") - self.stop_loss_pct)
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.BUY,
                    strength=strength,
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=stop,
                    timestamp=bar.timestamp,
                    reason=(
                        f"SMA{self.fast_period}>SMA{self.slow_period} uptrend; "
                        f"inverse-vol weight {strength}"
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
                    reason=f"SMA{self.fast_period}<SMA{self.slow_period} trend break",
                )
            )

        return signals
