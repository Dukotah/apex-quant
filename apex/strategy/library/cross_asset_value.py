"""
apex.strategy.library.cross_asset_value
=======================================
Cross-Asset VALUE across an asset-class universe — the long-horizon mean-reversion
counterpart to trend, hunted as a genuinely UNCORRELATED second edge.

Why this is the one long-only driver left to try (Sessions 19-20 closed the others):
  - Time-series trend / cross-sectional momentum exploit SHORT-horizon POSITIVE
    autocorrelation (recent winners keep winning). Any momentum-family signal on this
    universe is therefore correlated to the deployed trend strategy BY CONSTRUCTION.
  - Short-term (daily/weekly) reversal is market-NEUTRAL, so long-only keeps only the
    losing half — it failed on both ETFs and single names.
  - VALUE is different in KIND: it exploits LONG-horizon NEGATIVE autocorrelation —
    3-5-year losers tend to mean-revert. This coexists with short-term trend (an asset
    can be a 5-year laggard yet in a 6-month uptrend), so its return stream is
    structurally able to diverge from trend. This is the AQR "Value and Momentum
    Everywhere" pair: for asset classes with no book value, past 5-year reversal IS the
    value proxy, and value/momentum are the canonical low-correlation duo.

Nothing here contradicts the prior negative results — they killed SHORT-horizon
reversal; this is the MULTI-YEAR horizon, a different phenomenon.

VALUE SIGNAL (price-only, no new data source needed):
  value_score(asset) = -(return from `value_period` bars ago to `skip_recent` bars ago)
  i.e. the cheaper an asset is vs its multi-year-ago self, the higher its score. The
  most-recent `skip_recent` bars are EXCLUDED so the signal does not overlap with (and
  accidentally re-create) short-term momentum — the standard "5y reversal, skip last 12m".

RULES (long/flat, position-aware — mirrors CrossSectionalMomentumStrategy):
  - Each bar, update each sleeve's value_score and realized vol.
  - WANTED = in the top `top_k` cheapest by value_score. An OPTIONAL absolute trend
    filter (`use_trend_filter`, default OFF) can require price > its `trend_period` SMA
    to avoid value traps; it is OFF by default precisely so we first measure PURE value's
    correlation to trend (a trend filter would re-introduce the very correlation we are
    trying to escape — the lesson from cross_sectional_momentum).
  - flat + wanted  -> BUY  (inverse-vol sized, like the trend strategy)
  - held + !wanted -> SELL (full exit)
Holdings come from the broker-reconciled context (cold-start correct, no pyramiding).

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


class CrossAssetValueStrategy(BaseStrategy):
    """
    Hold the top-K cheapest sleeves by long-horizon reversal (value).

    Args:
        strategy_id, symbols: as usual.
        value_period:  lookback (bars) for the long-horizon value window (default 1260 ~ 5y).
        skip_recent:   most-recent bars excluded from the value window (default 252 ~ 1y),
                       so value does not overlap short-term momentum.
        top_k:         how many cheap sleeves to hold (default 3).
        use_trend_filter: if True, a wanted sleeve must also be above its `trend_period`
                       SMA (value-trap guard). Default False — measure pure value first.
        trend_period:  SMA period for the optional trend filter (default 200).
        vol_window:    lookback for inverse-vol sizing (default 60).
        stop_loss_pct, min_strength: as in CrossSectionalMomentumStrategy.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        value_period: int = 1260,
        skip_recent: int = 252,
        top_k: int = 3,
        use_trend_filter: bool = False,
        trend_period: int = 200,
        vol_window: int = 60,
        stop_loss_pct: Decimal = Decimal("0.05"),
        min_strength: Decimal = Decimal("0.10"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if value_period < 2:
            raise ValueError("value_period must be >= 2")
        if skip_recent < 0:
            raise ValueError("skip_recent must be >= 0")
        if skip_recent >= value_period:
            raise ValueError("skip_recent must be < value_period")
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if vol_window < 2:
            raise ValueError("vol_window must be >= 2")
        self.value_period = value_period
        self.skip_recent = skip_recent
        self.top_k = top_k
        self.use_trend_filter = use_trend_filter
        self.trend_period = trend_period
        self.vol_window = vol_window
        self.stop_loss_pct = stop_loss_pct
        self.min_strength = min_strength
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._value: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}
        self._vol: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}

    # ---- metrics ---------------------------------------------------------

    def _value_score(self, closes: list[float]) -> Optional[float]:
        """
        Long-horizon reversal: negative of the return from value_period bars ago to
        skip_recent bars ago. Cheaper-vs-multi-year-ago -> higher score.
        """
        if len(closes) < self.value_period + 1:
            return None
        old = closes[-(self.value_period + 1)]
        recent = closes[-(self.skip_recent + 1)]
        if not old:
            return None
        return -(recent / old - 1.0)

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

    def _in_top_k(self, ticker: str) -> bool:
        """Is `ticker` among the top_k cheapest sleeves by current value score?"""
        ranked = sorted(
            ((t, v) for t, v in self._value.items() if v is not None),
            key=lambda kv: (-kv[1], kv[0]),  # value desc, ticker asc — deterministic tie-break
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
        max_len = max(self.value_period, self.trend_period, self.vol_window) + 5
        if len(closes) > max_len:
            del closes[:-max_len]

        self._value[ticker] = self._value_score(closes)
        self._vol[ticker] = self._realized_vol(closes)

        if self._value[ticker] is None:
            return []
        if self.use_trend_filter and len(closes) >= self.trend_period:
            sma = ind.sma(closes, self.trend_period)[-1]
        else:
            sma = None

        # Wanted = a top-K cheapest sleeve (optionally also in its own uptrend).
        wanted = self._in_top_k(ticker)
        if self.use_trend_filter:
            wanted = wanted and sma is not None and bar.close > Decimal(str(sma))
        held = self._held(bar.symbol)
        signals: List[SignalEvent] = []

        if wanted and not held:
            strength = self._inverse_vol_strength(ticker)
            reason = f"top-{self.top_k} cheapest (value: {self.value_period}b reversal)"
            if self.use_trend_filter:
                reason += f", above SMA{self.trend_period}"
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.BUY,
                    strength=strength,
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=bar.close * (Decimal("1") - self.stop_loss_pct),
                    timestamp=bar.timestamp,
                    reason=reason,
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
                    reason=f"fell out of top-{self.top_k} cheapest",
                )
            )
        return signals
