"""
apex.strategy.library.dual_momentum
===================================
Dual Momentum (Gary Antonacci) — THE ANCHOR STRATEGY.

Global Equities Momentum (GEM) rules:
  Universe: an equity ETF (e.g. SPY), an international ETF (e.g. EFA),
  and a bond/safe-haven ETF (e.g. AGG). Tickers are passed via `symbols`;
  the caller names which role each symbol plays via `equity_ticker`,
  `intl_ticker`, and `bond_ticker` constructor arguments.

  Monthly rebalance — triggered by the FIRST bar of a new calendar month
  after the warmup period has elapsed:

    1. Compute trailing lookback-month return for SPY and the intl ETF using
       the most recent `lookback_window` daily closes in the price buffer.

    2. Absolute momentum filter:
         abs_mom = SPY 12-month return.
         If abs_mom > 0:
             target = whichever of {SPY, intl} has the HIGHER 12-month return.
         Else:
             target = bond/safe-haven ETF.

    3. If target != current_holding:
           Emit SELL SignalEvent for current_holding (if any).
           Emit BUY  SignalEvent for target.
       Else:
           Emit nothing (hold, costs nothing).

  Hold exactly ONE asset at a time.

IMPLEMENTATION NOTES:
  - on_bar only acts at month boundaries; returns [] at every other bar.
  - Warmup = lookback_window + 1 bars required for EACH of the two momentum
    symbols (equity and international). Until then, returns [].
  - Protective stop is a wide catastrophe backstop (default 15% below entry)
    because the absolute-momentum switch IS the real exit discipline.
  - No wall-clock time anywhere; month detection from bar.timestamp only.
  - Risk/uncertainty fails closed — if a buffer is not yet warm, no signal.

PERFORMANCE PRIOR (be skeptical):
  - Antonacci 39-yr: 17.43%/yr, 22.7% max DD. Independent ETF replication:
    ~6.75%/yr, ~30% max DD. Plan for the lower end.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, List, Optional

from apex.strategy.base_strategy import BaseStrategy
from apex.strategy import indicators as ind
from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol

logger = logging.getLogger(__name__)


class DualMomentumStrategy(BaseStrategy):
    """
    Global Equities Momentum (GEM) — monthly absolute + relative momentum.

    Args:
        strategy_id:      Unique identifier for this strategy instance.
        symbols:          All tradeable symbols (equity ETF, intl ETF, bond ETF).
        equity_ticker:    Ticker of the US equity ETF (e.g. 'SPY').
        intl_ticker:      Ticker of the international equity ETF (e.g. 'EFA').
        bond_ticker:      Ticker of the bond / safe-haven ETF (e.g. 'AGG').
        lookback_window:  Number of daily bars used for the trailing return
                          calculation (default 252 ≈ 12 trading months).
        stop_loss_pct:    Suggested catastrophe stop distance below entry price
                          (default 0.15 = 15%).
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        equity_ticker: str = "SPY",
        intl_ticker: str = "EFA",
        bond_ticker: str = "AGG",
        lookback_window: int = 252,
        stop_loss_pct: Decimal = Decimal("0.15"),
    ) -> None:
        super().__init__(strategy_id, symbols)

        self.equity_ticker = equity_ticker
        self.intl_ticker = intl_ticker
        self.bond_ticker = bond_ticker
        self.lookback_window = lookback_window
        self.stop_loss_pct = stop_loss_pct

        # Build a quick-lookup dict from ticker -> Symbol.
        self._sym_map: Dict[str, Symbol] = {s.ticker: s for s in symbols}

        # Validate that all required tickers are present in the universe.
        for role, ticker in (
            ("equity", equity_ticker),
            ("international", intl_ticker),
            ("bond", bond_ticker),
        ):
            if ticker not in self._sym_map:
                raise ValueError(
                    f"DualMomentumStrategy: {role} ticker '{ticker}' not found "
                    f"in symbols list. Provided tickers: "
                    f"{[s.ticker for s in symbols]}"
                )

        # Per-symbol price buffer: only equity and intl need the full window;
        # bond buffer is kept too for completeness / stop-loss price reference.
        self._closes: Dict[str, List[float]] = {s.ticker: [] for s in symbols}

        # Track the last month we rebalanced on (None = never rebalanced).
        # Stored as (year, month) tuple to be timezone-independent.
        self._last_rebalance_month: Optional[tuple] = None

        # The ticker of the ETF we currently intend to hold (strategy's own
        # state — the RiskManager tracks real fills, but this drives our logic).
        self._current_holding: Optional[str] = None

        # Cache the most-recent close for each ticker so we can compute the
        # stop-loss price for BUY signals.
        self._latest_close: Dict[str, Decimal] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        """
        Process one bar. Returns signals only at the first bar of a new
        calendar month, once all required warmup data is available.
        """
        ticker = bar.symbol.ticker

        # Silently ignore symbols not in our universe.
        if ticker not in self._closes:
            return []

        # Update buffers.
        self._closes[ticker].append(float(bar.close))
        self._latest_close[ticker] = bar.close

        # Trim the equity and intl buffers to lookback_window + a small
        # overhang so memory stays bounded.
        max_len = self.lookback_window + 10
        buf = self._closes[ticker]
        if len(buf) > max_len:
            del buf[: len(buf) - max_len]

        # Month-boundary detection: act only on the FIRST bar of a new month.
        bar_month = (bar.timestamp.year, bar.timestamp.month)
        if bar_month == self._last_rebalance_month:
            return []

        # It IS a new month — but first check warmup.
        if not self._is_warm():
            # Still collecting history; note month but emit nothing.
            self._last_rebalance_month = bar_month
            return []

        # We are warmed up and it is a new month. Run the GEM logic.
        self._last_rebalance_month = bar_month
        return self._rebalance(bar)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_warm(self) -> bool:
        """
        True when BOTH the equity and international buffers hold enough data
        to compute the trailing return (lookback_window + 1 closes needed).
        Fails closed: any missing data → not warm.
        """
        needed = self.lookback_window + 1
        return (
            len(self._closes.get(self.equity_ticker, [])) >= needed
            and len(self._closes.get(self.intl_ticker, [])) >= needed
        )

    def _trailing_return(self, ticker: str) -> Optional[float]:
        """
        Compute the trailing return over the last lookback_window bars for
        `ticker`. Returns None if insufficient data (fails closed).
        """
        buf = self._closes.get(ticker, [])
        if len(buf) < self.lookback_window + 1:
            return None

        # Use the most recent lookback_window+1 values.
        window = buf[-(self.lookback_window + 1):]
        ret_series = ind.rolling_return(window, self.lookback_window)
        # rolling_return returns None for positions without enough history;
        # the last element is always valid here because we validated length.
        ret = ret_series[-1]
        return ret

    def _rebalance(self, bar: Bar) -> List[SignalEvent]:
        """Execute the GEM selection logic and emit BUY/SELL signals."""

        spy_ret = self._trailing_return(self.equity_ticker)
        intl_ret = self._trailing_return(self.intl_ticker)

        # Fail closed: if either return is unavailable, do not trade.
        if spy_ret is None or intl_ret is None:
            logger.warning(
                "%s: rebalance skipped — trailing return unavailable "
                "(spy=%s, intl=%s)",
                self.strategy_id,
                spy_ret,
                intl_ret,
            )
            return []

        # --- Absolute momentum filter ---
        # If SPY 12-month return > 0, we stay in equities; otherwise bonds.
        if spy_ret > 0.0:
            # Relative momentum: pick the better of SPY vs international.
            if spy_ret >= intl_ret:
                target_ticker = self.equity_ticker
                reason = (
                    f"GEM: abs_mom positive (SPY {spy_ret:.2%}); "
                    f"relative: SPY ({spy_ret:.2%}) >= intl ({intl_ret:.2%})"
                )
            else:
                target_ticker = self.intl_ticker
                reason = (
                    f"GEM: abs_mom positive (SPY {spy_ret:.2%}); "
                    f"relative: intl ({intl_ret:.2%}) > SPY ({spy_ret:.2%})"
                )
        else:
            target_ticker = self.bond_ticker
            reason = (
                f"GEM: abs_mom negative (SPY {spy_ret:.2%}); rotating to bonds"
            )

        logger.info(
            "%s: rebalance — current=%s, target=%s",
            self.strategy_id,
            self._current_holding,
            target_ticker,
        )

        # No change in selection → hold, emit nothing.
        if target_ticker == self._current_holding:
            return []

        signals: List[SignalEvent] = []

        # Emit SELL for current holding (if any).
        if self._current_holding is not None:
            sell_sym = self._sym_map[self._current_holding]
            signals.append(
                SignalEvent(
                    symbol=sell_sym,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=f"GEM exit: rotating from {self._current_holding} to {target_ticker}",
                )
            )

        # Emit BUY for new target.
        buy_sym = self._sym_map[target_ticker]
        entry_price = self._latest_close.get(target_ticker)
        stop_loss: Optional[Decimal] = None
        if entry_price is not None:
            stop_loss = entry_price * (Decimal("1") - self.stop_loss_pct)

        signals.append(
            SignalEvent(
                symbol=buy_sym,
                side=OrderSide.BUY,
                strength=Decimal("1.0"),
                strategy_id=self.strategy_id,
                suggested_stop_loss=stop_loss,
                timestamp=bar.timestamp,
                reason=reason,
            )
        )

        # Update internal intent state.
        self._current_holding = target_ticker

        return signals
