"""
apex.strategy.library.trend_bond
================================
Trend-Following with a Bond Risk-Off Sleeve — the always-invested tactical core.

THESIS: own the risk asset (e.g. SPY) while it trends up — defined as its close
being above its slow SMA — and rotate ENTIRELY into a bond sleeve (e.g. AGG) when
it falls below. This is the classic time-series-momentum / Faber GTAA mechanic,
but with the risk-off capital parked in bonds rather than cash.

WHY THE BOND SLEEVE MATTERS (the cash-drag fix): a long/flat trend strategy sits
in zero-return cash during downtrends. Those flat days dilute its Sharpe — the
mean daily return is averaged over many zeros — while an always-invested benchmark
has no such drag. Holding bonds instead keeps the strategy invested in a real,
positive-expected-return asset during risk-off periods, so its risk-adjusted
performance is measured on the same footing as buy-and-hold. It is also simply
how real tactical allocation works: you don't hold cash, you hold T-bills/bonds.

THE RULES (daily bars):
  - Compute the slow SMA of the RISK asset's closes.
  - Once warmed up, on each risk-asset bar decide the target:
        close > SMA_slow  → hold the RISK asset (uptrend)
        close ≤ SMA_slow  → hold the BOND sleeve (risk-off)
  - On a change of target, emit SELL(current holding) + BUY(target). Otherwise
    emit nothing (no churn). The strategy is ALWAYS holding exactly one asset
    after warmup.

CONVENTION: symbols[0] is the risk asset, symbols[-1] is the bond sleeve.
Determinism: pure function of bar history; no I/O, no datetime.now().
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, List, Optional

from apex.core.events import SignalEvent
from apex.core.models import Bar, OrderSide, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class TrendBondStrategy(BaseStrategy):
    """
    Long the risk asset above its slow SMA; rotate to the bond sleeve below it.

    Args:
        strategy_id:   unique id for this instance.
        symbols:       [risk_asset, bond_sleeve] — risk first, bond last (>= 2).
        slow_period:   SMA lookback defining the trend (default 200 ≈ 10 months).
        stop_loss_pct: protective stop distance attached to each entry (the
                       backtester treats it as metadata; the strategy's own
                       trend flip is what drives exits).
    """

    def __init__(
        self,
        strategy_id: str,
        symbols: List[Symbol],
        slow_period: int = 200,
        stop_loss_pct: Decimal = Decimal("0.10"),
    ) -> None:
        super().__init__(strategy_id, symbols)
        if len(symbols) < 2:
            raise ValueError("TrendBondStrategy requires [risk_asset, bond_sleeve]")
        if slow_period < 2:
            raise ValueError("slow_period must be >= 2")
        self.risk: Symbol = symbols[0]
        self.bond: Symbol = symbols[-1]
        self.slow_period = slow_period
        self.stop_loss_pct = stop_loss_pct

        self._risk_closes: List[float] = []
        self._last_close: Dict[str, float] = {}
        self._holding: Optional[str] = None  # ticker currently held, or None

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        ticker = bar.symbol.ticker
        if ticker not in (self.risk.ticker, self.bond.ticker):
            return []

        self._last_close[ticker] = float(bar.close)
        if ticker == self.risk.ticker:
            self._risk_closes.append(float(bar.close))
            if len(self._risk_closes) > self.slow_period + 5:
                del self._risk_closes[: len(self._risk_closes) - (self.slow_period + 5)]

        # The trend trigger is computed on the risk asset; only decide on its bars.
        if ticker != self.risk.ticker:
            return []
        if len(self._risk_closes) < self.slow_period:
            return []

        sma = ind.sma(self._risk_closes, self.slow_period)[-1]
        if sma is None:
            return []

        price = self._risk_closes[-1]
        target = self.risk.ticker if price > sma else self.bond.ticker
        if target == self._holding:
            return []

        signals: List[SignalEvent] = []

        # Exit the current holding first (frees capital for the rotation).
        if self._holding is not None:
            held_sym = self.risk if self._holding == self.risk.ticker else self.bond
            signals.append(
                SignalEvent(
                    symbol=held_sym,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    timestamp=bar.timestamp,
                    reason=f"trend flip: exit {self._holding}",
                )
            )

        # Enter the target (need a known price to attach a stop).
        target_sym = self.risk if target == self.risk.ticker else self.bond
        tprice = self._last_close.get(target)
        if tprice is None:
            # Target's price not seen yet (e.g. bond bar hasn't arrived) — emit only
            # the exit this bar; the entry follows once we have a price. Rare.
            logger.debug(
                "%s: no price yet for target %s; deferring entry", self.strategy_id, target
            )
            return signals

        stop = Decimal(str(tprice)) * (Decimal("1") - self.stop_loss_pct)
        reason = (
            "trend up: hold risk asset"
            if target == self.risk.ticker
            else "trend down: risk-off to bonds"
        )
        signals.append(
            SignalEvent(
                symbol=target_sym,
                side=OrderSide.BUY,
                strength=Decimal("1.0"),
                strategy_id=self.strategy_id,
                suggested_stop_loss=stop,
                timestamp=bar.timestamp,
                reason=reason,
            )
        )
        self._holding = target
        logger.info(
            "%s: rotate -> %s (price %.2f vs SMA%d %.2f)",
            self.strategy_id,
            target,
            price,
            self.slow_period,
            sma,
        )
        return signals
