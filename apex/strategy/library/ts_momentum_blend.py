"""
apex.strategy.library.ts_momentum_blend
========================================
Time-Series Momentum Blend — a LONG-ONLY, multi-lookback momentum strategy.

Classic time-series (absolute) momentum says: an asset that has gone up over the
trailing window tends to keep going up. A single lookback is noisy and regime-
fragile (a 12-month signal misses fast turns; a 1-month signal whipsaws). This
strategy BLENDS several lookbacks (default 21 / 63 / 126 / 252 bars ≈ 1 / 3 / 6 /
12 trading months) into one conviction score, so the decision is robust to any one
horizon being wrong.

HOW THE SCORE IS BUILT (per symbol, every bar):
  1. For each lookback L, read the trailing return r_L from
     `indicators.rolling_return` (NEVER recomputed inline — one tested source of
     truth). Until the buffer holds L+1 closes, that sub-score is unavailable.
  2. Each return is squashed into a bounded sub-score in (-1, 1) with tanh, so a
     single explosive horizon can't dominate the blend:
         sub_L = tanh(r_L / scale)
  3. The sub-scores are combined as a fixed weighted average (longer horizons
     weighted a touch heavier by default — they're the structural trend; shorter
     ones the tactical confirm). Weights renormalize over only the lookbacks that
     are currently warm, so the blend stays meaningful during partial warmup.
         score = sum(w_L * sub_L) / sum(w_L over warm L)
     `score` is therefore always in (-1, 1).

THE RULES (long / flat — matches the deployed book, no shorting):
  - score >  buy_threshold  and flat  -> BUY. strength = scaled, clamped (0, 1].
  - score <= 0              and held  -> SELL (full exit, strength 1.0).
  - otherwise: emit nothing (hold or stay flat — costs nothing, no pyramiding).

  The asymmetric thresholds (enter only on real conviction, exit the moment
  momentum turns non-positive) bias toward capital preservation, which is the
  point of an absolute-momentum overlay.

POSITION AWARENESS (like multi_asset_trend): the strategy holds NO internal
long/flat flag. It reads its REAL holding from the broker-reconciled
StrategyContext each bar and emits the delta against a target state. That makes
it correct on a cold start, a restart, or a missed cron cycle — it will enter an
already-established momentum regime without needing to witness the crossover, and
it never double-buys what it already holds.

STOP-LOSS: an ATR-based protective stop is suggested on every BUY:
      stop = entry - atr_mult * ATR(period)
The RiskManager validates/overrides it (the strategy never sizes or places).
While ATR is still warming up, a percentage fallback stop is used so a BUY always
carries a stop (rule 7: no stop = no order).

DETERMINISM & PURITY: no wall-clock time (timestamps come from the bar), no
randomness, no I/O. Statistical math uses float to match indicators.py /
validation/metrics.py; the suggested stop and signal strength are Decimal
because they cross the event boundary toward money/risk logic.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy


class TimeSeriesMomentumBlend(BaseStrategy):
    """
    Long-only, multi-lookback time-series momentum with a blended conviction score.

    Args:
        strategy_id:   Unique identifier for this strategy instance.
        symbols:       The tradeable universe.
        lookbacks:     Trailing-return windows (in bars) to blend. Must be a
                       non-empty list of positive ints (default 21/63/126/252).
        weights:       Optional per-lookback blend weights (same length as
                       `lookbacks`). Default: linearly increasing so longer
                       horizons count slightly more. Renormalized over warm
                       lookbacks each bar.
        scale:         Return scale fed to tanh (default 0.20). A trailing return
                       equal to `scale` maps to sub-score tanh(1) ≈ 0.76.
        buy_threshold: Minimum blended score (in (0, 1)) to open a long.
        strength_floor:Minimum BUY strength so a marginal-but-valid entry is still
                       tradeable after the risk manager scales by strength.
        atr_period:    Lookback for the ATR-based protective stop (default 14).
        atr_mult:      Stop distance = atr_mult * ATR below entry (default 3).
        stop_loss_pct: Percentage fallback stop used while ATR is warming up.
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        lookbacks: Optional[List[int]] = None,
        weights: Optional[List[float]] = None,
        scale: float = 0.20,
        buy_threshold: float = 0.10,
        strength_floor: Decimal = Decimal("0.10"),
        atr_period: int = 14,
        atr_mult: Decimal = Decimal("3"),
        stop_loss_pct: Decimal = Decimal("0.10"),
    ) -> None:
        super().__init__(strategy_id, symbols)

        lookbacks = list(lookbacks) if lookbacks is not None else [21, 63, 126, 252]
        if not lookbacks:
            raise ValueError("lookbacks must be a non-empty list")
        if any(lb <= 0 for lb in lookbacks):
            raise ValueError("every lookback must be a positive integer")

        if weights is None:
            # Linearly increasing weights: 1, 2, 3, ... favouring longer horizons.
            weights = [float(i + 1) for i in range(len(lookbacks))]
        if len(weights) != len(lookbacks):
            raise ValueError("weights must have the same length as lookbacks")
        if any(w <= 0 for w in weights):
            raise ValueError("every weight must be positive")

        if scale <= 0:
            raise ValueError("scale must be positive")
        if not (0.0 < buy_threshold < 1.0):
            raise ValueError("buy_threshold must be in the open interval (0, 1)")
        if atr_period <= 0:
            raise ValueError("atr_period must be positive")
        if atr_mult <= 0:
            raise ValueError("atr_mult must be positive")

        self.lookbacks = lookbacks
        self.weights = weights
        self.scale = scale
        self.buy_threshold = buy_threshold
        self.strength_floor = strength_floor
        self.atr_period = atr_period
        self.atr_mult = atr_mult
        self.stop_loss_pct = stop_loss_pct

        # The longest lookback drives warmup / buffer bounding.
        self._max_lookback = max(lookbacks)

        # Per-symbol rolling OHLC close buffers (close drives momentum + ATR's
        # close component; high/low drive ATR's true range).
        self._closes: Dict[str, List[float]] = {s.ticker: [] for s in symbols}
        self._highs: Dict[str, List[float]] = {s.ticker: [] for s in symbols}
        self._lows: Dict[str, List[float]] = {s.ticker: [] for s in symbols}

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _blended_score(self, closes: List[float]) -> Optional[float]:
        """
        Blend the tanh-squashed trailing returns over every WARM lookback into a
        single conviction score in (-1, 1). Returns None if no lookback is warm
        yet (fails closed — never trade on missing data).
        """
        num = 0.0
        den = 0.0
        for lookback, weight in zip(self.lookbacks, self.weights):
            # rolling_return needs lookback+1 closes; skip horizons not yet warm.
            if len(closes) < lookback + 1:
                continue
            window = closes[-(lookback + 1):]
            ret = ind.rolling_return(window, lookback)[-1]
            if ret is None:
                continue
            sub = math.tanh(ret / self.scale)
            num += weight * sub
            den += weight
        if den == 0.0:
            return None
        return num / den

    def _suggested_stop(self, symbol: Symbol, entry: Decimal) -> Decimal:
        """
        ATR-based protective stop below entry. Falls back to a percentage stop
        while ATR is still warming up so a BUY ALWAYS carries a stop. The stop is
        floored at zero (a stop can never be negative).
        """
        ticker = symbol.ticker
        atr_series = ind.atr(
            self._highs[ticker],
            self._lows[ticker],
            self._closes[ticker],
            self.atr_period,
        )
        atr_val = atr_series[-1] if atr_series else None

        if atr_val is not None and atr_val > 0:
            distance = self.atr_mult * Decimal(str(atr_val))
        else:
            distance = entry * self.stop_loss_pct

        stop = entry - distance
        if stop < Decimal("0"):
            stop = Decimal("0")
        return stop

    def _held(self, symbol: Symbol) -> bool:
        """
        True iff we ACTUALLY hold a long position, read from the broker-reconciled
        context. With no context bound (isolated use) we treat ourselves as flat —
        the harness binds and refreshes the context before each dispatch.
        """
        if self.context is None:
            return False
        pos = self.context.get_position(symbol)
        return pos is not None and pos.quantity > 0

    # ------------------------------------------------------------------
    # Main hook
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in self._closes:
            return []  # not a symbol we trade

        closes = self._closes[ticker]
        highs = self._highs[ticker]
        lows = self._lows[ticker]
        closes.append(float(bar.close))
        highs.append(float(bar.high))
        lows.append(float(bar.low))

        # Keep buffers bounded: need the longest lookback (+1 for the return) and
        # ATR's prior-close, with a little slack.
        max_len = max(self._max_lookback, self.atr_period) + 5
        if len(closes) > max_len:
            del closes[:-max_len]
            del highs[:-max_len]
            del lows[:-max_len]

        # Warmup: at least the SHORTEST lookback must be computable. Until then,
        # the blend is unavailable and we fail closed.
        score = self._blended_score(closes)
        if score is None:
            return []

        held = self._held(bar.symbol)
        price = bar.close
        signals: List[SignalEvent] = []

        # Target state: long iff the blended momentum is convincingly positive.
        if score > self.buy_threshold and not held:
            strength = self._score_to_strength(score)
            stop = self._suggested_stop(bar.symbol, price)
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.BUY,
                    strength=strength,
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=stop,
                    timestamp=bar.timestamp,
                    reason=(
                        f"TS-momentum blend score {score:.3f} > "
                        f"{self.buy_threshold:.3f}; strength {strength}"
                    ),
                )
            )
        elif score <= 0.0 and held:
            signals.append(
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=f"TS-momentum blend score {score:.3f} <= 0; exiting",
                )
            )

        return signals

    def _score_to_strength(self, score: float) -> Decimal:
        """
        Map a blended score (already in (0, 1] when this is called) to a BUY
        strength, clamped to (0, 1] and floored at `strength_floor` so a valid
        entry is always tradeable. score is positive here by construction.
        """
        strength = Decimal(str(score))
        if strength > Decimal("1"):
            strength = Decimal("1")
        if strength < self.strength_floor:
            strength = self.strength_floor
        return strength
