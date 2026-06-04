"""
apex.strategy.library.etf_rotation
==================================
Weekly ETF Momentum Rotation — THE DIVERSIFIER.

THESIS: Rank a basket of sector/asset ETFs by recent return; own the top N,
volatility-scaled; rebalance weekly. Low turnover, diversified, with an
absolute-momentum risk-off overlay.

THE RULES:
  Universe: liquid sector ETFs (XLK, XLF, XLE, XLV, XLY, XLI, XLP, ...) + a bond
            ETF (AGG/IEF) as the risk-off sleeve.
  Weekly, at the first bar of a new ISO week (once warmed up):
    1. Rank universe by trailing N-month (default 3-month ≈ 63 trading days) total return.
    2. Select top K (default 1–3) ETFs.
    3. Absolute-momentum overlay: if NO ETF has positive trailing return, rotate
       entirely to the bond ETF (the last symbol in `symbols` is treated as the
       risk-off sleeve).
    4. Size each selected ETF inversely proportional to its recent realized
       volatility (lower vol → larger strength). Normalize so strengths sum to 1
       and express each as a Decimal in (0, 1].
    5. Emit SELL for any currently-held ETF no longer selected.
       Emit BUY for newly selected ETFs (with strength, reason, stop_loss).
       Emit nothing if the selection is unchanged (avoids unnecessary churn).

IMPLEMENTATION NOTES:
  - Week-boundary detection: bar.timestamp ISO week number changes relative to the
    last bar's ISO week. The first bar of a new week triggers the rebalance.
  - Warmup: requires at least `momentum_period + 1` closes per symbol before
    any rebalance fires. Return [] during warmup.
  - Volatility estimate: annualised realized vol = stddev of last `vol_period`
    daily log returns × sqrt(252). Minimum vol floor of 0.001 prevents division
    by zero.
  - Determinism: pure function of bar history. No I/O, no datetime.now().

WHY IT FITS: weekly turnover, sector diversification, built-in risk-off overlay.
"""
from __future__ import annotations

import logging
import math
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)

# Minimum realized-vol floor to prevent division by zero.
_VOL_FLOOR: float = 0.001


class ETFRotationStrategy(BaseStrategy):
    """
    Weekly cross-sectional momentum rotation with volatility-scaled sizing
    and an absolute-momentum risk-off overlay.

    Args:
        strategy_id: Unique identifier for this strategy instance.
        symbols: All tradeable symbols. By convention, the LAST symbol is
                 treated as the risk-off (bond) sleeve. The remaining symbols
                 form the sector universe that is ranked by momentum.
        momentum_period: Trailing period (in bars) for return ranking.
                         Default 63 ≈ 3 months of trading days.
        vol_period: Trailing period (in bars) for realized-volatility estimate.
                    Default 21 ≈ 1 month.
        top_k: Maximum number of ETFs to hold simultaneously. Default 1.
        stop_loss_pct: Suggested stop-loss as a fraction below entry price.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        momentum_period: int = 63,
        vol_period: int = 21,
        top_k: int = 1,
        stop_loss_pct: Decimal = Decimal("0.05"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if len(symbols) < 2:
            raise ValueError(
                "ETFRotationStrategy requires at least 2 symbols "
                "(1 sector ETF + 1 bond/risk-off ETF)"
            )
        if momentum_period <= 0:
            raise ValueError("momentum_period must be positive")
        if vol_period <= 0:
            raise ValueError("vol_period must be positive")
        if top_k <= 0:
            raise ValueError("top_k must be positive")

        self.momentum_period = momentum_period
        self.vol_period = vol_period
        self.top_k = top_k
        self.stop_loss_pct = stop_loss_pct

        # Last symbol is the risk-off (bond) sleeve by convention.
        self._bond_symbol: Symbol = symbols[-1]
        # Sector universe: everything except the bond sleeve.
        self._sector_symbols: List[Symbol] = symbols[:-1]

        # Per-symbol rolling close buffer. We only keep what we need.
        self._max_buf: int = momentum_period + vol_period + 5
        self._closes: Dict[str, List[float]] = {
            s.ticker: [] for s in symbols
        }

        # Internal holdings tracking (tickers currently held).
        self._holdings: Set[str] = set()

        # ISO (year, week) tuple of the last bar we processed.
        self._last_iso_week: Optional[Tuple[int, int]] = None

    # ------------------------------------------------------------------
    # Core hook
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        """
        Called on every incoming bar. Returns signals only on week boundaries
        once the strategy is warmed up.
        """
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []

        # Accumulate close.
        buf = self._closes[ticker]
        buf.append(float(bar.close))
        if len(buf) > self._max_buf:
            del buf[: len(buf) - self._max_buf]

        # Detect week boundary using ISO calendar.
        iso = bar.timestamp.isocalendar()
        current_week: Tuple[int, int] = (iso[0], iso[1])  # (year, week)
        new_week = self._last_iso_week is not None and current_week != self._last_iso_week
        self._last_iso_week = current_week

        if not new_week:
            return []

        # Warmup check: every symbol must have enough bars.
        if not self._is_warmed_up():
            logger.debug(
                "%s: warmup — not enough bars for full momentum + vol window",
                self.strategy_id,
            )
            return []

        return self._rebalance(bar)

    # ------------------------------------------------------------------
    # Rebalance logic
    # ------------------------------------------------------------------

    def _rebalance(self, trigger_bar: Bar) -> List[SignalEvent]:
        """
        Compute the target portfolio and emit the minimal set of signals
        required to reach it.
        """
        momentum_by_ticker = self._compute_momentum()
        vol_by_ticker = self._compute_vol()

        # Sector ETFs with valid momentum readings.
        ranked: List[Tuple[str, float]] = [
            (t, m)
            for t, m in momentum_by_ticker.items()
            if t in {s.ticker for s in self._sector_symbols}
            and m is not None
        ]
        # Sort descending by momentum.
        ranked.sort(key=lambda x: x[1], reverse=True)

        # Determine target selection.
        bond_ticker = self._bond_symbol.ticker
        target_tickers: List[str]

        if not ranked or ranked[0][1] <= 0.0:
            # Absolute-momentum overlay: all negative → risk-off to bonds.
            target_tickers = [bond_ticker]
            logger.info(
                "%s: all ETF momentum non-positive — rotating to bond sleeve %s",
                self.strategy_id,
                bond_ticker,
            )
        else:
            # Take top K among those with POSITIVE momentum.
            positive = [(t, m) for t, m in ranked if m > 0.0]
            top = positive[: self.top_k]
            target_tickers = [t for t, _ in top]
            logger.info(
                "%s: top-%d selection: %s",
                self.strategy_id,
                self.top_k,
                target_tickers,
            )

        target_set = set(target_tickers)

        # Short-circuit if nothing changed.
        if target_set == self._holdings:
            logger.debug(
                "%s: selection unchanged (%s) — no signals emitted",
                self.strategy_id,
                target_set,
            )
            return []

        # Compute inverse-vol strengths for incoming positions.
        strengths = self._inverse_vol_strengths(target_tickers, vol_by_ticker)

        signals: List[SignalEvent] = []

        # --- Emit SELLs for dropped holdings ---
        dropped = self._holdings - target_set
        for ticker in sorted(dropped):
            sym = self._symbol_for(ticker)
            if sym is None:
                continue
            signals.append(
                SignalEvent(
                    symbol=sym,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=trigger_bar.timestamp,
                    reason=f"ETF rotation: {ticker} dropped from top-{self.top_k} selection",
                )
            )
            logger.info("%s: SELL %s (dropped)", self.strategy_id, ticker)

        # --- Emit BUYs for newly entered positions ---
        added = target_set - self._holdings
        for ticker in sorted(added):
            sym = self._symbol_for(ticker)
            if sym is None:
                continue
            closes = self._closes.get(ticker, [])
            if not closes:
                continue
            last_price = Decimal(str(closes[-1]))
            stop = last_price * (Decimal("1") - self.stop_loss_pct)
            strength = strengths.get(ticker, Decimal("1.0"))
            mom_val = momentum_by_ticker.get(ticker)
            reason = (
                f"ETF rotation: {ticker} selected"
                + (
                    f" (3-mo return {mom_val:.1%})"
                    if mom_val is not None
                    else " (risk-off sleeve)"
                )
            )
            signals.append(
                SignalEvent(
                    symbol=sym,
                    side=OrderSide.BUY,
                    strength=strength,
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=stop,
                    timestamp=trigger_bar.timestamp,
                    reason=reason,
                )
            )
            logger.info(
                "%s: BUY %s strength=%s stop=%s",
                self.strategy_id,
                ticker,
                strength,
                stop,
            )

        # Update internal holdings to the new target.
        self._holdings = target_set

        return signals

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_warmed_up(self) -> bool:
        """True only when every symbol has enough bars for momentum AND vol."""
        needed = self.momentum_period + 1
        for sym in self.symbols:
            if len(self._closes.get(sym.ticker, [])) < needed:
                return False
        return True

    def _compute_momentum(self) -> Dict[str, Optional[float]]:
        """
        Trailing `momentum_period`-bar return for each symbol.
        Returns (current - past) / past. None if insufficient data.
        """
        result: Dict[str, Optional[float]] = {}
        for sym in self.symbols:
            buf = self._closes.get(sym.ticker, [])
            if len(buf) < self.momentum_period + 1:
                result[sym.ticker] = None
                continue
            past = buf[-self.momentum_period - 1]
            current = buf[-1]
            result[sym.ticker] = (current / past - 1.0) if past != 0.0 else None
        return result

    def _compute_vol(self) -> Dict[str, float]:
        """
        Annualised realized volatility from the last `vol_period` daily log returns.
        Returns at least `_VOL_FLOOR` to prevent division by zero.
        """
        result: Dict[str, float] = {}
        for sym in self.symbols:
            buf = self._closes.get(sym.ticker, [])
            # Need vol_period+1 prices to get vol_period returns.
            if len(buf) < self.vol_period + 1:
                result[sym.ticker] = _VOL_FLOOR
                continue
            recent = buf[-(self.vol_period + 1):]
            log_rets: List[float] = []
            for i in range(1, len(recent)):
                if recent[i - 1] > 0.0 and recent[i] > 0.0:
                    log_rets.append(math.log(recent[i] / recent[i - 1]))
            if len(log_rets) < 2:
                result[sym.ticker] = _VOL_FLOOR
                continue
            mean = sum(log_rets) / len(log_rets)
            variance = sum((r - mean) ** 2 for r in log_rets) / (len(log_rets) - 1)
            daily_vol = math.sqrt(variance)
            ann_vol = daily_vol * math.sqrt(252.0)
            result[sym.ticker] = max(ann_vol, _VOL_FLOOR)
        return result

    def _inverse_vol_strengths(
        self,
        target_tickers: List[str],
        vol_by_ticker: Dict[str, float],
    ) -> Dict[str, Decimal]:
        """
        Compute inverse-volatility weights normalized to sum to 1.0,
        expressed as Decimals rounded to 6 decimal places.
        """
        if not target_tickers:
            return {}

        inv_vols: Dict[str, float] = {}
        for t in target_tickers:
            vol = vol_by_ticker.get(t, _VOL_FLOOR)
            inv_vols[t] = 1.0 / max(vol, _VOL_FLOOR)

        total = sum(inv_vols.values())
        if total <= 0.0:
            # Equal weight fallback.
            eq = Decimal("1") / Decimal(str(len(target_tickers)))
            return {t: eq for t in target_tickers}

        strengths: Dict[str, Decimal] = {}
        for t in target_tickers:
            raw = inv_vols[t] / total
            # Clamp to (0, 1] and round to 6dp for clean Decimal arithmetic.
            clamped = min(max(raw, 1e-6), 1.0)
            strengths[t] = Decimal(str(round(clamped, 6)))
        return strengths

    def _symbol_for(self, ticker: str) -> Optional[Symbol]:
        """Look up the Symbol object for a given ticker."""
        for sym in self.symbols:
            if sym.ticker == ticker:
                return sym
        return None
