"""
apex.strategy.library.defensive_trend_allocator
================================================
Defensive Trend Allocator — the "kitchen-sink" research synthesis (Session 34).

This strategy is a deliberate SYNTHESIS of the survivable ideas surfaced by a
multi-agent literature review across four strategy families, each contributing
exactly one mechanism so the books are genuinely diversified rather than four
flavours of the same trend bet:

  1. TREND GATE — multi-horizon "barbell" price-vs-SMA vote.
     Source: Hurst, Ooi & Pedersen, "A Century of Evidence on Trend-Following
     Investing" (JPM 2017); Faber, "A Quantitative Approach to Tactical Asset
     Allocation" (2007). Long-only/no-leverage SURVIVES (Faber 5/5); the academic
     long/short Sharpe does NOT and is contested (Huang et al. 2020 JFE).

  2. ABSOLUTE-MOMENTUM CONFIRMATION — trailing 12-month return must be > 0.
     Source: Moskowitz, Ooi & Pedersen, "Time Series Momentum" (JFE 2012). The
     long-only-survivable core of TSMOM (the sign-of-12m-return signal), used as a
     SECOND gate on top of the trend vote to cut whipsaw in flat-but-noisy regimes.

  3. RISK-PARITY SLEEVE WEIGHTING — inverse-volatility conviction.
     Source: Maillard, Roncalli & Teiletche, "Equally-Weighted Risk Contributions"
     (JPM 2010). Inverse-vol is the closed-form ERC special case (equal pairwise
     correlation) — leverage-free, optimiser-free, the implementable 5/5 core of
     risk parity. The levered/short anomalies (BAB, levered risk parity) do NOT
     survive our constraints (Frazzini-Pedersen 2014; Novy-Marx 2022).

  4. VOLATILITY-TARGET DE-RISKING OVERLAY — cap total conviction in turmoil.
     Source: Harvey et al., "The Impact of Volatility Targeting" (JPM 2018). We
     keep ONLY the de-risking leg (scale DOWN when realized vol > target), never
     lever up — the long-only-survivable, out-of-sample-robust half. The Sharpe-
     enhancement claim of Moreira-Muir (2017) is explicitly REJECTED: it failed
     out-of-sample (Cederburg et al. 2020 JFE).

  5. SEASONAL "HALLOWEEN" TILT — de-weight the May-Oct "summer".
     Source: Bouman & Jacobsen, "The Halloween Indicator" (AER 2002). Included as
     a conviction MODIFIER, not a standalone book. Treated as the weakest, most
     data-mining-prone leg (Maberly-Pierce 2004 show the US effect is largely the
     '87 crash + LTCM) — present for completeness of the synthesis, expected to be
     the first thing the Gauntlet's PBO/DSR gates argue against.

⚠️ OVERFITTING DISCLOSURE (read before trusting any backtest of this):
  This strategy has a HIGH free-parameter count by design (it is the kitchen-sink
  synthesis the user requested). The decay literature is explicit that this is the
  danger zone — McLean & Pontiff (2016) (~50% post-publication Sharpe decay),
  Harvey-Liu-Zhu (2016) (require t>3.0), Bailey & López de Prado (7 configs ->
  spurious Sharpe>1; reject if PBO>=50%). To keep PBO honest, EVERY parameter
  below is fixed at its literature-canonical value and is NOT to be grid-searched.
  This is a RESEARCH strategy: it must clear the full Gauntlet (incl. Gates 8/9
  DSR + PBO) AND beat the deployed multi_asset_trend out-of-sample before it could
  ever be considered for deployment. The expected outcome, per the research, is
  that the synthesis does NOT justify its complexity. That is a valid result.

ARCHITECTURE: like every strategy here, it only expresses INTENT + CONVICTION via
SignalEvent.strength. It never sizes a position or touches the broker — the
RiskManager remains the sole sizer. All four conviction modifiers (inverse-vol,
seasonal, vol-target) are folded into `strength`; entry/exit remain state-based
(target long-vs-flat, emit the delta against the broker-reconciled context), so a
cold start / missed cron cycle enters an established trend without a fresh cross,
exactly like multi_asset_trend.

Deterministic, no I/O, stdlib-only math. Seasonality reads `bar.timestamp.month`
(the bar's own UTC time — an INPUT, identical in backtest and live), never the
wall clock, so backtest/live parity and Golden Rule #10 hold.
"""

from __future__ import annotations

import math
import statistics
from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy

# Months Nov-Apr are the "winter" half that historically carries the equity
# premium (Bouman & Jacobsen 2002). May-Oct is the de-weighted "summer".
_WINTER_MONTHS = frozenset({11, 12, 1, 2, 3, 4})
# Trading days per year, for annualizing the daily realized-vol estimate.
_TRADING_DAYS = 252


class DefensiveTrendAllocatorStrategy(BaseStrategy):
    """
    Long-only multi-sleeve allocator fusing trend + absolute-momentum + risk-parity
    weighting + a capped volatility-target overlay + a seasonal tilt.

    Args (all defaults are literature-canonical; do NOT tune to a backtest):
        strategy_id: unique id for this instance.
        symbols: the sleeve universe (e.g. SPY/EFA/TLT/GLD/DBC/VNQ).
        trend_lookbacks: SMA speeds for the barbell trend vote (default [50, 200] —
            Hurst fast/slow barbell; price>SMA at each speed casts one long vote).
        trend_threshold: vote fraction needed to be long (default 0.5 = majority).
        abs_mom_lookback: absolute-momentum confirmation horizon in bars
            (default 252 = 12 months; Moskowitz). Long requires trailing return > 0.
        vol_window: realized-vol lookback in returns (default 63 = ~3 months; the
            ERC/risk-parity-canonical window).
        target_vol: annualized portfolio vol target for the de-risking overlay
            (default 0.15; Harvey et al. — roughly SPY's long-run realized vol, so
            average exposure is ~full and we only ever scale DOWN).
        summer_weight: conviction multiplier applied May-Oct (default 0.5; the
            Halloween summer de-weight). 1.0 disables the seasonal tilt.
        stop_loss_pct: protective stop distance suggested to the RiskManager.
        min_strength: floor so a wild/de-risked sleeve still gets a tradeable size.
        vol_method: "ewma" (default; RiskMetrics, reacts faster, no ghost effect —
            Harvey et al.) or "simple" (population stdev).
        ewma_lambda: EWMA decay for vol_method="ewma" (default 0.94, daily standard).
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        *,
        trend_lookbacks: Optional[List[int]] = None,
        trend_threshold: float = 0.5,
        abs_mom_lookback: int = 252,
        vol_window: int = 63,
        target_vol: float = 0.15,
        summer_weight: Decimal = Decimal("0.5"),
        stop_loss_pct: Decimal = Decimal("0.05"),
        min_strength: Decimal = Decimal("0.10"),
        vol_method: str = "ewma",
        ewma_lambda: float = 0.94,
    ) -> None:
        super().__init__(strategy_id, symbols)
        lookbacks = list(trend_lookbacks) if trend_lookbacks is not None else [50, 200]
        if not lookbacks or any(lb < 2 for lb in lookbacks):
            raise ValueError("trend_lookbacks must be non-empty positive lookbacks (>= 2)")
        if not (0.0 < trend_threshold <= 1.0):
            raise ValueError("trend_threshold must be in (0, 1]")
        if abs_mom_lookback < 2:
            raise ValueError("abs_mom_lookback must be >= 2")
        if vol_window < 2:
            raise ValueError("vol_window must be >= 2")
        if target_vol <= 0:
            raise ValueError("target_vol must be positive")
        if not (Decimal("0") < summer_weight <= Decimal("1")):
            raise ValueError("summer_weight must be in (0, 1]")
        if not (Decimal("0") <= min_strength <= Decimal("1")):
            raise ValueError("min_strength must be in [0, 1]")
        if vol_method not in ("simple", "ewma"):
            raise ValueError("vol_method must be 'simple' or 'ewma'")
        if not (0.0 < ewma_lambda < 1.0):
            raise ValueError("ewma_lambda must be in (0, 1)")

        self.trend_lookbacks = lookbacks
        self.trend_threshold = trend_threshold
        self.abs_mom_lookback = abs_mom_lookback
        self.vol_window = vol_window
        self.target_vol = target_vol
        self.summer_weight = summer_weight
        self.stop_loss_pct = stop_loss_pct
        self.min_strength = min_strength
        self.vol_method = vol_method
        self.ewma_lambda = ewma_lambda

        # Per-symbol rolling close buffers + latest realized (daily) vol. Like
        # multi_asset_trend, this holds NO internal long/flat flag: it reads its
        # real position from the broker-reconciled context each bar so it is correct
        # on a cold start, restart, or missed cron cycle.
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._vol: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}

    # ---- volatility ------------------------------------------------------

    def _realized_vol(self, closes: list[float]) -> Optional[float]:
        """Daily realized vol over the last `vol_window` close-to-close returns."""
        if len(closes) < self.vol_window + 1:
            return None
        window = closes[-(self.vol_window + 1) :]
        rets = [
            (window[i] - window[i - 1]) / window[i - 1]
            for i in range(1, len(window))
            if window[i - 1] != 0
        ]
        if len(rets) < 2:
            return None
        if self.vol_method == "ewma":
            lam = self.ewma_lambda
            var = rets[0] ** 2
            for r in rets[1:]:
                var = lam * var + (1.0 - lam) * r * r
            return math.sqrt(var)
        return statistics.pstdev(rets)

    def _inverse_vol_weight(self, ticker: str) -> Decimal:
        """
        Risk-parity conviction (Maillard et al.): strength = min_vol / vol_ticker,
        so the calmest sleeve earns the full cap (1.0) and wilder sleeves scale down.
        Falls back to full conviction while vol is still warming up.
        """
        own = self._vol.get(ticker)
        if own is None or own <= 0:
            return Decimal("1.0")
        live = [v for v in self._vol.values() if v is not None and v > 0]
        if not live:
            return Decimal("1.0")
        ratio = min(live) / own  # <= 1 by construction (own >= min)
        weight = Decimal(str(ratio))
        return min(weight, Decimal("1"))

    def _vol_target_factor(self) -> Decimal:
        """
        Capped volatility-target de-risking overlay (Harvey et al. 2018), applied at
        the PORTFOLIO level: factor = min(1, target_vol / annualized_portfolio_vol).
        We only ever scale DOWN (cap at 1.0 — never lever). The portfolio vol proxy
        is the mean of the live sleeve daily vols, annualized by sqrt(252); this is a
        correlation-blind, conservative estimate (it ignores diversification, so it
        de-risks a touch earlier than a full covariance estimate would).
        """
        live = [v for v in self._vol.values() if v is not None and v > 0]
        if not live:
            return Decimal("1.0")  # warming up — no de-risking yet
        port_daily = sum(live) / len(live)
        port_annual = port_daily * math.sqrt(_TRADING_DAYS)
        if port_annual <= 0:
            return Decimal("1.0")
        factor = Decimal(str(self.target_vol / port_annual))
        return min(factor, Decimal("1"))

    def _seasonal_factor(self, month: int) -> Decimal:
        """Halloween tilt: full weight Nov-Apr, `summer_weight` May-Oct."""
        return Decimal("1.0") if month in _WINTER_MONTHS else self.summer_weight

    # ---- position --------------------------------------------------------

    def _held(self, symbol: Symbol) -> bool:
        """True if we ACTUALLY hold a long position, read from the context."""
        if self.context is None:
            return False
        pos = self.context.get_position(symbol)
        return pos is not None and pos.quantity > 0

    # ---- trend / momentum decision ---------------------------------------

    def _want_long(self, closes: list[float], price: float) -> Optional[bool]:
        """
        Target trend state: True=long, False=flat, None=warming up. Long requires
        BOTH gates: (a) the multi-speed barbell vote reaches trend_threshold, AND
        (b) trailing absolute momentum over `abs_mom_lookback` is positive.
        """
        need = max(max(self.trend_lookbacks), self.abs_mom_lookback)
        if len(closes) < need + 1:
            return None

        # Gate (a): barbell price-vs-SMA vote.
        votes = []
        for lb in self.trend_lookbacks:
            s = ind.sma(closes, lb)[-1]
            if s is None:
                return None
            votes.append(1.0 if price > s else 0.0)
        trend_ok = (sum(votes) / len(votes)) >= self.trend_threshold

        # Gate (b): absolute-momentum confirmation (sign of trailing return).
        roc = ind.rolling_return(closes, self.abs_mom_lookback)[-1]
        if roc is None:
            return None
        mom_ok = roc > 0.0

        return trend_ok and mom_ok

    def _entry_strength(self, ticker: str, month: int) -> Decimal:
        """
        Compose the three conviction modifiers into a single entry strength:
            strength = inverse_vol_weight * vol_target_factor * seasonal_factor
        clamped to [min_strength, 1]. This is the only channel through which the
        synthesis expresses sizing — the RiskManager multiplies the cap by it.
        """
        weight = self._inverse_vol_weight(ticker)
        weight *= self._vol_target_factor()
        weight *= self._seasonal_factor(month)
        if weight > Decimal("1"):
            weight = Decimal("1")
        if weight < self.min_strength:
            weight = self.min_strength
        return weight

    # ---- main hook -------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []  # not a sleeve we trade

        closes = self._closes[ticker]
        closes.append(float(bar.close))

        # Keep the buffer bounded: need the longest trend/momentum lookback plus the
        # vol_window of returns, plus a little slack.
        need = max(max(self.trend_lookbacks), self.abs_mom_lookback)
        max_len = max(need, self.vol_window) + 5
        if len(closes) > max_len:
            del closes[:-max_len]

        # Update this sleeve's realized vol every bar (used for weighting + overlay).
        self._vol[ticker] = self._realized_vol(closes)

        want_long = self._want_long(closes, float(bar.close))
        if want_long is None:
            return []  # warming up
        held = self._held(bar.symbol)
        signals: List[SignalEvent] = []
        price = bar.close

        if want_long and not held:
            strength = self._entry_strength(ticker, bar.timestamp.month)
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
                        f"barbell{self.trend_lookbacks}>={self.trend_threshold} + "
                        f"abs-mom({self.abs_mom_lookback})>0; conviction {strength}"
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
                    reason="trend/momentum gate broke — full exit",
                )
            )

        return signals
