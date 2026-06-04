"""
apex.strategy.library.rsi2_vol_filtered
=======================================
Volatility-Filtered RSI(2) — THE IMPROVEMENT over plain RSI(2).

⚠️ SPEC STUB. Full rules: docs/STRATEGY_PLAYBOOK.md §3.

THE UPGRADE: take RSI(2) signals ONLY when ATR(14) is within ~1 standard
deviation of its 100-day mean. Skips entries during volatility spikes where
mean-reversion fails spectacularly.

Documented effect: ~20% fewer trades, profit factor +0.3.

BUILD APPROACH: implement rsi2_mean_reversion.py first, then this = that logic
plus one extra gate in on_bar:
    atr = ATR(14); atr_mean = mean(ATR over 100d); atr_std = std(ATR over 100d)
    if abs(atr[-1] - atr_mean) > atr_std:  return []   # too volatile, skip
"""
from __future__ import annotations

from typing import List

from apex.strategy.base_strategy import BaseStrategy
from apex.core.events import SignalEvent
from apex.core.models import Bar


class RSI2VolFilteredStrategy(BaseStrategy):
    """Spec stub — see module docstring and docs/STRATEGY_PLAYBOOK.md §3."""

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        # TODO(phase-3): RSI(2) logic + ATR(14)-within-1σ-of-100d-mean gate.
        raise NotImplementedError(
            "RSI2VolFilteredStrategy is a spec stub. Build rsi2_mean_reversion "
            "first, then add the ATR volatility gate. See STRATEGY_PLAYBOOK §3."
        )
