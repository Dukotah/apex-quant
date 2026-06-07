"""
apex.strategy.library.bond_carry
=================================
Bond Carry (Yield-Curve Slope) Strategy — research/library reference.

THESIS — THE CARRY MECHANISM
When the yield curve is positively sloped (10Y yield > 3M yield), owning duration
earns two simultaneous returns that have NOTHING to do with price trend:

  1. Carry: you clip the yield spread each day (you borrow short, you lend long).
  2. Roll-down: as time passes a 10-year bond becomes a 9.9-year bond, which on a
     normal upward-sloping curve is priced at a LOWER yield (higher price). You
     capture this roll-down without the price ever needing to move.

When the curve inverts (3M > 10Y), both of these mechanisms reverse: carry is
negative and roll-down works against you.  The correct allocation is to own the
SHORT end (defensive: SHV, BIL) and earn the now-higher short rate, while avoiding
duration losses if the curve eventually steepens from a rate cut.

This is a CARRY edge, not a trend/momentum edge.  It is structurally uncorrelated
to moving-average trend signals because the SLOPE of the curve can be positive
while price momentum on any given bond ETF is negative (and vice versa).  That
is the diversification property we want as a second edge alongside multi_asset_trend.

SIGNAL SOURCES
The strategy ingests two NON-TRADEABLE yield symbols alongside the tradeable ETFs:
  ^TNX — CBOE 10-Year Treasury Yield.  Yahoo Finance serves this as a price series
          where close = yield in percent (e.g. 4.25 means 4.25%).
  ^IRX — CBOE 13-Week Treasury Bill Yield.  Same convention.

These symbols are included in the strategy's `symbols` list so the data feed
delivers their bars normally.  The strategy stores their closes but NEVER emits a
SignalEvent for them — they are signal-only ride-alongs.

RULES (long/flat, position-aware — mirrors multi_asset_trend)
  - On each bar update the most recently seen yield for that ticker (if it is a
    yield symbol) or the ETF price (if it is a tradeable symbol).
  - Warmup: until BOTH yields have been observed at least once, return [].
  - Compute slope = tnx_latest - irx_latest (both in percent; e.g. 4.25 - 5.10 = -0.85).
  - State machine:
        slope > inversion_buffer  →  RISK-ON : want long_etf, want flat short_etf
        slope <= 0                →  RISK-OFF : want flat long_etf, want short_etf
        0 < slope <= inversion_buffer →  HOLD current state (hysteresis zone)
  - For each tradeable ETF, compare the desired holding to what context.get_position
    reports (broker-reconciled). Emit BUY only when not already held; emit SELL only
    when held but no longer wanted.  Never pyramid; idempotent on cold start.

HYSTERESIS (inversion_buffer)
  `inversion_buffer` (default Decimal("0")) creates a dead-band around zero so tiny
  slope fluctuations do not cause constant flipping.  Default 0 = pure sign rule.
  Recommended live value: Decimal("0.25") (25 bps) — enough to absorb daily noise.
  With the default of 0 the strategy enters duration as soon as slope > 0 and exits
  only when slope <= 0 (entry threshold > exit threshold → asymmetric, no flip band).

SUGGESTED STOP-LOSS
  Bond ETFs (IEF, TLT, SHV, BIL) are low-volatility.  A 5% stop on IEF covers
  roughly 2-3 months of adverse duration movement on a 50-bps rate shock, which is
  a reasonable outer bound.  For SHV/BIL the same 5% is ultra-conservative (they
  almost never move 2%).  The RiskManager may tighten this further.

LIVE / GAUNTLET NOTE
  This is a research/library reference.  Gauntlet validation (Sharpe, drawdown, OOS
  walk-forward, Monte Carlo) must be run separately by the framework operator before
  promotion to the deployed strategy set.  The data plumbing for ^TNX/^IRX bars must
  be verified in the data feed layer (Yahoo Finance provider) before live use.

Deterministic: pure function of bar history. No I/O, no datetime.now(), no RNG.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, List, Optional, Set

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)

# Default stop distance for bond ETF entries (5% — conservative for low-vol instruments).
_DEFAULT_STOP_PCT = Decimal("0.05")


class BondCarryStrategy(BaseStrategy):
    """
    Yield-curve carry strategy: long duration when curve is positively sloped,
    long defensive short-duration when inverted.

    Args:
        strategy_id:        Unique identifier for this strategy instance.
        symbols:            Full universe including BOTH tradeable ETFs AND the two
                            non-tradeable yield symbols (^TNX, ^IRX).  The strategy
                            will silently ignore any bar for a symbol not in this list.
        long_etf:           The DURATION sleeve (e.g. IEF or TLT).  Bought when the
                            curve is positively sloped (carry + roll-down positive).
        short_etf:          The DEFENSIVE sleeve (e.g. SHV or BIL).  Bought when the
                            curve is inverted or flat (earn short rate, avoid duration).
        ten_year_ticker:    Ticker string for the 10-year yield series (default "^TNX").
                            Bar.close is interpreted as yield in percent.
        three_month_ticker: Ticker string for the 3-month yield series (default "^IRX").
                            Bar.close is interpreted as yield in percent.
        inversion_buffer:   Dead-band in yield-percent units (default Decimal("0")).
                            The curve must be MORE positive than this to trigger a
                            risk-on entry; it must reach <= 0 to trigger risk-off.
                            Set to e.g. Decimal("0.25") for a 25-bps hysteresis band.
        stop_loss_pct:      Fractional stop distance attached to each entry (default 5%).
                            The RiskManager validates and may tighten this.

    Symbol convention
    -----------------
    Pass all five (or more) symbols in `symbols`:
        [long_etf_symbol, short_etf_symbol, tnx_symbol, irx_symbol]
    The strategy checks bar.symbol.ticker against ten_year_ticker / three_month_ticker
    and treats those bars as yield observations only — no order is ever emitted for them.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        *,
        long_etf: Symbol,
        short_etf: Symbol,
        ten_year_ticker: str = "^TNX",
        three_month_ticker: str = "^IRX",
        inversion_buffer: Decimal = Decimal("0"),
        stop_loss_pct: Decimal = _DEFAULT_STOP_PCT,
    ) -> None:
        super().__init__(strategy_id, symbols)

        # Validate tradeable ETFs are in the declared universe.
        universe_tickers: Set[str] = {s.ticker for s in symbols}
        if long_etf.ticker not in universe_tickers:
            raise ValueError(f"long_etf '{long_etf.ticker}' must be included in symbols")
        if short_etf.ticker not in universe_tickers:
            raise ValueError(f"short_etf '{short_etf.ticker}' must be included in symbols")
        if long_etf.ticker == short_etf.ticker:
            raise ValueError("long_etf and short_etf must be different symbols")
        if inversion_buffer < Decimal("0"):
            raise ValueError("inversion_buffer must be >= 0")
        if stop_loss_pct <= Decimal("0") or stop_loss_pct >= Decimal("1"):
            raise ValueError("stop_loss_pct must be in (0, 1)")

        self.long_etf = long_etf
        self.short_etf = short_etf
        self.ten_year_ticker = ten_year_ticker
        self.three_month_ticker = three_month_ticker
        self.inversion_buffer = inversion_buffer
        self.stop_loss_pct = stop_loss_pct

        # Tradeable tickers we will ever emit signals for.
        self._tradeable: Set[str] = {long_etf.ticker, short_etf.ticker}

        # Lookup map: ticker -> Symbol object (for building SignalEvents).
        self._sym_map: Dict[str, Symbol] = {s.ticker: s for s in symbols}

        # Latest yield observations.  None until we have seen a bar for each.
        self._tnx: Optional[Decimal] = None  # 10-year yield (%)
        self._irx: Optional[Decimal] = None  # 3-month yield (%)

        # Last seen bar price for each tradeable ETF (needed for stop calculation).
        self._last_price: Dict[str, Decimal] = {}

        # Current "regime" so we stay in the hysteresis zone without flipping.
        # "risk_on" → duration ETF is wanted; "risk_off" → defensive ETF is wanted;
        # None → undecided (only before first yield observations).
        self._regime: Optional[str] = None  # "risk_on" | "risk_off" | None

    # ------------------------------------------------------------------ helpers

    def _held(self, symbol: Symbol) -> bool:
        """True if we actually hold a long position in symbol (context-reconciled)."""
        if self.context is None:
            return False
        pos = self.context.get_position(symbol)
        return pos is not None and pos.quantity > 0

    def _compute_regime(self) -> Optional[str]:
        """
        Determine the target regime from current yield observations.

        Returns:
            "risk_on"  — curve is steep enough to earn positive carry (slope > buffer).
            "risk_off" — curve is flat or inverted (slope <= 0).
            None       — yields not yet observed (warmup); no decision possible.

        The dead-band between 0 and inversion_buffer preserves the CURRENT regime:
        if we are already risk-on, a slope of 0.10 with buffer=0.25 keeps us risk-on
        (do not flip to risk-off just because slope shrank); if we are risk-off, a
        slope of 0.10 with buffer=0.25 does NOT flip us to risk-on (insufficient
        evidence).  Only a slope > buffer triggers a risk-on entry; only a slope <= 0
        triggers a risk-off entry.  This is asymmetric — by design — and matches how
        real carry strategies handle curve-flattening noise.
        """
        if self._tnx is None or self._irx is None:
            return None

        slope = self._tnx - self._irx

        if slope > self.inversion_buffer:
            return "risk_on"
        if slope <= Decimal("0"):
            return "risk_off"
        # In the dead-band: preserve current regime (or None if no regime yet).
        return self._regime

    def _make_signal(
        self,
        symbol: Symbol,
        side: OrderSide,
        timestamp,
        reason: str,
    ) -> SignalEvent:
        price = self._last_price.get(symbol.ticker)
        stop: Optional[Decimal] = None
        if side == OrderSide.BUY and price is not None:
            stop = price * (Decimal("1") - self.stop_loss_pct)
        return SignalEvent(
            symbol=symbol,
            side=side,
            strength=Decimal("1.0"),  # carry is binary: in or out
            strategy_id=self.strategy_id,
            suggested_stop_loss=stop,
            timestamp=timestamp,
            reason=reason,
        )

    # ------------------------------------------------------------------ main hook

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        """
        Process one bar.  Yield bars update internal state; ETF bars may trigger
        regime-change signals.  Returns [] for any ticker not in the universe.
        """
        ticker = bar.symbol.ticker

        # Silently discard bars that are outside our declared universe entirely.
        if ticker not in self._sym_map:
            return []

        # --- Update observations ---

        if ticker == self.ten_year_ticker:
            # bar.close IS the yield in percent for ^TNX.
            self._tnx = bar.close
            return []  # yield bars never trigger orders

        if ticker == self.three_month_ticker:
            # bar.close IS the yield in percent for ^IRX.
            self._irx = bar.close
            return []  # yield bars never trigger orders

        if ticker not in self._tradeable:
            # Symbol is in the universe but not a yield or tradeable ticker.
            return []

        # Record the latest ETF price for stop-loss calculations.
        self._last_price[ticker] = bar.close

        # --- Warmup guard ---
        if self._tnx is None or self._irx is None:
            return []

        # --- Determine regime ---
        new_regime = self._compute_regime()
        if new_regime is None:
            return []

        # Commit the regime (may be unchanged if in the dead-band).
        self._regime = new_regime

        # --- Emit delta signals vs. actual holdings ---
        signals: List[SignalEvent] = []
        slope = self._tnx - self._irx

        want_long = new_regime == "risk_on"
        want_short = new_regime == "risk_off"

        # Only act when this bar's ticker is relevant to the current target.
        # The strategy emits signals on a per-ticker basis as bars arrive,
        # so we check each ETF against its desired state when its bar lands.

        if ticker == self.long_etf.ticker:
            held = self._held(self.long_etf)
            if want_long and not held:
                signals.append(
                    self._make_signal(
                        self.long_etf,
                        OrderSide.BUY,
                        bar.timestamp,
                        reason=(
                            f"curve positively sloped: {self.ten_year_ticker} "
                            f"{float(self._tnx):.2f}% - {self.three_month_ticker} "
                            f"{float(self._irx):.2f}% = {float(slope):.2f}% "
                            f"(buffer {float(self.inversion_buffer):.2f}%)"
                        ),
                    )
                )
            elif held and not want_long:
                signals.append(
                    self._make_signal(
                        self.long_etf,
                        OrderSide.SELL,
                        bar.timestamp,
                        reason=(
                            f"curve inverted/flat: slope {float(slope):.2f}% <= 0; exiting duration"
                        ),
                    )
                )

        elif ticker == self.short_etf.ticker:
            held = self._held(self.short_etf)
            if want_short and not held:
                signals.append(
                    self._make_signal(
                        self.short_etf,
                        OrderSide.BUY,
                        bar.timestamp,
                        reason=(
                            f"curve inverted/flat: {self.ten_year_ticker} "
                            f"{float(self._tnx):.2f}% - {self.three_month_ticker} "
                            f"{float(self._irx):.2f}% = {float(slope):.2f}%; "
                            f"rotating to defensive"
                        ),
                    )
                )
            elif held and not want_short:
                signals.append(
                    self._make_signal(
                        self.short_etf,
                        OrderSide.SELL,
                        bar.timestamp,
                        reason=(
                            f"curve re-steepened: slope {float(slope):.2f}% "
                            f"> buffer {float(self.inversion_buffer):.2f}%; "
                            f"exiting defensive"
                        ),
                    )
                )

        return signals
