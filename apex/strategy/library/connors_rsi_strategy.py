"""
apex.strategy.library.connors_rsi_strategy
===========================================
ConnorsRSI-style mean reversion — LONG-ONLY, position-aware research candidate.

⚠️  UNVALIDATED RESEARCH CANDIDATE. This strategy has NOT been through the
    Gauntlet (docs/VALIDATION_GAUNTLET.md). It is a hypothesis to be tested, not
    a deployable edge. Do not allocate capital to it until it clears the gates.

THE IDEA (Larry Connors' "ConnorsRSI" family):
  Classic RSI(2) mean reversion buys oversold dips and exits on a snap-back. The
  ConnorsRSI refinement blends THREE oversold lenses into one composite so a
  single noisy reading can't trigger an entry:

    1. A short-period price RSI (default RSI(3)) — the raw momentum oversold read.
    2. A "streak" RSI — RSI applied to the run-length of consecutive up/down
       closes (down days accumulate a negative streak, up days a positive one).
       This penalizes price that has fallen for *many days in a row*, which is the
       hallmark of an exhausted, mean-reverting selloff.
    3. A percent-rank of today's one-bar return over a lookback window — how
       extreme today's move is relative to its own recent history (0..100).

  The composite is the simple average of the three (each on a 0..100 scale):

      connors_rsi = (rsi(close, rsi_period)
                     + rsi(streak, streak_rsi_period)
                     + percent_rank(ret_1, rank_window)) / 3

  ENTRY  (flat → long): connors_rsi <= entry_threshold  (deeply oversold).
  EXIT   (long → flat): price RSI(rsi_period) >= exit_threshold (recovered),
                        i.e. the snap-back has happened.

POSITION AWARENESS (matches multi_asset_trend):
  This strategy holds NO internal long/flat flag. Every bar it reads its ACTUAL
  holding from the broker-reconciled StrategyContext and emits only the DELTA
  toward its target state. Consequences:
    - Correct on a cold start / restart / missed cron cycle (decision is
      "target vs. what I actually hold", not "did I see the entry bar?").
    - Never pyramids: already-long + still-oversold emits nothing.
    - Idempotent: re-dispatching the same bar yields the same (possibly empty)
      signal set.

STOPS (golden rule 7 — every BUY carries a stop):
  Each BUY attaches an ATR-based protective stop:  entry - atr_mult * ATR.
  During the ATR warmup (insufficient bars) it falls back to a fixed percentage
  stop  entry * (1 - stop_loss_pct)  so a BUY is NEVER emitted without a stop.
  The RiskManager remains the sole sizer/validator; this is only a suggestion.

DETERMINISM & PURITY: no datetime.now(), no randomness, no I/O. Indicator math
reuses apex.strategy.indicators (RSI); the streak and percent-rank helpers are
implemented privately here (we deliberately do NOT import any parallel ind_*
modules). Indicator-layer math uses float; suggested stop prices use Decimal.
No look-ahead — every read uses only data up to and including the current bar.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class ConnorsRSIStrategy(BaseStrategy):
    """
    ConnorsRSI-style mean reversion (long-only, position-aware).

    Args:
        strategy_id: unique id for this instance.
        symbols: the universe to trade (each tracked independently).
        rsi_period: lookback for the short price RSI (default 3, the Connors read).
        streak_rsi_period: lookback for the streak RSI (default 2).
        rank_window: lookback (in one-bar returns) for the percent-rank (default 100).
        entry_threshold: composite ConnorsRSI must be <= this to BUY (default 10).
        exit_threshold: price RSI must be >= this to exit a long (default 70).
        atr_period: ATR lookback for the protective stop (default 14).
        atr_mult: stop distance = atr_mult * ATR below entry (default 2.5).
        stop_loss_pct: percentage stop fallback during ATR warmup (default 0.05).
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        rsi_period: int = 3,
        streak_rsi_period: int = 2,
        rank_window: int = 100,
        entry_threshold: float = 10.0,
        exit_threshold: float = 70.0,
        atr_period: int = 14,
        atr_mult: Decimal = Decimal("2.5"),
        stop_loss_pct: Decimal = Decimal("0.05"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if rsi_period < 1:
            raise ValueError("rsi_period must be >= 1")
        if streak_rsi_period < 1:
            raise ValueError("streak_rsi_period must be >= 1")
        if rank_window < 1:
            raise ValueError("rank_window must be >= 1")
        if not (0.0 <= entry_threshold <= 100.0):
            raise ValueError("entry_threshold must be in [0, 100]")
        if not (0.0 <= exit_threshold <= 100.0):
            raise ValueError("exit_threshold must be in [0, 100]")
        if atr_period < 1:
            raise ValueError("atr_period must be >= 1")
        if atr_mult <= 0:
            raise ValueError("atr_mult must be positive")
        if not (Decimal("0") < stop_loss_pct < Decimal("1")):
            raise ValueError("stop_loss_pct must be in (0, 1)")
        self.rsi_period = rsi_period
        self.streak_rsi_period = streak_rsi_period
        self.rank_window = rank_window
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.atr_period = atr_period
        self.atr_mult = atr_mult
        self.stop_loss_pct = stop_loss_pct
        # Per-symbol rolling OHLC buffers. NOTE: no internal long/flat flag — the
        # real position is read from the (broker-reconciled) context each bar.
        self._highs: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._lows: Dict[str, list[float]] = {s.ticker: [] for s in symbols}
        self._closes: Dict[str, list[float]] = {s.ticker: [] for s in symbols}

    # ---- private indicator helpers ---------------------------------------

    @staticmethod
    def _streak_series(closes: list[float]) -> list[float]:
        """
        Run-length of consecutive same-direction closes, signed.

        Up close → positive run extends (+1, +2, ...). Down close → negative run
        extends (-1, -2, ...). Unchanged close → resets to 0. The first bar has no
        prior close, so it is 0. Length matches `closes`.
        """
        out: list[float] = [0.0] * len(closes)
        streak = 0.0
        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                streak = streak + 1.0 if streak > 0 else 1.0
            elif closes[i] < closes[i - 1]:
                streak = streak - 1.0 if streak < 0 else -1.0
            else:
                streak = 0.0
            out[i] = streak
        return out

    def _percent_rank(self, returns: list[float]) -> Optional[float]:
        """
        Percent-rank (0..100) of the most recent one-bar return within the
        trailing `rank_window` returns BEFORE it (look-back only — no look-ahead).

        Convention (Connors): the share of prior returns strictly less than the
        current return, scaled to 0..100. A deeply negative (worst) move ranks
        near 0 → contributes oversold weight to the composite. Returns None until
        there are enough prior observations to rank against.
        """
        if len(returns) < 2:
            return None
        current = returns[-1]
        history = returns[-(self.rank_window + 1):-1]  # up to rank_window priors
        if not history:
            return None
        less = sum(1 for r in history if r < current)
        return 100.0 * less / len(history)

    def _connors_rsi(
        self, closes: list[float]
    ) -> tuple[Optional[float], Optional[float]]:
        """
        Compute (composite_connors_rsi, price_rsi) for the latest bar from the
        close buffer, or (None, None)/(None, price_rsi) while warming up.

        composite = mean of price RSI, streak RSI, and the return percent-rank,
        each on a 0..100 scale. price_rsi is returned separately for the exit test.
        """
        price_rsi_series = ind.rsi(closes, self.rsi_period)
        price_rsi = price_rsi_series[-1] if price_rsi_series else None
        if price_rsi is None:
            return None, None

        streak = self._streak_series(closes)
        streak_rsi_series = ind.rsi(streak, self.streak_rsi_period)
        streak_rsi = streak_rsi_series[-1] if streak_rsi_series else None
        if streak_rsi is None:
            return None, price_rsi

        # One-bar simple returns (close-to-close), then percent-rank the latest.
        rets = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1] != 0
        ]
        rank = self._percent_rank(rets)
        if rank is None:
            return None, price_rsi

        composite = (price_rsi + streak_rsi + rank) / 3.0
        return composite, price_rsi

    def _suggested_stop(
        self, ticker: str, entry: Decimal
    ) -> Decimal:
        """
        ATR-based protective stop  entry - atr_mult * ATR, falling back to a fixed
        percentage stop during ATR warmup so every BUY carries a stop. The stop is
        clamped to stay strictly positive (a stop at/below 0 would be meaningless).
        """
        highs = self._highs[ticker]
        lows = self._lows[ticker]
        closes = self._closes[ticker]
        atr_series = ind.atr(highs, lows, closes, self.atr_period)
        atr_val = atr_series[-1] if atr_series else None
        if atr_val is not None and atr_val > 0:
            stop = entry - self.atr_mult * Decimal(str(atr_val))
        else:
            stop = entry * (Decimal("1") - self.stop_loss_pct)
        # Fail closed: never suggest a non-positive stop.
        if stop <= 0:
            stop = entry * (Decimal("1") - self.stop_loss_pct)
        return stop

    # ---- position --------------------------------------------------------

    def _held(self, symbol: Symbol) -> bool:
        """
        True if we ACTUALLY hold a long position in `symbol`, read from the
        broker-reconciled context. With no context bound (isolated use) we treat
        ourselves as flat — the harness always binds/refreshes before dispatch.
        """
        if self.context is None:
            return False
        pos = self.context.get_position(symbol)
        return pos is not None and pos.quantity > 0

    # ---- main hook -------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []  # not a symbol we trade

        highs = self._highs[ticker]
        lows = self._lows[ticker]
        closes = self._closes[ticker]
        highs.append(float(bar.high))
        lows.append(float(bar.low))
        closes.append(float(bar.close))

        # Keep buffers bounded: need the rank window of returns plus slack for the
        # RSI/ATR warmups and the streak history.
        max_len = max(self.rank_window, self.atr_period) + max(
            self.rsi_period, self.streak_rsi_period
        ) + 5
        if len(closes) > max_len:
            del highs[:-max_len]
            del lows[:-max_len]
            del closes[:-max_len]

        composite, price_rsi = self._connors_rsi(closes)
        if price_rsi is None:
            return []  # still warming up

        held = self._held(bar.symbol)
        signals: List[SignalEvent] = []
        price = bar.close

        # ENTRY: flat + deeply oversold composite → BUY (with mandatory stop).
        if not held:
            if composite is None:
                return []  # composite still warming up; no entry yet
            if composite <= self.entry_threshold:
                stop = self._suggested_stop(ticker, price)
                # Conviction scales with how far below the threshold we are: deeper
                # oversold → stronger. Clamped to [0, 1]; threshold of 0 → full.
                if self.entry_threshold > 0:
                    conv = 1.0 - (composite / self.entry_threshold)
                else:
                    conv = 1.0
                strength = Decimal(str(min(1.0, max(0.0, conv))))
                if strength <= 0:
                    strength = Decimal("0.01")  # floor: a fired signal is tradeable
                signals.append(
                    SignalEvent(
                        symbol=bar.symbol,
                        side=OrderSide.BUY,
                        strength=strength,
                        strategy_id=self.strategy_id,
                        suggested_stop_loss=stop,
                        timestamp=bar.timestamp,
                        reason=(
                            f"ConnorsRSI {composite:.1f}<={self.entry_threshold:.1f} "
                            f"oversold (priceRSI{self.rsi_period}={price_rsi:.1f})"
                        ),
                    )
                )
        # EXIT: long + price RSI recovered → SELL (full exit).
        elif price_rsi >= self.exit_threshold:
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=(
                        f"priceRSI{self.rsi_period}={price_rsi:.1f}"
                        f">={self.exit_threshold:.1f} recovered; exit"
                    ),
                )
            )

        return signals
