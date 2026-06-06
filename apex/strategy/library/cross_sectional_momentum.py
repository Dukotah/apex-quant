"""
apex.strategy.library.cross_sectional_momentum
==============================================
Cross-Sectional (relative-strength) Momentum across an asset-class universe.

A DIFFERENT mechanism from MultiAssetTrendStrategy, deliberately — the goal is a
second edge UNCORRELATED to time-series trend, the only thing that meaningfully lifts
a combined portfolio's Sharpe (Session 16/0.5).

  - Time-series trend (the deployed strategy): hold EVERY sleeve that is above its own
    200-day SMA. Absolute: each asset judged against its own history.
  - Cross-sectional momentum (this): hold only the TOP-K sleeves ranked by relative
    momentum against EACH OTHER — concentrate in the current leaders.

The two disagree often (trend holds 6 uptrending sleeves equally; cross-sectional holds
only the 2-3 strongest), so their return streams can diverge — which is the point.

RULES (long/flat, position-aware):
  - Each bar, update each sleeve's momentum = rolling return over `mom_period`.
  - A sleeve is WANTED if it is (a) in the top `top_k` by momentum AND (b) above its own
    `trend_period` SMA (an absolute filter — never chase a falling "leader", the dual-
    momentum insight that the cost-failing pure cross-sectional `etf_rotation` lacked).
  - flat + wanted  -> BUY  (inverse-vol sized, like the trend strategy)
  - held + !wanted -> SELL (full exit)
Holdings are read from the broker-reconciled context (cold-start correct, no pyramiding),
exactly like MultiAssetTrendStrategy.

Deterministic, no I/O, stdlib-only math.
"""
from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class CrossSectionalMomentumStrategy(BaseStrategy):
    """
    Hold the top-K relative-strength leaders that are also in their own uptrend.

    Args:
        strategy_id, symbols: as usual.
        mom_period:   lookback (bars) for the relative-momentum ranking (default 126 ~ 6mo).
        top_k:        how many leaders to hold (default 3).
        trend_period: absolute trend filter SMA (default 200); a leader below it is skipped.
        vol_window:   lookback for inverse-vol sizing (default 60).
        stop_loss_pct, min_strength: as in MultiAssetTrendStrategy.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        mom_period: int = 126,
        top_k: int = 3,
        trend_period: int = 200,
        vol_window: int = 60,
        stop_loss_pct: Decimal = Decimal("0.05"),
        min_strength: Decimal = Decimal("0.10"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if mom_period < 2:
            raise ValueError("mom_period must be >= 2")
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if vol_window < 2:
            raise ValueError("vol_window must be >= 2")
        self.mom_period = mom_period
        self.top_k = top_k
        self.trend_period = trend_period
        self.vol_window = vol_window
        self.stop_loss_pct = stop_loss_pct
        self.min_strength = min_strength
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._mom: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}
        self._vol: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}

    # ---- metrics ---------------------------------------------------------

    def _momentum(self, closes: list[float]) -> Optional[float]:
        """Total return over the last `mom_period` bars (relative-strength score)."""
        if len(closes) < self.mom_period + 1:
            return None
        past = closes[-(self.mom_period + 1)]
        return (closes[-1] / past - 1.0) if past else None

    def _realized_vol(self, closes: list[float]) -> Optional[float]:
        if len(closes) < self.vol_window + 1:
            return None
        w = closes[-(self.vol_window + 1):]
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

    def _in_top_k(self, ticker: str) -> bool:
        """Is `ticker` among the top_k sleeves by current momentum?"""
        ranked = sorted(
            ((t, m) for t, m in self._mom.items() if m is not None),
            key=lambda kv: (-kv[1], kv[0]),   # score desc, ticker asc — deterministic tie-break
        )
        leaders = {t for t, _ in ranked[: self.top_k]}
        return ticker in leaders

    # ---- main hook -------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []
        closes = self._closes[ticker]
        closes.append(float(bar.close))
        max_len = max(self.trend_period, self.mom_period, self.vol_window) + 5
        if len(closes) > max_len:
            del closes[:-max_len]

        self._mom[ticker] = self._momentum(closes)
        self._vol[ticker] = self._realized_vol(closes)

        if len(closes) < max(self.trend_period, self.mom_period + 1):
            return []
        sma = ind.sma(closes, self.trend_period)[-1]
        if sma is None or self._mom[ticker] is None:
            return []

        # Wanted = a relative-strength leader that is ALSO in its own uptrend.
        wanted = self._in_top_k(ticker) and bar.close > Decimal(str(sma))
        held = self._held(bar.symbol)
        signals: List[SignalEvent] = []

        if wanted and not held:
            strength = self._inverse_vol_strength(ticker)
            signals.append(SignalEvent(
                symbol=bar.symbol, side=OrderSide.BUY, strength=strength,
                strategy_id=self.strategy_id,
                suggested_stop_loss=bar.close * (Decimal("1") - self.stop_loss_pct),
                timestamp=bar.timestamp,
                reason=f"top-{self.top_k} momentum leader, above SMA{self.trend_period}",
            ))
        elif held and not wanted:
            signals.append(SignalEvent(
                symbol=bar.symbol, side=OrderSide.SELL, strength=Decimal("1.0"),
                strategy_id=self.strategy_id, timestamp=bar.timestamp,
                reason=f"fell out of top-{self.top_k} or below SMA{self.trend_period}",
            ))
        return signals
