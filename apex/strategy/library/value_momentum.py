"""
apex.strategy.library.value_momentum
====================================
Combined per-asset VALUE + MOMENTUM composite-score strategy — the documented
"next probe" for the second uncorrelated edge (DECISIONS Session 22, next probe (b)).

THE THESIS (why combine, rather than run two separate sleeves):
  Sessions 19-22 established the two raw return drivers available on a price-only,
  long-only ETF universe and *how they relate*:
    - MOMENTUM (time-series / relative strength) exploits SHORT-horizon POSITIVE
      autocorrelation — recent winners keep winning. It is correlated to the deployed
      trend strategy by construction (+0.76, Session 19), but it is a strong premium.
    - VALUE (long-horizon reversal) exploits LONG-horizon NEGATIVE autocorrelation —
      multi-year losers mean-revert. Session 22 proved it comes back genuinely
      UNCORRELATED to trend (+0.29) — a real diversifier — but standalone it is too
      WEAK (Sharpe 0.30, edge < costs) to earn any blend weight on its own.
  This is exactly the AQR "Value and Momentum Everywhere" pair: the two signals are
  individually orthogonal-ish (one fast-positive, one slow-negative autocorrelation),
  so a per-asset COMBINED score — "hold assets that are both relatively cheap AND
  trending" — is the canonical way to harvest them together. Combining at the SCORE
  level (one composite rank) rather than at the SLEEVE level (two portfolios blended)
  lets each name's value tilt and momentum tilt reinforce: an asset only earns a top
  rank when both legs agree, which historically lifts the combined Sharpe above either
  leg alone and keeps turnover lower than two competing books.

THE COMPOSITE SCORE (price-only, no new data source, deterministic):
  For each asset, cross-sectionally Z-SCORE each leg across the live universe, then
  blend:

      value_raw(a)    = -(return from `value_period` bars ago to `skip_recent` bars ago)
                        (cheaper-vs-multi-year-ago -> higher; skip last ~1y so value
                        does not overlap and re-create momentum — "5y reversal, skip 12m")
      momentum_raw(a) = weighted blend of trailing total returns over several lookbacks
                        (default 1/3/6/12 months ~ 21/63/126/252 bars), the standard
                        multi-horizon time-series momentum blend (robust to any single
                        lookback's noise).

      z_value(a)      = (value_raw(a)    - mean_value)    / stdev_value
      z_momentum(a)   = (momentum_raw(a) - mean_momentum) / stdev_momentum
      composite(a)    = value_weight * z_value(a) + momentum_weight * z_momentum(a)

  Z-scoring puts the two legs on the same scale so neither dominates by units, and
  makes the blend weights mean what they say. With a single live asset (stdev 0) a
  leg's Z contributes 0 — the blend degrades gracefully rather than dividing by zero.

RULES (long/flat, position-aware — mirrors CrossAssetValueStrategy /
       CrossSectionalMomentumStrategy exactly):
  - Each bar, update each sleeve's value_raw, momentum_raw and realized vol.
  - WANTED = in the top `top_k` sleeves by COMPOSITE score. An OPTIONAL absolute trend
    filter (`use_trend_filter`, default OFF) can require price > its `trend_period` SMA
    to avoid catching a falling knife; it is OFF by default so we first measure the PURE
    composite's correlation to trend (a trend filter re-introduces the very correlation
    we are trying to escape — the lesson from cross_sectional_momentum / cross_asset_value).
  - flat + wanted  -> BUY  (inverse-vol sized, like the trend strategy)
  - held + !wanted -> SELL (full exit)
Holdings come from the broker-reconciled context (cold-start correct, no pyramiding).

UNIVERSE: configurable via the `symbols` argument so this can run on the richer
10-13 ETF pool (DECISIONS Session 22 next probe (a): "run value on the richer 10-13 ETF
`expanded` universe so the cross-sectional rank has more to separate"). The module-level
`DEFAULT_VALMOM_UNIVERSE` is the broad multi-asset `expanded` set (10 ETFs spanning US/
intl/EM equities, Treasuries/credit, gold/silver, commodities, REITs) — more sleeves give
both the value and the momentum cross-sectional ranks more dispersion to exploit.

NOT DEPLOYED. Like cross_asset_value, this is a research/library entry only. Before any
capital it MUST clear the full Validation Gauntlet (esp. Sharpe@2x-cost > 0, the gate
the weak pure-value sleeve failed) AND the correlation gate vs. the deployed trend
strategy. Combining with momentum will RAISE correlation to trend vs. pure value's +0.29;
the open question is whether the stronger premium clears costs by enough that the blend
still earns weight despite the higher correlation. Do NOT mark this deployed until both
gates pass on real data.

Deterministic, no I/O, stdlib-only math — safe on the free CI runner.
"""
from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from apex.core.events import SignalEvent
from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy

# Broad multi-asset ETF pool (the `expanded` universe from scripts/validate_real.py):
# US large-cap, developed-intl, emerging equities; long & intermediate Treasuries;
# investment-grade credit; gold; silver; broad commodities; US REITs. A richer rank
# than the smart-7 so both the value and momentum cross-sections have more to separate.
DEFAULT_VALMOM_UNIVERSE: tuple[str, ...] = (
    "SPY", "EFA", "EEM", "TLT", "IEF", "LQD", "GLD", "SLV", "DBC", "VNQ",
)

# Default multi-horizon momentum lookbacks (~1/3/6/12 months of trading days) and the
# weights applied to each. Equal weights by default — a deliberately un-tuned, robust
# blend (no single lookback cherry-picked).
DEFAULT_MOM_LOOKBACKS: tuple[int, ...] = (21, 63, 126, 252)


def default_universe(asset_class: AssetClass = AssetClass.ETF) -> List[Symbol]:
    """Construct Symbols for the broad multi-asset ETF pool (helper for callers/configs)."""
    return [Symbol(t, asset_class) for t in DEFAULT_VALMOM_UNIVERSE]


class ValueMomentumStrategy(BaseStrategy):
    """
    Hold the top-K sleeves by a combined cross-sectional VALUE + MOMENTUM Z-score.

    Args:
        strategy_id, symbols: as usual. `symbols` IS the (configurable) universe —
            pass the richer 10-13 ETF pool here; use `default_universe()` for the broad
            multi-asset default.
        value_period:    lookback (bars) for the long-horizon value window (default 1260 ~ 5y).
        skip_recent:     most-recent bars excluded from the value window (default 252 ~ 1y),
                         so value does not overlap short-term momentum.
        mom_lookbacks:   trailing-return lookbacks (bars) blended into the momentum leg
                         (default 21/63/126/252 ~ 1/3/6/12 months).
        mom_weights:     per-lookback weights for the momentum blend (default equal).
        value_weight:    weight on the value Z-score in the composite (default 0.5).
        momentum_weight: weight on the momentum Z-score in the composite (default 0.5).
        top_k:           how many top-composite sleeves to hold (default 3).
        use_trend_filter: if True, a wanted sleeve must also be above its `trend_period`
                         SMA (falling-knife guard). Default False — measure the pure
                         composite first.
        trend_period:    SMA period for the optional trend filter (default 200).
        vol_window:      lookback for inverse-vol sizing (default 60).
        stop_loss_pct, min_strength: as in CrossAssetValueStrategy.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        value_period: int = 1260,
        skip_recent: int = 252,
        mom_lookbacks: Sequence[int] = DEFAULT_MOM_LOOKBACKS,
        mom_weights: Optional[Sequence[float]] = None,
        value_weight: float = 0.5,
        momentum_weight: float = 0.5,
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
        lookbacks = list(mom_lookbacks)
        if not lookbacks:
            raise ValueError("mom_lookbacks must be non-empty")
        if any(lb < 1 for lb in lookbacks):
            raise ValueError("each mom_lookback must be >= 1")
        if mom_weights is None:
            weights = [1.0] * len(lookbacks)
        else:
            weights = list(mom_weights)
            if len(weights) != len(lookbacks):
                raise ValueError("mom_weights must match mom_lookbacks length")
            if any(w < 0 for w in weights):
                raise ValueError("mom_weights must be non-negative")
            if sum(weights) <= 0:
                raise ValueError("mom_weights must sum to a positive value")
        if value_weight < 0 or momentum_weight < 0:
            raise ValueError("value_weight and momentum_weight must be non-negative")
        if value_weight + momentum_weight <= 0:
            raise ValueError("value_weight + momentum_weight must be > 0")
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if vol_window < 2:
            raise ValueError("vol_window must be >= 2")

        self.value_period = value_period
        self.skip_recent = skip_recent
        self.mom_lookbacks = lookbacks
        self.mom_weights = weights
        self.value_weight = float(value_weight)
        self.momentum_weight = float(momentum_weight)
        self.top_k = top_k
        self.use_trend_filter = use_trend_filter
        self.trend_period = trend_period
        self.vol_window = vol_window
        self.stop_loss_pct = stop_loss_pct
        self.min_strength = min_strength

        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._value: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}
        self._mom: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}
        self._vol: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}

    # ---- raw leg metrics -------------------------------------------------

    def _value_raw(self, closes: list[float]) -> Optional[float]:
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

    def _momentum_raw(self, closes: list[float]) -> Optional[float]:
        """
        Weighted blend of trailing total returns over `mom_lookbacks`. Returns None
        until EVERY lookback has enough history (so the blend is always comparable
        across assets — no asset gets a partial, incomparable momentum score).
        """
        longest = max(self.mom_lookbacks)
        if len(closes) < longest + 1:
            return None
        num = 0.0
        denom = 0.0
        for lb, w in zip(self.mom_lookbacks, self.mom_weights):
            past = closes[-(lb + 1)]
            if not past:
                return None
            num += w * (closes[-1] / past - 1.0)
            denom += w
        return num / denom if denom else None

    def _realized_vol(self, closes: list[float]) -> Optional[float]:
        if len(closes) < self.vol_window + 1:
            return None
        w = closes[-(self.vol_window + 1):]
        rets = [(w[i] - w[i - 1]) / w[i - 1] for i in range(1, len(w)) if w[i - 1]]
        return statistics.pstdev(rets) if len(rets) >= 2 else None

    # ---- composite ranking ------------------------------------------------

    @staticmethod
    def _zscores(raw: Dict[str, float]) -> Dict[str, float]:
        """
        Cross-sectional Z-scores of the live (non-None) raw values. With < 2 live
        values, or zero dispersion, every Z is 0.0 (the leg contributes nothing rather
        than blowing up) — a graceful degenerate case.
        """
        vals = list(raw.values())
        if len(vals) < 2:
            return {t: 0.0 for t in raw}
        mean = statistics.fmean(vals)
        sd = statistics.pstdev(vals)
        if sd == 0:
            return {t: 0.0 for t in raw}
        return {t: (v - mean) / sd for t, v in raw.items()}

    def _composites(self) -> Dict[str, float]:
        """
        Combined composite score per asset that has BOTH legs available. Only assets
        with both a value and a momentum score participate in the ranking — a partial
        score would not be comparable to a full one.
        """
        live_value = {t: v for t, v in self._value.items() if v is not None}
        live_mom = {t: m for t, m in self._mom.items() if m is not None}
        both = live_value.keys() & live_mom.keys()
        if not both:
            return {}
        zv = self._zscores({t: live_value[t] for t in both})
        zm = self._zscores({t: live_mom[t] for t in both})
        return {
            t: self.value_weight * zv[t] + self.momentum_weight * zm[t]
            for t in both
        }

    def _in_top_k(self, ticker: str, composites: Dict[str, float]) -> bool:
        """Is `ticker` among the top_k sleeves by current composite score?"""
        ranked = sorted(composites.items(), key=lambda kv: kv[1], reverse=True)
        leaders = {t for t, _ in ranked[: self.top_k]}
        return ticker in leaders

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

    # ---- main hook -------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []
        closes = self._closes[ticker]
        closes.append(float(bar.close))
        max_len = max(
            self.value_period, self.trend_period, self.vol_window, max(self.mom_lookbacks)
        ) + 5
        if len(closes) > max_len:
            del closes[:-max_len]

        self._value[ticker] = self._value_raw(closes)
        self._mom[ticker] = self._momentum_raw(closes)
        self._vol[ticker] = self._realized_vol(closes)

        # Need this sleeve to have BOTH legs scored before it can be ranked.
        if self._value[ticker] is None or self._mom[ticker] is None:
            return []

        composites = self._composites()
        if ticker not in composites:
            return []

        if self.use_trend_filter and len(closes) >= self.trend_period:
            sma = ind.sma(closes, self.trend_period)[-1]
        else:
            sma = None

        wanted = self._in_top_k(ticker, composites)
        if self.use_trend_filter:
            wanted = wanted and sma is not None and bar.close > Decimal(str(sma))
        held = self._held(bar.symbol)
        signals: List[SignalEvent] = []

        if wanted and not held:
            strength = self._inverse_vol_strength(ticker)
            reason = (
                f"top-{self.top_k} value+momentum composite "
                f"(value {self.value_weight}/mom {self.momentum_weight})"
            )
            if self.use_trend_filter:
                reason += f", above SMA{self.trend_period}"
            signals.append(SignalEvent(
                symbol=bar.symbol, side=OrderSide.BUY, strength=strength,
                strategy_id=self.strategy_id,
                suggested_stop_loss=bar.close * (Decimal("1") - self.stop_loss_pct),
                timestamp=bar.timestamp,
                reason=reason,
            ))
        elif held and not wanted:
            signals.append(SignalEvent(
                symbol=bar.symbol, side=OrderSide.SELL, strength=Decimal("1.0"),
                strategy_id=self.strategy_id, timestamp=bar.timestamp,
                reason=f"fell out of top-{self.top_k} value+momentum composite",
            ))
        return signals
