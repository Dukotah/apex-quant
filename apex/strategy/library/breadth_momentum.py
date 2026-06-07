"""
apex.strategy.library.breadth_momentum
=======================================
Vigilant Asset Allocation (VAA) — Breadth Momentum.

THESIS
------
Classic relative-momentum strategies (e.g. Dual Momentum) rotate to safety only
when the *target asset's own* momentum turns negative. VAA adds a BREADTH signal:
if ANY member of the offensive universe has negative momentum the system goes
fully defensive — without waiting for the leader to break.  This "early warning"
detection historically moves the rotation 1–4 months earlier in drawdowns,
materially cutting the crash participation that plagues single-asset rules.

The strategy holds AT MOST ONE asset at any time (full rotation, not blend), making
it suitable as a long-only tactical sleeve alongside trend-following.

SCORING — the "13612W" composite momentum score
    score = 12*r1 + 4*r3 + 2*r6 + 1*r12

where r_n = price_today / price_n_bars_ago - 1.  The coefficients weight recent
momentum more than long-term momentum (month/quarter/half-year/year).  Default
lookbacks: r1≈21 bars, r3≈63, r6≈126, r12≈252 (all constructor params).

BREADTH RULE (each bar, once warmed up to max lookback)
    breadth_b = # of offensive assets whose score ≤ 0
    if breadth_b >= breadth_trigger (default 1 → ANY negative):
        target = the DEFENSIVE asset with the highest score
    else:
        target = the OFFENSIVE asset with the highest score
Tie-break is deterministic: alphabetically by ticker (ascending), matching the
convention in the other rotation strategies.

POSITION AWARENESS
    The strategy reads its actual holdings from the injected StrategyContext
    (broker-reconciled) each bar. It emits:
      • SELL for the currently-held asset when the target changes.
      • BUY for the new target (with wide stop-loss, default 10%).
    It never pyramids and is idempotent on a cold start (enters the current target
    even without a fresh signal-change, because it compares target vs. actual
    holdings, not target vs. last-emitted signal).

TIMING-LUCK CAVEAT
    Keller & Keuning note the original VAA implementation is evaluated at
    month-end (end-of-month prices).  This bar-by-bar implementation rebalances
    whenever the breadth regime or ranked winner changes, which eliminates the
    strict month-end evaluation date but means results can be sensitive to the
    exact bar on which the first bar of a new month arrives — so-called
    "timing luck".  To compare against the paper, one would need to restrict
    rebalancing to the final bar of each calendar month.  This implementation
    is intentionally signal-driven (not calendar-driven) to match the framework's
    style and to allow faster reactions; accept that results will differ from the
    published backtest for this reason.

CLASSIFICATION
    Research / library draft.  Must clear the Gauntlet (7 gates) before
    promotion to deployed status.  Expected low correlation with multi_asset_trend
    (trend is sleeve-by-sleeve state; VAA is a cross-market breadth toggle).

CITATION
    Keller, W. J., & Keuning, J. W. (2017).
    Breadth momentum and the vigilant asset allocation (VAA) strategy.
    SSRN Working Paper 3002624.
    https://ssrn.com/abstract=3002624
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default universes (tickers only; Symbol objects are passed by the caller)
# ---------------------------------------------------------------------------
_DEFAULT_OFFENSIVE = ["SPY", "EFA", "EEM", "AGG"]
_DEFAULT_DEFENSIVE = ["LQD", "IEF", "SHV"]


class BreadthMomentumStrategy(BaseStrategy):
    """
    Vigilant Asset Allocation (VAA) breadth momentum rotation.

    Args:
        strategy_id:      Unique identifier for this strategy instance.
        symbols:          All tradeable symbols — MUST include every ticker in
                          both ``offensive_tickers`` and ``defensive_tickers``.
        offensive_tickers: Tickers forming the risky universe scored for
                          breadth.  Default: ['SPY', 'EFA', 'EEM', 'AGG'].
        defensive_tickers: Tickers forming the safe-haven universe.  The
                          highest-scoring member is selected when breadth is
                          bad.  Default: ['LQD', 'IEF', 'SHV'].
        lookback_1:       Bars for the 1-month return leg  (default 21).
        lookback_3:       Bars for the 3-month return leg  (default 63).
        lookback_6:       Bars for the 6-month return leg  (default 126).
        lookback_12:      Bars for the 12-month return leg (default 252).
                          Also governs the warmup requirement.
        breadth_trigger:  Number of offensive assets that must have a score
                          ≤ 0 before the entire portfolio rotates defensive.
                          Default 1 (ANY negative asset triggers defense).
        stop_loss_pct:    Wide catastrophe stop suggested to the RiskManager
                          on entry.  Default 0.10 (10%).
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        offensive_tickers: Optional[List[str]] = None,
        defensive_tickers: Optional[List[str]] = None,
        lookback_1: int = 21,
        lookback_3: int = 63,
        lookback_6: int = 126,
        lookback_12: int = 252,
        breadth_trigger: int = 1,
        stop_loss_pct: Decimal = Decimal("0.10"),
    ) -> None:
        super().__init__(strategy_id, symbols)

        self.offensive_tickers: List[str] = (
            list(offensive_tickers) if offensive_tickers is not None else list(_DEFAULT_OFFENSIVE)
        )
        self.defensive_tickers: List[str] = (
            list(defensive_tickers) if defensive_tickers is not None else list(_DEFAULT_DEFENSIVE)
        )

        if lookback_1 >= lookback_3 >= lookback_6 >= lookback_12:
            # Degenerate — but we just need lookbacks to be sensible, not strictly ordered.
            pass
        if lookback_12 < 1:
            raise ValueError("lookback_12 must be >= 1")
        if breadth_trigger < 1:
            raise ValueError("breadth_trigger must be >= 1")

        self.lookback_1 = lookback_1
        self.lookback_3 = lookback_3
        self.lookback_6 = lookback_6
        self.lookback_12 = lookback_12
        self.breadth_trigger = breadth_trigger
        self.stop_loss_pct = stop_loss_pct

        # Build a canonical ticker → Symbol map for the full universe.
        self._sym_map: Dict[str, Symbol] = {s.ticker: s for s in symbols}

        # Validate that all required tickers are present.
        all_required = set(self.offensive_tickers) | set(self.defensive_tickers)
        missing = all_required - set(self._sym_map)
        if missing:
            raise ValueError(
                f"BreadthMomentumStrategy: tickers {sorted(missing)} not found in symbols list. "
                f"Provided: {sorted(self._sym_map)}"
            )

        # Per-ticker price buffer.  We only need lookback_12 + 1 closes.
        self._closes: Dict[str, List[float]] = {s.ticker: [] for s in symbols}

        # Cache the most-recent close so we can compute the stop-loss price.
        self._latest_close: Dict[str, Decimal] = {}

        # Maximum buffer length — lookback_12 + a small overhang.
        self._max_buf = self.lookback_12 + 10

    # ------------------------------------------------------------------
    # Momentum scoring
    # ------------------------------------------------------------------

    def _trailing_return(self, ticker: str, lookback: int) -> Optional[float]:
        """
        Simple trailing return over ``lookback`` bars:
            price_today / price_lookback_bars_ago - 1
        Returns None if insufficient data.
        """
        buf = self._closes.get(ticker, [])
        if len(buf) < lookback + 1:
            return None
        past = buf[-(lookback + 1)]
        now = buf[-1]
        if past == 0.0:
            return None
        return now / past - 1.0

    def _score(self, ticker: str) -> Optional[float]:
        """
        13612W composite score:
            12*r1 + 4*r3 + 2*r6 + 1*r12

        Returns None if any leg is unavailable (warmup).  Fails closed.
        """
        r1 = self._trailing_return(ticker, self.lookback_1)
        r3 = self._trailing_return(ticker, self.lookback_3)
        r6 = self._trailing_return(ticker, self.lookback_6)
        r12 = self._trailing_return(ticker, self.lookback_12)
        if None in (r1, r3, r6, r12):
            return None
        return 12.0 * r1 + 4.0 * r3 + 2.0 * r6 + 1.0 * r12  # type: ignore[operator]

    # ------------------------------------------------------------------
    # Warmup check
    # ------------------------------------------------------------------

    def _is_warm(self) -> bool:
        """
        True when every ticker in both universes has at least lookback_12 + 1
        closes.  Fails closed: any missing data → not warm.
        """
        needed = self.lookback_12 + 1
        all_tickers = self.offensive_tickers + self.defensive_tickers
        return all(len(self._closes.get(t, [])) >= needed for t in all_tickers)

    # ------------------------------------------------------------------
    # Position awareness
    # ------------------------------------------------------------------

    def _held_ticker(self) -> Optional[str]:
        """
        Return the ticker of the single asset we currently hold (long position),
        or None if flat.  Reads from the broker-reconciled StrategyContext.
        """
        if self.context is None:
            return None
        for ticker in self.offensive_tickers + self.defensive_tickers:
            sym = self._sym_map.get(ticker)
            if sym is None:
                continue
            pos = self.context.get_position(sym)
            if pos is not None and pos.quantity > 0:
                return ticker
        return None

    # ------------------------------------------------------------------
    # Selection logic
    # ------------------------------------------------------------------

    def _select_target(self) -> Optional[str]:
        """
        Apply the VAA breadth rule and return the target ticker.

        Returns None if any score is unavailable (should not happen after warmup
        check, but fails closed regardless).
        """
        # Score every offensive asset.
        off_scores: Dict[str, float] = {}
        for ticker in self.offensive_tickers:
            s = self._score(ticker)
            if s is None:
                return None  # not warmed up — caller should have checked
            off_scores[ticker] = s

        # Count how many offensive assets have a score <= 0 (breadth signal).
        breadth_b = sum(1 for s in off_scores.values() if s <= 0.0)

        if breadth_b >= self.breadth_trigger:
            # BAD breadth — rotate fully defensive: pick highest-scoring defensive.
            def_scores: Dict[str, float] = {}
            for ticker in self.defensive_tickers:
                s = self._score(ticker)
                if s is None:
                    return None
                def_scores[ticker] = s

            # Highest score wins; tie-break: alphabetically ascending by ticker.
            target = max(
                def_scores,
                key=lambda t: (def_scores[t], sorted(self.defensive_tickers).index(t) * -1),
            )
            # Simpler deterministic tie-break: sort by (-score, ticker) and take first.
            target = sorted(def_scores, key=lambda t: (-def_scores[t], t))[0]
        else:
            # GOOD breadth — stay offensive: pick highest-scoring offensive asset.
            target = sorted(off_scores, key=lambda t: (-off_scores[t], t))[0]

        return target

    # ------------------------------------------------------------------
    # Main hook
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        """
        Process one bar.  Returns [] during warmup or when no rotation is needed.
        Returns [SELL, BUY] when the target asset changes, or [BUY] on a cold
        start entering an established regime.
        """
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []  # symbol not in our universe — ignore

        # Update price buffer.
        closes = self._closes[ticker]
        closes.append(float(bar.close))
        if len(closes) > self._max_buf:
            del closes[: len(closes) - self._max_buf]

        # Always update the latest close cache for stop-loss computation.
        self._latest_close[ticker] = bar.close

        # Not ready until every universe member is warmed up.
        if not self._is_warm():
            return []

        # Select the target for this bar.
        target = self._select_target()
        if target is None:
            return []

        # Determine what we actually hold right now.
        held = self._held_ticker()

        # No change → hold, emit nothing (never pyramid).
        if target == held:
            return []

        signals: List[SignalEvent] = []

        # Emit SELL for the currently-held asset (if any).
        if held is not None:
            sell_sym = self._sym_map[held]
            signals.append(
                SignalEvent(
                    symbol=sell_sym,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=(f"VAA exit: rotating from {held} to {target}"),
                )
            )

        # Emit BUY for the new target with a wide protective stop.
        buy_sym = self._sym_map[target]
        entry_price = self._latest_close.get(target)
        stop_loss: Optional[Decimal] = None
        if entry_price is not None:
            stop_loss = entry_price * (Decimal("1") - self.stop_loss_pct)

        # Determine which regime we are in (for the reason string).
        off_scores = {t: self._score(t) for t in self.offensive_tickers}
        breadth_b = sum(1 for s in off_scores.values() if s is not None and s <= 0.0)
        regime = "defensive" if breadth_b >= self.breadth_trigger else "offensive"

        signals.append(
            SignalEvent(
                symbol=buy_sym,
                side=OrderSide.BUY,
                strength=Decimal("1.0"),
                strategy_id=self.strategy_id,
                suggested_stop_loss=stop_loss,
                timestamp=bar.timestamp,
                reason=(
                    f"VAA {regime}: breadth_b={breadth_b}/{len(self.offensive_tickers)}; "
                    f"target={target}"
                ),
            )
        )

        logger.info(
            "%s: bar=%s regime=%s breadth_b=%d target=%s held=%s",
            self.strategy_id,
            bar.timestamp,
            regime,
            breadth_b,
            target,
            held,
        )

        return signals
