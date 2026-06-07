"""
apex.strategy.library.turn_of_month
=====================================
Turn-of-Month (TOM) seasonal strategy — a pure calendar-anomaly edge.

THESIS
------
Equity returns are not uniformly distributed across the trading month.
Since at least Ariel (1987), "A Monthly Effect in Stock Returns", Journal of
Financial Economics, the last few trading days of a month and the first few
trading days of the following month consistently produce outsized returns
("the turn-of-month effect"). The mechanism is plausibly institutional: month-
end cash flows (pension contributions, fund rebalances, dividend reinvestment)
concentrate demand around month boundaries. The anomaly has been replicated
across decades and international markets, though its magnitude in US large-cap
has compressed since circa 2010 as it became widely known.

STRUCTURAL ORTHOGONALITY
-------------------------
TOM is a pure CALENDAR signal — it is 'on' or 'off' based solely on the date,
with zero dependence on price levels, trends, momentum, or cross-asset
correlation. The deployed multi_asset_trend sleeve is an entirely price-driven
edge. Their correlation is expected to be near zero, making TOM a genuine
diversifier as a second sleeve rather than a redundant bet.

CALENDAR-PROXY APPROXIMATION (no look-ahead)
---------------------------------------------
The exact TOM window is "the last N trading days of month M plus the first N
trading days of month M+1". The problem: "last N trading days" of a month can
only be identified retroactively, once the month has ended and we can count back.
That is a look-ahead bias — we cannot use it in a live or correctly simulated
system.

This implementation uses a DETERMINISTIC CALENDAR-DAY PROXY:
  - LONG when:  bar.timestamp.day >= month_end_day  (default: 24)
                OR bar.timestamp.day <= month_start_days  (default: 3)
  - FLAT otherwise.

The threshold 24 means "from the 24th of the month onwards". In a typical 30/31-
day month that captures approximately the last 6–7 calendar days, which reliably
contains the last 3–4 trading days even accounting for weekends and holidays.
month_start_days=3 catches the first 3 calendar days; on a full 5-day trading
week that is always 1–3 trading days.

This proxy introduces a small day-count mismatch relative to exact trading-day
counting — it is an acknowledged simplification, not an error. Users who want
exact trading-day counting must supply an external holiday calendar, which would
introduce I/O dependencies incompatible with this architecture's determinism
requirement.

SLEEVE DESIGN
-------------
  - risk_symbol (default: SPY): long during the TOM window.
  - defensive_symbol (optional, default: IEF): held flat otherwise. If None,
    cash is held instead. The defensive sleeve is optional because some accounts
    or constraint sets may not permit holding fixed income.

POSITION AWARENESS
------------------
Exactly like multi_asset_trend: reads the actual broker-reconciled holding from
the StrategyContext each bar. No internal long/flat flag is maintained. This
means the strategy is idempotent on cold start (mid-window restart enters the
correct sleeve immediately) and never pyramids.

CAVEATS
-------
  - US-effect decay: the TOM premium in US large-cap has compressed meaningfully
    since ~2010. Build the position SMALL, monitor OOS carefully, and treat it
    as a diversifier rather than a primary edge. The Gauntlet validation
    (done separately by the researcher, not here) will confirm whether the OOS
    edge remains significant.
  - This module is a RESEARCH / LIBRARY DRAFT. It has passed unit tests but has
    NOT yet been run through the Apex Gauntlet validation pipeline. Do not deploy
    to a live account until a full Gauntlet run produces a passing grade.
  - Month-boundary arithmetic uses the bar's calendar date only. No network
    calls, no holiday APIs, no wall-clock reads. All time comes from bar
    timestamps (bar.timestamp). Determinism is preserved.

REFERENCES
----------
  Ariel, R. A. (1987). "A Monthly Effect in Stock Returns." Journal of Financial
  Economics, 18(1), 161–174.

  McConnell, J. J., & Xu, W. (2008). "Equity Returns at the Turn of the Month."
  Financial Analysts Journal, 64(2), 49–64.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy.base_strategy import BaseStrategy


class TurnOfMonthStrategy(BaseStrategy):
    """
    Long-only TOM strategy: long risk ETF during the turn-of-month window,
    optionally defensive ETF outside the window, otherwise cash.

    Args:
        strategy_id: unique id for this instance.
        risk_symbol: the ETF to hold during the TOM window (e.g. SPY).
        defensive_symbol: optional ETF to hold outside the window (e.g. IEF).
            Pass None to hold cash when not in the TOM window.
        month_end_day: calendar day-of-month at or after which the window starts
            (default 24). The window covers day >= month_end_day OR
            day <= month_start_days.
        month_start_days: calendar day-of-month at or before which the window
            is still considered "turn of new month" (default 3).
        stop_loss_pct: wide protective stop distance suggested to the RiskManager
            on risk-sleeve entries (default 8%). Defensive entries carry no stop.
    """

    def __init__(
        self,
        strategy_id: str,
        risk_symbol: Symbol,
        defensive_symbol: Optional[Symbol] = None,
        month_end_day: int = 24,
        month_start_days: int = 3,
        stop_loss_pct: Decimal = Decimal("0.08"),
    ) -> None:
        symbols = [risk_symbol]
        if defensive_symbol is not None:
            symbols.append(defensive_symbol)
        super().__init__(strategy_id, symbols)

        if not (1 <= month_end_day <= 31):
            raise ValueError("month_end_day must be in [1, 31]")
        if not (0 <= month_start_days <= 10):
            raise ValueError("month_start_days must be in [0, 10]")
        if stop_loss_pct <= Decimal("0") or stop_loss_pct >= Decimal("1"):
            raise ValueError("stop_loss_pct must be in (0, 1)")

        self.risk_symbol = risk_symbol
        self.defensive_symbol = defensive_symbol
        self.month_end_day = month_end_day
        self.month_start_days = month_start_days
        self.stop_loss_pct = stop_loss_pct

        # The strategy is STATELESS w.r.t. positions — truth comes from context.
        # We track nothing here except the last bar timestamp we processed, for
        # an internal guard against duplicate same-timestamp bars.
        self._last_ts: Optional[object] = None

    # ---- calendar logic (deterministic, no I/O) ----------------------------

    def _in_tom_window(self, bar: Bar) -> bool:
        """
        True iff bar.timestamp.day is inside the turn-of-month proxy window:
          day >= month_end_day  (tail of current month)  OR
          day <= month_start_days  (head of next month).

        Pure calendar arithmetic — no look-ahead, no external calls.
        """
        d = bar.timestamp.day
        return d >= self.month_end_day or d <= self.month_start_days

    # ---- position helpers --------------------------------------------------

    def _held(self, symbol: Symbol) -> bool:
        """
        True if we ACTUALLY hold a long position in `symbol`, read from the
        broker-reconciled context. Treats no-context as flat so isolated unit
        tests that don't bind a context still behave deterministically.
        """
        if self.context is None:
            return False
        pos = self.context.get_position(symbol)
        return pos is not None and pos.quantity > 0

    # ---- main hook ---------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        """
        Called once per completed bar for any symbol in self.symbols.

        Logic (runs on EVERY bar for EITHER sleeve symbol so the strategy can
        act promptly when either bar arrives first at a day boundary):

          in_window = day >= month_end_day OR day <= month_start_days

          if in_window:
              target_long = risk_symbol
              target_flat = defensive_symbol (if any)
          else:
              target_long = defensive_symbol (if any)
              target_flat = risk_symbol

          emit BUY target_long  iff not already held
          emit SELL target_flat iff currently held
        """
        # Only act on bars for symbols we manage.
        if bar.symbol not in self.symbols:
            return []

        # Deduplicate same-timestamp bars (e.g. if both sleeves arrive at once
        # and the engine sends the same bar twice — shouldn't happen in normal
        # operation but guard determinism).
        # We drive decisions on the risk_symbol bar primarily; if a defensive-
        # only bar arrives and we've already processed risk today, skip.
        # Actually: to keep logic simple and correct, we process EACH bar of
        # EACH sleeve separately. The position-awareness makes this idempotent.

        in_window = self._in_tom_window(bar)

        if in_window:
            want_long = self.risk_symbol
            want_flat = self.defensive_symbol
        else:
            want_long = self.defensive_symbol  # None → just go to cash
            want_flat = self.risk_symbol

        signals: List[SignalEvent] = []
        price = bar.close

        # --- Enter the desired long sleeve -----------------------------------
        if want_long is not None and not self._held(want_long):
            stop: Optional[Decimal] = None
            if want_long is self.risk_symbol:
                stop = price * (Decimal("1") - self.stop_loss_pct)

            window_label = "TOM window" if in_window else "off-window defensive"
            signals.append(
                SignalEvent(
                    symbol=want_long,
                    side=OrderSide.BUY,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=stop,
                    timestamp=bar.timestamp,
                    reason=(
                        f"TOM {window_label}: day={bar.timestamp.day} "
                        f"(end_day>={self.month_end_day} or "
                        f"start_day<={self.month_start_days})"
                    ),
                )
            )

        # --- Exit the sleeve we no longer want -------------------------------
        if want_flat is not None and self._held(want_flat):
            signals.append(
                SignalEvent(
                    symbol=want_flat,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=(
                        f"TOM exit {'risk' if want_flat is self.risk_symbol else 'defensive'}: "
                        f"day={bar.timestamp.day}"
                    ),
                )
            )

        return signals
