"""
apex.strategy.library.value_momentum
====================================
Combined per-asset VALUE + MOMENTUM score — Session-23 probe (2) at the second-edge frontier.

Sessions 22-23 established the shape of the problem:
  - Cross-asset VALUE (long-horizon reversal) is the FIRST long-only driver that comes back
    genuinely UNCORRELATED to the deployed trend strategy (corr +0.29) — the value/momentum
    thesis holds in this universe — but on its own its premium is too weak to deploy
    (smart-7 standalone Sharpe 0.30, edge < costs).
  - Probe (1) [S23] showed a RICHER ETF pool does not rescue value — it dilutes it. The
    limit is the KIND of asset, not the count, so a bigger basket is the wrong lever.

This strategy is probe (2): instead of running value as its own weak sleeve and blending two
separate return streams (which put 0% weight on value), combine the two signals AT THE
ASSET LEVEL. The AQR "Value and Momentum Everywhere" result is that a per-asset COMBINED
value+momentum score beats either signal alone AND beats a portfolio that blends the two
standalone sleeves — because the combination reweights within each asset (you hold names that
are BOTH cheap and trending, and the combined signal is itself less volatile than either leg).

COMBINED SIGNAL (price-only, no new data source — both legs are already in the codebase):
  value_score(asset)    = -(return from `value_period` to `skip_recent` bars ago)   (cheap = high)
  momentum_score(asset) =  return over the last `mom_period` bars                   (trending = high)
  Each bar, rank the eligible universe (assets that have BOTH scores) by each signal
  separately — higher score -> better (rank 0). The COMBINED rank is
        value_weight * value_rank + (1 - value_weight) * momentum_rank
  and the WANTED set is the `top_k` assets with the LOWEST combined rank — i.e. the names
  that score well on both legs at once. Ranks (not raw scores) are combined deliberately:
  the two signals live on different scales (5y reversal vs 6mo return) and ranks are the
  standard scale-free, outlier-robust way to fuse them.

RULES (long/flat, position-aware — mirrors CrossAssetValueStrategy / CrossSectionalMomentum):
  - flat + wanted  -> BUY  (inverse-vol sized, like the trend strategy)
  - held + !wanted -> SELL (full exit)
  Holdings come from the broker-reconciled context (cold-start correct, no pyramiding).

  An OPTIONAL absolute trend filter (`use_trend_filter`, default OFF) can additionally
  require price > its `trend_period` SMA. It is OFF by default so we first measure the PURE
  combined signal: the momentum leg already penalises falling names, so the combined rank is
  structurally resistant to value traps without it.

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


class ValueMomentumStrategy(BaseStrategy):
    """
    Hold the top-K sleeves by a combined value+momentum rank (cheap AND trending).

    Args:
        strategy_id, symbols: as usual.
        value_period:  lookback (bars) for the long-horizon value window (default 1260 ~ 5y).
        skip_recent:   most-recent bars excluded from the value window (default 252 ~ 1y),
                       so the value leg does not overlap the momentum leg.
        mom_period:    lookback (bars) for the momentum leg (default 126 ~ 6mo).
        top_k:         how many combined leaders to hold (default 3).
        value_weight:  weight on the value rank in the combined rank (default 0.5 = equal);
                       momentum gets (1 - value_weight). Must be in [0, 1].
        use_trend_filter: if True, a wanted sleeve must also be above its `trend_period`
                       SMA. Default False — measure the pure combined signal first.
        trend_period:  SMA period for the optional trend filter (default 200).
        vol_window:    lookback for inverse-vol sizing (default 60).
        stop_loss_pct, min_strength: as in CrossAssetValueStrategy.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        value_period: int = 1260,
        skip_recent: int = 252,
        mom_period: int = 126,
        top_k: int = 3,
        exit_rank_buffer: int = 0,
        value_weight: Decimal = Decimal("0.5"),
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
        if mom_period < 2:
            raise ValueError("mom_period must be >= 2")
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if exit_rank_buffer < 0:
            raise ValueError("exit_rank_buffer must be >= 0")
        if not (Decimal("0") <= value_weight <= Decimal("1")):
            raise ValueError("value_weight must be in [0, 1]")
        if vol_window < 2:
            raise ValueError("vol_window must be >= 2")
        self.value_period = value_period
        self.skip_recent = skip_recent
        self.mom_period = mom_period
        self.top_k = top_k
        self.exit_rank_buffer = exit_rank_buffer
        self.value_weight = float(value_weight)
        self.use_trend_filter = use_trend_filter
        self.trend_period = trend_period
        self.vol_window = vol_window
        self.stop_loss_pct = stop_loss_pct
        self.min_strength = min_strength
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._value: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}
        self._mom: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}
        self._vol: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}

    # ---- metrics ---------------------------------------------------------

    def _value_score(self, closes: list[float]) -> Optional[float]:
        """Long-horizon reversal: cheaper-vs-multi-year-ago -> higher score."""
        if len(closes) < self.value_period + 1:
            return None
        old = closes[-(self.value_period + 1)]
        recent = closes[-(self.skip_recent + 1)]
        if not old:
            return None
        return -(recent / old - 1.0)

    def _momentum_score(self, closes: list[float]) -> Optional[float]:
        """Total return over the last `mom_period` bars (trending -> higher score)."""
        if len(closes) < self.mom_period + 1:
            return None
        past = closes[-(self.mom_period + 1)]
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

    @staticmethod
    def _ranks(tickers: List[str], scores: Dict[str, Optional[float]]) -> Dict[str, int]:
        """0-based rank of each ticker by score, DESCENDING (highest score -> rank 0)."""
        # Ticker is a deterministic secondary key so equal scores never depend on the
        # construction-time symbol order (Golden Rule 10 — determinism is sacred).
        ordered = sorted(tickers, key=lambda t: (-scores[t], t))  # type: ignore[operator]
        return {t: i for i, t in enumerate(ordered)}

    def _wanted_set(self, k: int | None = None) -> set[str]:
        """The top `k` sleeves by combined value+momentum rank (lower = better on both).
        Defaults to the strict top_k (the entry band)."""
        if k is None:
            k = self.top_k
        eligible = [
            t
            for t in self._closes
            if self._value.get(t) is not None and self._mom.get(t) is not None
        ]
        if not eligible:
            return set()
        v_rank = self._ranks(eligible, self._value)
        m_rank = self._ranks(eligible, self._mom)
        combined = sorted(
            eligible,
            key=lambda t: (
                self.value_weight * v_rank[t] + (1.0 - self.value_weight) * m_rank[t],
                t,  # deterministic tie-break on equal combined rank
            ),
        )
        return set(combined[:k])

    # ---- main hook -------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []
        closes = self._closes[ticker]
        closes.append(float(bar.close))
        max_len = max(self.value_period, self.mom_period, self.trend_period, self.vol_window) + 5
        if len(closes) > max_len:
            del closes[:-max_len]

        self._value[ticker] = self._value_score(closes)
        self._mom[ticker] = self._momentum_score(closes)
        self._vol[ticker] = self._realized_vol(closes)

        # No combined score until this sleeve has both legs.
        if self._value[ticker] is None or self._mom[ticker] is None:
            return []
        if self.use_trend_filter and len(closes) >= self.trend_period:
            sma = ind.sma(closes, self.trend_period)[-1]
        else:
            sma = None

        held = self._held(bar.symbol)
        # Enter in the strict top_k; once held, keep until the name drops out of the wider
        # top_(k + exit_rank_buffer) band. Hysteresis cuts boundary-churn turnover (the same
        # lever that lifted pure value to grade A). Default buffer 0 == original behaviour.
        wanted = ticker in self._wanted_set(
            self.top_k + self.exit_rank_buffer if held else self.top_k
        )
        if self.use_trend_filter:
            wanted = wanted and sma is not None and bar.close > Decimal(str(sma))
        signals: List[SignalEvent] = []

        if wanted and not held:
            strength = self._inverse_vol_strength(ticker)
            reason = f"top-{self.top_k} combined value+momentum (vw={self.value_weight:.2f})"
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
                    reason=f"fell out of top-{self.top_k} combined value+momentum",
                )
            )
        return signals
