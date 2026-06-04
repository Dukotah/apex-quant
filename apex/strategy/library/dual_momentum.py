"""
apex.strategy.library.dual_momentum
===================================
Dual Momentum (Gary Antonacci) — THE ANCHOR STRATEGY.

⚠️ SPEC STUB. Implement in a dedicated session (see SESSION_PLAYBOOK.md).
Full rules and rationale: docs/STRATEGY_PLAYBOOK.md §1.

WHY THIS ONE FIRST:
  - Monthly rebalance → near-zero transaction costs (overfitting/cost resistant)
  - Uses only free daily data
  - Few parameters = hard to overfit
  - Built-in absolute-momentum switch is its own drawdown defense

THE RULES (classic Global Equities Momentum):
  Universe: SPY (US), VEU/EFA (international ex-US), AGG (bonds), BIL (cash).
  Monthly, at the close of the last trading day of the month:
    1. lookback = trailing 12-month total return.
    2. abs_mom = SPY 12-mo return.
    3. If abs_mom > 0:
         target = the ETF (SPY vs international) with the higher 12-mo return.
       else:
         target = AGG (or BIL).
    4. If target != current holding: emit SELL (current) + BUY (target) signals.
       Else: emit nothing (hold).
  Hold exactly one asset at a time.

IMPLEMENTATION NOTES:
  - on_bar only acts on month-end bars; otherwise returns []. Use the bar
    timestamp + the StrategyContext's bar history to detect month boundaries.
  - Compute 12-mo return from ~252 daily bars (or use monthly bars if the feed
    provides them). Need warmup of 252+ bars before first signal.
  - Position sizing is the RiskManager's job; emit strength=1.0 (full conviction,
    single-asset). Suggest a wide protective stop (e.g. 15% below) — the absolute
    momentum switch is the real exit, the stop is a catastrophe backstop.
  - Determinism: no wall-clock time; month detection from bar timestamps only.

PERFORMANCE PRIOR (be skeptical):
  - Antonacci 39-yr: 17.43%/yr, 22.7% max DD. Independent ETF replication:
    ~6.75%/yr, ~30% max DD. Plan for the lower end.
"""
from __future__ import annotations

from typing import List

from apex.strategy.base_strategy import BaseStrategy
from apex.core.events import SignalEvent
from apex.core.models import Bar


class DualMomentumStrategy(BaseStrategy):
    """Spec stub — see module docstring and docs/STRATEGY_PLAYBOOK.md §1."""

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        # TODO(phase-3): implement month-end detection + 12-mo return ranking +
        # absolute-momentum switch. Return [] until implemented.
        raise NotImplementedError(
            "DualMomentumStrategy is a spec stub. Implement per the module "
            "docstring and docs/STRATEGY_PLAYBOOK.md before use."
        )
