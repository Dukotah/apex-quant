"""
apex.strategy.library.short_term_reversal
==========================================
Short-Term Cross-Sectional Reversal with a trend filter — the mean-reversion attempt
at an edge UNCORRELATED to the deployed trend strategy.

Why this shape (eyes open that mean-reversion is hard):
  - Trend / momentum buy WINNERS (assets going up). Any momentum-flavoured strategy is
    therefore correlated to the deployed trend bot by construction (Session 19, +0.76).
  - This buys short-term LOSERS — the most oversold dips — which is the opposite sign of
    autocorrelation, so its return stream CAN be uncorrelated/negatively correlated.
  - The classic killer of mean-reversion is (a) catching falling knives and (b) turnover
    vs costs. We mitigate (a) with a long-term TREND FILTER (only buy dips in assets that
    are still above their 200d SMA — "buy the dip in an uptrend"), and we let the Gauntlet's
    2× cost-stress gate be the honest judge of (b). RSI2 failed exactly that gate.

RULES (long/flat, position-aware):
  - Each bar, score each sleeve by its recent `reversal_period` return (more negative =
    more oversold) and whether it is above its `trend_period` SMA.
  - Candidates = sleeves ABOVE their trend filter. Among those, the `bottom_k` MOST
    oversold are WANTED (buy the dip).
  - flat + wanted  -> BUY  (inverse-vol sized)
  - held + !wanted -> SELL (the bounce / trend break — a reversion exit)
Holdings read from the broker-reconciled context (cold-start correct, no pyramiding).

Deterministic, no I/O, stdlib-only.
"""

from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class ShortTermReversalStrategy(BaseStrategy):
    """
    Buy the most-oversold dips among assets still in a long-term uptrend; exit on bounce.

    Args:
        strategy_id, symbols: as usual.
        reversal_period: short lookback (bars) for the oversold score (default 5 ~ 1 week).
        bottom_k:        how many of the most-oversold uptrend names to hold (default 2).
        trend_period:    long-term SMA filter; only dips in uptrends are eligible (default 200).
        vol_window:      lookback for inverse-vol sizing (default 60).
        stop_loss_pct, min_strength: as in the other library strategies.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        reversal_period: int = 5,
        bottom_k: int = 2,
        trend_period: int = 200,
        vol_window: int = 60,
        stop_loss_pct: Decimal = Decimal("0.05"),
        min_strength: Decimal = Decimal("0.10"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if reversal_period < 1:
            raise ValueError("reversal_period must be >= 1")
        if bottom_k < 1:
            raise ValueError("bottom_k must be >= 1")
        if vol_window < 2:
            raise ValueError("vol_window must be >= 2")
        self.reversal_period = reversal_period
        self.bottom_k = bottom_k
        self.trend_period = trend_period
        self.vol_window = vol_window
        self.stop_loss_pct = stop_loss_pct
        self.min_strength = min_strength
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._rev: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}
        self._uptrend: Dict[str, bool] = {s.ticker: False for s in symbols}
        self._vol: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}

    # ---- metrics ---------------------------------------------------------

    def _reversal_score(self, closes: list[float]) -> Optional[float]:
        """Recent return over reversal_period; LOWER (more negative) = more oversold."""
        if len(closes) < self.reversal_period + 1:
            return None
        past = closes[-(self.reversal_period + 1)]
        return (closes[-1] / past - 1.0) if past else None

    def _realized_vol(self, closes: list[float]) -> Optional[float]:
        if len(closes) < self.vol_window + 1:
            return None
        w = closes[-(self.vol_window + 1) :]
        rets = [(w[i] - w[i - 1]) / w[i - 1] for i in range(1, len(w)) if w[i - 1]]
        return statistics.pstdev(rets) if len(rets) >= 2 else None

    def _inverse_vol_strength(self, ticker: str) -> Decimal:
        own = self._vol.get(ticker)
        if own is None or own <= 0:
            return Decimal("1.0")
        live = [v for v in self._vol.values() if v is not None and v > 0]
        if not live:
            return Decimal("1.0")
        strength = Decimal(str(min(live) / own))
        if strength > Decimal("1"):
            strength = Decimal("1")
        return max(self.min_strength, strength)

    def _held(self, symbol: Symbol) -> bool:
        if self.context is None:
            return False
        pos = self.context.get_position(symbol)
        return pos is not None and pos.quantity > 0

    def _is_oversold_leader(self, ticker: str) -> bool:
        """Among uptrend names, is `ticker` one of the bottom_k most-oversold?"""
        candidates = [
            (t, self._rev[t])
            for t in self._rev
            if self._uptrend.get(t) and self._rev[t] is not None
        ]
        if not candidates:
            return False
        # ascending by recent return → most negative (most oversold) first;
        # ticker as secondary key keeps the bottom-K deterministic on equal returns.
        candidates.sort(key=lambda kv: (kv[1], kv[0]))
        chosen = {t for t, _ in candidates[: self.bottom_k]}
        return ticker in chosen

    # ---- main hook -------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []
        closes = self._closes[ticker]
        closes.append(float(bar.close))
        max_len = max(self.trend_period, self.vol_window, self.reversal_period) + 5
        if len(closes) > max_len:
            del closes[:-max_len]

        self._rev[ticker] = self._reversal_score(closes)
        self._vol[ticker] = self._realized_vol(closes)

        if len(closes) < self.trend_period:
            return []
        sma = ind.sma(closes, self.trend_period)[-1]
        if sma is None or self._rev[ticker] is None:
            return []
        self._uptrend[ticker] = bar.close > Decimal(str(sma))

        wanted = self._uptrend[ticker] and self._is_oversold_leader(ticker)
        held = self._held(bar.symbol)
        signals: List[SignalEvent] = []

        if wanted and not held:
            strength = self._inverse_vol_strength(ticker)
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.BUY,
                    strength=strength,
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=bar.close * (Decimal("1") - self.stop_loss_pct),
                    timestamp=bar.timestamp,
                    reason=f"oversold dip (bottom-{self.bottom_k}) in an uptrend",
                )
            )
        elif held and not wanted:
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason="bounced out of oversold set or trend broke",
                )
            )
        return signals
