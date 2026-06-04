"""
apex.strategy.library.rsi2_mean_reversion
=========================================
RSI(2) Mean Reversion (Larry Connors) — THE TACTICAL COMPLEMENT.

⚠️ SPEC STUB. Implement in a dedicated session (see SESSION_PLAYBOOK.md).
Full rules and rationale: docs/STRATEGY_PLAYBOOK.md §2.

WHY: Complements Dual Momentum. Momentum profits from trends; this profits from
short-term dislocations *within* trends. Run them together with capped capital.

THE RULES (long-only):
  Trend filter:  price > 200-day SMA  (only buy dips in confirmed uptrends).
  Entry:         2-period RSI < 10  (Connors: < 5 is even stronger) → BUY @ close.
  Exit:          close > 5-day SMA, OR a time stop after N days (default 5).
  Universe:      liquid ETFs / large-caps (SPY, QQQ work well).

IMPLEMENTATION NOTES:
  - Read SMA(200), SMA(5), RSI(2) from apex.strategy.indicators (don't recompute).
  - Warmup: need 200+ bars before the trend filter is valid; return [] until then.
  - Emit strength scaled by how deep RSI is (e.g. RSI<5 → 1.0, RSI<10 → 0.6).
  - Suggest a stop-loss (e.g. recent swing low or fixed %) — RiskManager validates.
  - Capital: this is tactical. In live config, cap its allocation to 15-25%.
  - Determinism: pure function of bar history.

PERFORMANCE PRIOR: ~9%/yr on SPY, invested only ~28% of the time, but ~34% max DD
in volatile periods. The vol-filtered variant (rsi2_vol_filtered.py) tames that.
"""
from __future__ import annotations

from typing import List

from apex.strategy.base_strategy import BaseStrategy
from apex.core.events import SignalEvent
from apex.core.models import Bar


class RSI2MeanReversionStrategy(BaseStrategy):
    """Spec stub — see module docstring and docs/STRATEGY_PLAYBOOK.md §2."""

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        # TODO(phase-3): trend filter (>200 SMA) + RSI(2)<10 entry + 5-SMA exit.
        raise NotImplementedError(
            "RSI2MeanReversionStrategy is a spec stub. Implement per the module "
            "docstring and docs/STRATEGY_PLAYBOOK.md before use."
        )
