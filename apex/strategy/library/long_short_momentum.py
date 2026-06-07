"""
apex.strategy.library.long_short_momentum
==========================================
Market-Neutral Long/Short Cross-Sectional Momentum Strategy.

THESIS (AQR-style cross-sectional momentum):
  Rank the universe by trailing momentum (default 126-bar return, ~6 months).
  LONG the top-K ranked names (expect outperformance), SHORT the bottom-K
  ranked names (expect underperformance). Equal counts on each side make the
  book DOLLAR-NEUTRAL by construction — net market exposure is ~zero; P&L
  comes purely from spread between winners and losers.

  This is the mechanism behind AQR's MOM factor (Asness, Moskowitz, Pedersen 2013)
  and is academically robust across asset classes and decades.

RESEARCH DRAFT — not yet validated through the Gauntlet.
  Live shorting requires ``RiskConfig.allow_short = True`` (being added in a
  separate risk-core change). Without it, SELL-to-open short signals are safely
  ignored by the long-only RiskManager — all BUY (long entry) signals still
  execute normally.

POSITION-AWARE DELTA EMISSION:
  The strategy reads ``context.get_position(symbol)`` each bar and emits ONLY
  the delta needed to reach the target state. Four transitions:

  - flat + in top-K    -> BUY   (open long)
  - long + not top-K   -> SELL  (close long — SELL-to-close, not SELL-to-open)
  - flat + in bottom-K -> SELL  (open short  — SELL-to-open; needs allow_short)
  - short + not bot-K  -> BUY   (cover short — BUY-to-close)

  The distinction between "SELL-to-close a long" and "SELL-to-open a short" is
  made by reading the current position. If position.quantity > 0 we are long and
  the SELL closes it; if position is None/flat the SELL is a new short. The
  RiskManager sees the current portfolio and interprets the signal accordingly.

STOP-LOSS CONVENTION:
  - Long entries: ``suggested_stop_loss`` is BELOW entry price (loss if price falls).
  - Short entries: ``suggested_stop_loss`` is ABOVE entry price (loss if price rises).

DETERMINISM: no randomness, no I/O, no wall-clock time. Tie-breaks are resolved
by ticker alphabetically (ascending).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy.base_strategy import BaseStrategy


class LongShortMomentumStrategy(BaseStrategy):
    """
    Market-neutral cross-sectional momentum: long top-K, short bottom-K.

    Args:
        strategy_id: unique id for this instance.
        symbols: the universe to rank (all tracked simultaneously).
        mom_period: trailing-return lookback in bars (default 126, ~6 months).
        top_k: number of names to hold long (default 3).
        bot_k: number of names to short (default 3; must be <= len(symbols) - top_k).
        stop_loss_pct: protective stop distance from entry, as a fraction
            (default 0.05 = 5%). Longs: stop below price; shorts: stop above price.
        strength: fixed signal strength / conviction sent to the RiskManager
            (default 1.0). Kept simple by design — inverse-vol weighting can
            be layered on later after Gauntlet validation.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        mom_period: int = 126,
        top_k: int = 3,
        bot_k: int = 3,
        stop_loss_pct: Decimal = Decimal("0.05"),
        strength: Decimal = Decimal("1.0"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if mom_period < 2:
            raise ValueError("mom_period must be >= 2")
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if bot_k < 1:
            raise ValueError("bot_k must be >= 1")
        if top_k + bot_k > len(symbols):
            raise ValueError(
                f"top_k ({top_k}) + bot_k ({bot_k}) exceeds universe size ({len(symbols)})"
            )
        if not (Decimal("0") < stop_loss_pct < Decimal("1")):
            raise ValueError("stop_loss_pct must be in (0, 1)")
        if not (Decimal("0") < strength <= Decimal("1")):
            raise ValueError("strength must be in (0, 1]")

        self.mom_period = mom_period
        self.top_k = top_k
        self.bot_k = bot_k
        self.stop_loss_pct = stop_loss_pct
        self.strength = strength

        # Per-symbol rolling close buffers (plain float for arithmetic speed).
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        # Latest momentum score per ticker (None while warming up).
        self._mom: Dict[str, Optional[float]] = {s.ticker: None for s in symbols}

    # ---- momentum metric -------------------------------------------------

    def _momentum(self, closes: list[float]) -> Optional[float]:
        """
        Total return over the last ``mom_period`` bars.

        Returns ``closes[-1] / closes[-(mom_period+1)] - 1``, or None if there
        is not yet enough history. Requires mom_period + 1 data points.
        """
        if len(closes) < self.mom_period + 1:
            return None
        past = closes[-(self.mom_period + 1)]
        if past == 0:
            return None
        return closes[-1] / past - 1.0

    # ---- ranking ---------------------------------------------------------

    def _ranked(self) -> list[str]:
        """
        Return all tickers with a momentum score, sorted best-to-worst.
        Tie-break: ascending ticker (alphabetical) — deterministic.
        """
        scored = [(t, m) for t, m in self._mom.items() if m is not None]
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return [t for t, _ in scored]

    def _in_top_k(self, ticker: str) -> bool:
        ranked = self._ranked()
        return ticker in set(ranked[: self.top_k])

    def _in_bot_k(self, ticker: str) -> bool:
        ranked = self._ranked()
        return ticker in set(ranked[-self.bot_k :]) if len(ranked) >= self.bot_k else False

    # ---- position helpers ------------------------------------------------

    def _position_quantity(self, symbol: Symbol) -> Decimal:
        """
        Current held quantity for ``symbol`` from the broker-reconciled context.
        Returns Decimal('0') if flat or no context bound.
        Positive = long, negative = short.
        """
        if self.context is None:
            return Decimal("0")
        pos = self.context.get_position(symbol)
        if pos is None:
            return Decimal("0")
        return pos.quantity

    def _is_long(self, symbol: Symbol) -> bool:
        return self._position_quantity(symbol) > Decimal("0")

    def _is_short(self, symbol: Symbol) -> bool:
        return self._position_quantity(symbol) < Decimal("0")

    def _is_flat(self, symbol: Symbol) -> bool:
        return self._position_quantity(symbol) == Decimal("0")

    # ---- main hook -------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []  # unknown symbol

        # Accumulate close history; trim to avoid unbounded growth.
        closes = self._closes[ticker]
        closes.append(float(bar.close))
        max_len = self.mom_period + 10
        if len(closes) > max_len:
            del closes[:-max_len]

        # Update momentum score for this ticker.
        self._mom[ticker] = self._momentum(closes)

        # Check if all tickers have enough history for a valid ranking.
        # We need at least top_k + bot_k tickers with scores so the ranking
        # is meaningful. Warmup returns [] per the golden rule.
        scored_count = sum(1 for m in self._mom.values() if m is not None)
        if scored_count < self.top_k + self.bot_k:
            return []

        price = bar.close
        signals: List[SignalEvent] = []

        want_long = self._in_top_k(ticker)
        want_short = self._in_bot_k(ticker)
        is_long = self._is_long(bar.symbol)
        is_short = self._is_short(bar.symbol)
        is_flat = self._is_flat(bar.symbol)

        # --- Long-side delta ----------------------------------------------
        if want_long and is_flat:
            # Open new long. Stop BELOW entry price.
            stop = price * (Decimal("1") - self.stop_loss_pct)
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.BUY,
                    strength=self.strength,
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=stop,
                    timestamp=bar.timestamp,
                    reason=f"top-{self.top_k} momentum long entry; stop below price",
                )
            )
        elif want_long and is_short:
            # Cover the existing short first (BUY-to-close). Stop above price
            # (the covering buy exits the risk; stop above is protective for the
            # short that is being closed — use same convention as short entry but
            # this closes the position, so stop is above).
            stop = price * (Decimal("1") + self.stop_loss_pct)
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.BUY,
                    strength=self.strength,
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=stop,
                    timestamp=bar.timestamp,
                    reason=f"top-{self.top_k} momentum: cover short (migrating to long)",
                )
            )
        elif not want_long and is_long:
            # Exit the long (SELL-to-close). No stop on exit signal.
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=f"dropped out of top-{self.top_k}: close long",
                )
            )

        # --- Short-side delta ---------------------------------------------
        if want_short and is_flat:
            # Open new short (SELL-to-open). Stop ABOVE entry price.
            # Requires RiskConfig.allow_short=True; ignored by long-only RM.
            stop = price * (Decimal("1") + self.stop_loss_pct)
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.SELL,
                    strength=self.strength,
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=stop,
                    timestamp=bar.timestamp,
                    reason=f"bottom-{self.bot_k} momentum short entry; stop above price",
                )
            )
        elif want_short and is_long:
            # Close the long first (SELL-to-close) before going short.
            # (Separate BUY signals for the eventual long-to-short flip are
            # handled in the next bar after the position is flat — keeps
            # state transitions atomic and safe.)
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=f"bottom-{self.bot_k} momentum: close long before short",
                )
            )
        elif not want_short and is_short:
            # Cover the short (BUY-to-close).
            stop = price * (Decimal("1") + self.stop_loss_pct)
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.BUY,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=stop,
                    timestamp=bar.timestamp,
                    reason=f"rose out of bottom-{self.bot_k}: cover short",
                )
            )

        return signals
