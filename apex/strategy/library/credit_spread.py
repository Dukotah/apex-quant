"""
apex.strategy.library.credit_spread
====================================
Credit-Spread Regime Defensive-Rotation Strategy.

THESIS
------
Credit markets (high-yield vs. investment-grade bonds) historically lead equity
drawdowns by weeks because credit-market participants — institutions with access to
private information — re-price default risk before equity volatility erupts.  The
HYG/LQD price ratio is therefore a real-time risk-appetite gauge: when high-yield
underperforms investment-grade (ratio falls, spreads widen), markets are pricing in
rising credit stress.  That stress tends to arrive in equity portfolios later, giving
this signal a HEAD-START over price-trend indicators that fire only after equity
weakness has already begun.

The HYG/LQD spread regime is FUNDAMENTALLY different from price-trend: it measures
CREDIT MARKET STRUCTURE, not equity-price momentum.  Practitioner evidence (Gary
Antonacci, Meb Faber, Verdad Capital research) supports low-to-negative correlation
with trend signals over medium horizons, with the regime flip often preceding the
trend break by 2–6 weeks.

IMPLEMENTATION NOTE — HYG & LQD as RIDE-ALONG GAUGES
------------------------------------------------------
HYG and LQD are NON-TRADEABLE in this strategy.  Their bar closes are consumed
purely as the credit-spread signal; no SignalEvent is ever emitted for them.  This
mirrors the cross_asset_value pattern for ride-along symbols: they appear in the
strategy's `symbols` universe so the engine routes their bars here, but the strategy
guard-clauses return [] immediately for any ticker not in its tradeable set.

RATIO ACCUMULATION
------------------
On each bar, the strategy updates the latest known close for the gauge that just
arrived.  A single ratio observation (HYG_close / LQD_close) is recorded when
BOTH gauges have been seen on the SAME bar timestamp.  Specifically the strategy
tracks the most-recent timestamp for which a ratio was already recorded; the ratio
is only appended once per unique bar timestamp, so feeding HYG and LQD bars for
the same date yields exactly one ratio point.  This keeps the rolling window
correctly sized at one point per trading day.

REGIME RULE WITH HYSTERESIS
----------------------------
Each bar, compute ratio = HYG_close / LQD_close.  Over a rolling window of
`ratio_window` (default 252) ratio observations, compute a z-score:

    z = (ratio - mean(window)) / pstdev(window)

A low (negative) z-score means HYG is cheap relative to LQD → spreads are WIDE →
risk-OFF.  A high z-score means HYG is expensive relative to LQD → spreads are
TIGHT → risk-ON.

The hysteresis dead-band prevents whipsaw:
  - Enter RISK-OFF (target = IEF) when z <= `enter_z`   (default –1.0)
  - Return to RISK-ON (target = SPY) when z >= `exit_z`  (default –0.5)
  - Between the two thresholds, hold the CURRENT regime (no flip).

POSITION-AWARE DELTA SIGNALS
-----------------------------
Like multi_asset_trend and cross_asset_value, this strategy emits DELTAS vs. actual
holdings (broker-reconciled via StrategyContext), so it is:
  - Idempotent on cold-start (enters an established regime immediately)
  - Never pyramiding (held + same target → nothing)
  - Correct across restarts / missed cron cycles

WARMUP
------
Returns [] until the rolling window has at least `ratio_window` ratio observations
AND both HYG and LQD closes have been seen.  Any bar arriving before that is
silently accumulated.

STATUS
------
RESEARCH / library draft — requires Gauntlet validation (walk-forward OOS, Monte
Carlo) before deployment.  Thesis is practitioner-evidenced (Gary Antonacci,
Verdad Capital), not peer-reviewed factor literature.  Treat results as
directionally indicative; correlation with the deployed trend edge is a key
empirical question to verify in the Gauntlet run.

RIGOR CAVEAT
------------
The practitioner evidence is real and internally consistent, but thinner than
peer-reviewed factor research.  The signal may not survive transaction-cost
analysis or may exhibit regime instability post-discovery.  Gate 4 (Monte Carlo)
is particularly important here.

DATA ASSUMPTION
---------------
The engine feeds HYG and LQD bar closes from the same data pipeline as tradeable
ETFs.  This strategy assumes those price series are available and correctly
normalized.  Verify data plumbing (Alpaca free tier returns HYG/LQD daily bars)
before live deployment.

Deterministic, no I/O, stdlib-only math.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy.base_strategy import BaseStrategy


class CreditSpreadRegimeStrategy(BaseStrategy):
    """
    Rotate between a risk ETF (default SPY) and a defensive ETF (default IEF)
    based on the z-scored HYG/LQD price ratio.

    Args:
        strategy_id:     Unique id for this instance.
        symbols:         Full symbol list including risk_sym, defensive_sym, hyg_sym, lqd_sym.
                         HYG and LQD are ride-along gauges — bars for them are consumed but
                         no orders are ever emitted for them.
        risk_sym:        The risk-on sleeve (matched by ticker 'SPY' if None).
        defensive_sym:   The defensive sleeve (matched by ticker 'IEF' if None).
        hyg_sym:         High-yield ETF gauge (matched by ticker 'HYG' if None).
        lqd_sym:         Investment-grade ETF gauge (matched by ticker 'LQD' if None).
        ratio_window:    Rolling window (bars) for z-score of the HYG/LQD ratio (default 252).
        enter_z:         Z-score threshold to ENTER risk-off (target IEF); trigger when z <= enter_z
                         (default -1.0).  More negative = stronger signal required to go defensive.
        exit_z:          Z-score threshold to RETURN to risk-on (target SPY); trigger when
                         z >= exit_z (default -0.5).  Must be > enter_z to create a dead-band.
        stop_loss_pct:   Wide protective stop suggested to the RiskManager on entries (default 8%).
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        risk_sym: Optional[Symbol] = None,
        defensive_sym: Optional[Symbol] = None,
        hyg_sym: Optional[Symbol] = None,
        lqd_sym: Optional[Symbol] = None,
        ratio_window: int = 252,
        enter_z: Decimal = Decimal("-1.0"),
        exit_z: Decimal = Decimal("-0.5"),
        stop_loss_pct: Decimal = Decimal("0.08"),
    ) -> None:
        super().__init__(strategy_id, symbols)

        # Resolve default symbols from universe if not passed explicitly.
        self._risk_sym = risk_sym or self._find_sym(symbols, "SPY")
        self._defensive_sym = defensive_sym or self._find_sym(symbols, "IEF")
        self._hyg_sym = hyg_sym or self._find_sym(symbols, "HYG")
        self._lqd_sym = lqd_sym or self._find_sym(symbols, "LQD")

        if self._risk_sym is None:
            raise ValueError("risk_sym not found in symbols — pass it explicitly or include SPY")
        if self._defensive_sym is None:
            raise ValueError(
                "defensive_sym not found in symbols — pass it explicitly or include IEF"
            )
        if self._hyg_sym is None:
            raise ValueError("hyg_sym not found in symbols — pass it explicitly or include HYG")
        if self._lqd_sym is None:
            raise ValueError("lqd_sym not found in symbols — pass it explicitly or include LQD")

        if ratio_window < 2:
            raise ValueError("ratio_window must be >= 2")
        if exit_z <= enter_z:
            raise ValueError(
                "exit_z must be > enter_z to create a hysteresis dead-band "
                f"(enter_z={enter_z}, exit_z={exit_z})"
            )
        if stop_loss_pct <= Decimal("0") or stop_loss_pct >= Decimal("1"):
            raise ValueError("stop_loss_pct must be in (0, 1)")

        self.ratio_window = ratio_window
        self.enter_z = enter_z
        self.exit_z = exit_z
        self.stop_loss_pct = stop_loss_pct

        # Fast membership sets.
        self._gauge_tickers = {self._hyg_sym.ticker, self._lqd_sym.ticker}
        self._tradeable_tickers = {self._risk_sym.ticker, self._defensive_sym.ticker}

        # State: latest known closes for the gauges.
        self._hyg_close: Optional[float] = None
        self._lqd_close: Optional[float] = None
        # Track the bar timestamp at which the ratio was last recorded, so that
        # feeding both HYG and LQD bars for the SAME trading day appends exactly
        # ONE ratio point (not two).
        self._last_ratio_ts: Optional[datetime] = None
        # Rolling buffer of ratio observations (one per trading day).
        self._ratio_history: list[float] = []
        # Current regime: 'risk' or 'defensive'; None = not yet bootstrapped.
        self._regime: Optional[str] = None

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _find_sym(symbols: List[Symbol], ticker: str) -> Optional[Symbol]:
        """Return the first Symbol whose ticker matches, or None."""
        for s in symbols:
            if s.ticker == ticker:
                return s
        return None

    def _held(self, symbol: Symbol) -> bool:
        """True iff we actually hold a long position in `symbol`."""
        if self.context is None:
            return False
        pos = self.context.get_position(symbol)
        return pos is not None and pos.quantity > 0

    def _try_record_ratio(self, bar_ts: datetime) -> None:
        """
        If both gauge closes are available and we have not yet recorded a ratio
        for `bar_ts`, append one ratio point and update `_last_ratio_ts`.

        This ensures exactly one ratio observation per trading day regardless of
        the order in which HYG and LQD bars arrive.
        """
        if self._hyg_close is None or self._lqd_close is None:
            return
        if self._lqd_close == 0.0:
            return
        if self._last_ratio_ts == bar_ts:
            return  # already recorded for this timestamp
        ratio = self._hyg_close / self._lqd_close
        self._ratio_history.append(ratio)
        self._last_ratio_ts = bar_ts
        # Bound the buffer.
        max_len = self.ratio_window + 5
        if len(self._ratio_history) > max_len:
            del self._ratio_history[:-max_len]

    def _compute_zscore(self) -> Optional[float]:
        """
        Z-score of the latest ratio vs. the rolling `ratio_window`.
        Returns None if fewer than `ratio_window` observations exist.
        """
        if len(self._ratio_history) < self.ratio_window:
            return None
        window = self._ratio_history[-self.ratio_window :]
        mean = statistics.mean(window)
        stdev = statistics.pstdev(window)
        if stdev == 0.0:
            return 0.0
        return (window[-1] - mean) / stdev

    def _update_regime(self, z: float) -> None:
        """
        Apply the hysteresis rule to update `_regime` from z.

          - Bootstrap (first computable z): set regime from current z.
          - risk-ON  and z <= enter_z → flip to defensive.
          - risk-OFF and z >= exit_z  → flip to risk-on.
          - Inside the dead-band      → hold current regime.
        """
        enter = float(self.enter_z)
        exit_ = float(self.exit_z)

        if self._regime is None:
            self._regime = "defensive" if z <= enter else "risk"
            return

        if self._regime == "risk" and z <= enter:
            self._regime = "defensive"
        elif self._regime == "defensive" and z >= exit_:
            self._regime = "risk"
        # else: inside dead-band — no change.

    # ---- main hook ---------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker

        # --- Update gauge closes and try to record a ratio observation ---
        if ticker == self._hyg_sym.ticker:
            self._hyg_close = float(bar.close)
            self._try_record_ratio(bar.timestamp)
            return []  # gauges are never traded

        if ticker == self._lqd_sym.ticker:
            self._lqd_close = float(bar.close)
            self._try_record_ratio(bar.timestamp)
            return []  # gauges are never traded

        # --- Non-tradeable, non-gauge tickers are ignored ---
        if ticker not in self._tradeable_tickers:
            return []

        # --- Warmup: need a full window and both gauges seen ---
        z = self._compute_zscore()
        if z is None:
            return []

        # Update regime (with hysteresis).
        self._update_regime(z)

        # Determine target and other sleeves.
        if self._regime == "risk":
            target = self._risk_sym
            other = self._defensive_sym
        else:
            target = self._defensive_sym
            other = self._risk_sym

        # Only the TARGET sleeve's bar drives entry; the OTHER sleeve's bar
        # drives only the exit (if it's currently held but shouldn't be).
        if ticker != target.ticker:
            if ticker == other.ticker and self._held(other):
                return [
                    SignalEvent(
                        symbol=other,
                        side=OrderSide.SELL,
                        strength=Decimal("1.0"),
                        strategy_id=self.strategy_id,
                        timestamp=bar.timestamp,
                        reason=(
                            f"credit-spread regime flip: z={z:.3f}, exiting "
                            f"{other.ticker} in favour of {target.ticker}"
                        ),
                    )
                ]
            return []

        # We are on the TARGET sleeve's bar.
        signals: List[SignalEvent] = []
        held_target = self._held(target)
        held_other = self._held(other)

        # Exit the OTHER sleeve if still held (rotation).
        if held_other:
            signals.append(
                SignalEvent(
                    symbol=other,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=(
                        f"credit-spread regime: z={z:.3f}, rotating from "
                        f"{other.ticker} → {target.ticker}"
                    ),
                )
            )

        # Enter the TARGET sleeve if not already held.
        if not held_target:
            stop = bar.close * (Decimal("1") - self.stop_loss_pct)
            regime_label = "risk-ON" if self._regime == "risk" else "risk-OFF"
            spread_label = "tight spreads" if self._regime == "risk" else "wide spreads"
            signals.append(
                SignalEvent(
                    symbol=target,
                    side=OrderSide.BUY,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=stop,
                    timestamp=bar.timestamp,
                    reason=(
                        f"credit-spread regime={regime_label}: z={z:.3f} "
                        f"({spread_label}), entering {target.ticker}"
                    ),
                )
            )

        return signals
