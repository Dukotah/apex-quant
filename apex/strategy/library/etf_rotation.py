"""
apex.strategy.library.etf_rotation
==================================
Weekly ETF Momentum Rotation — THE DIVERSIFIER.

⚠️ SPEC STUB. Full rules: docs/STRATEGY_PLAYBOOK.md §4.

THESIS: Rank a basket of sector/asset ETFs by recent return; own the top N,
volatility-scaled; rebalance weekly. Low turnover, diversified, with an
absolute-momentum risk-off overlay.

THE RULES:
  Universe: liquid sector ETFs (XLK, XLF, XLE, XLV, XLY, XLI, XLP, ...) + a bond
            ETF (AGG/IEF) as the risk-off sleeve.
  Weekly, at Friday close:
    1. Rank universe by trailing 3-month (or 6-month) total return.
    2. Select top 1-3 ETFs that ALSO have positive absolute momentum.
    3. Size each inversely to its recent volatility (lower vol = larger weight).
    4. If nothing has positive absolute momentum → rotate fully to bonds.
    5. Emit SELL for dropped holdings, BUY for new ones, hold the rest.

IMPLEMENTATION NOTES:
  - Week-end detection from bar timestamps (like dual_momentum's month detection).
  - Trailing-return ranking + per-ETF realized vol from indicators/bar history.
  - Inverse-vol weighting must respect RiskManager's per-position + exposure caps;
    emit strength proportional to target weight and let the RiskManager finalize.
  - Warmup: enough bars for the lookback (≈63 for 3-mo) + vol estimate.
  - Determinism: pure function of bar history.

WHY IT FITS: weekly turnover, sector diversification, built-in risk-off overlay.
"""
from __future__ import annotations

from typing import List

from apex.strategy.base_strategy import BaseStrategy
from apex.core.events import SignalEvent
from apex.core.models import Bar


class ETFRotationStrategy(BaseStrategy):
    """Spec stub — see module docstring and docs/STRATEGY_PLAYBOOK.md §4."""

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        # TODO(phase-3): week-end detection + momentum rank + abs-mom filter +
        # inverse-vol sizing + risk-off rotation.
        raise NotImplementedError(
            "ETFRotationStrategy is a spec stub. Implement per the module "
            "docstring and docs/STRATEGY_PLAYBOOK.md §4 before use."
        )
