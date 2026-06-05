"""
apex.validation.drift_monitor
=============================
The part of the Gauntlet that NEVER stops: the live-vs-backtest drift monitor —
the alpha-decay kill switch.

Passing the Gauntlet is not permanent. Edges fade (the trade gets crowded, the
market regime shifts, or it was never real). This monitor continuously compares
a strategy's ROLLING LIVE performance to the performance the Gauntlet validated.
When the live rolling Sharpe falls below a floor (default 70% of the validated
Sharpe), the strategy is AUTO-QUARANTINED: stop allocating capital, keep logging,
alert. The RiskManager caps the bleed in the meantime.

This mirrors docs/VALIDATION_GAUNTLET.md ("Post-Deployment: The Gauntlet Never
Stops") and uses the same 0.70 floor as gauntlet.grade_and_assemble's
quarantine_sharpe_floor.

Pure stdlib (deque + the metrics module). Deterministic: same return stream →
same decisions. Quarantine is STICKY — clearing it requires a human reviewing
why the edge decayed and calling reset().
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque, Optional

from apex.validation import metrics

DEFAULT_FLOOR_RATIO = 0.70           # matches the Gauntlet's quarantine floor
DEFAULT_WINDOW = 30                  # "30-day" rolling Sharpe


class DriftState(str, Enum):
    WARMING_UP = "warming_up"        # not enough live data to judge yet
    ACTIVE = "active"                # tracking; within tolerance of the backtest
    QUARANTINED = "quarantined"      # decayed below the floor — stop allocating


@dataclass(frozen=True)
class DriftReading:
    """A point-in-time assessment of live-vs-backtest drift."""
    state: DriftState
    rolling_sharpe: float
    validated_sharpe: float
    floor: float
    drift_ratio: float               # rolling / validated (1.0 = on track)
    observations: int
    reason: str

    @property
    def is_quarantined(self) -> bool:
        return self.state == DriftState.QUARANTINED

    def summary(self) -> str:
        return (
            f"Drift [{self.state.value}]: rolling Sharpe {self.rolling_sharpe:.2f} "
            f"vs validated {self.validated_sharpe:.2f} "
            f"(ratio {self.drift_ratio:.0%}, floor {self.floor:.2f}, "
            f"n={self.observations}) — {self.reason}"
        )


class DriftMonitor:
    """
    Tracks a live strategy's rolling Sharpe and auto-quarantines on decay.

    Args:
        strategy_id:      the strategy being monitored.
        validated_sharpe: the Sharpe the Gauntlet validated (must be > 0; a
                          strategy reaching live has a positive validated edge).
        window:           rolling window of daily returns (default 30).
        floor_ratio:      quarantine when rolling < floor_ratio * validated_sharpe.
        min_observations: returns needed before the monitor will judge (default =
                          window; below this it stays WARMING_UP, never quarantines
                          on too-little data).
        periods_per_year: annualization factor for the rolling Sharpe.

    Usage:
        mon = DriftMonitor("dual_momentum", validated_sharpe=1.4)
        for r in live_daily_returns:
            reading = mon.record_return(r)
            if reading.is_quarantined:
                halt_allocation(); alert(reading.summary())
    """

    def __init__(
        self,
        strategy_id: str,
        validated_sharpe: float,
        window: int = DEFAULT_WINDOW,
        floor_ratio: float = DEFAULT_FLOOR_RATIO,
        min_observations: Optional[int] = None,
        periods_per_year: int = metrics.TRADING_DAYS_PER_YEAR,
    ) -> None:
        if validated_sharpe <= 0:
            raise ValueError("validated_sharpe must be > 0 (a live strategy has a positive edge)")
        if window < 2:
            raise ValueError("window must be >= 2")

        self.strategy_id = strategy_id
        self.validated_sharpe = validated_sharpe
        self.window = window
        self.floor = floor_ratio * validated_sharpe
        self.min_observations = min_observations if min_observations is not None else window
        self.periods_per_year = periods_per_year

        self._returns: Deque[float] = deque(maxlen=window)
        self._last_equity: Optional[float] = None
        self._quarantined: bool = False
        self._quarantine_reason: str = ""

    # ------------------------------------------------------------------ inputs

    def record_return(self, daily_return: float) -> DriftReading:
        """Record one period's return (0.01 = +1%) and re-assess."""
        self._returns.append(daily_return)
        return self.check()

    def record_equity(self, equity: float) -> DriftReading:
        """
        Record an equity point; the period return is derived from the previous
        equity. The first call only seeds the baseline (returns a WARMING_UP read).
        """
        if self._last_equity is not None and self._last_equity != 0:
            self._returns.append(equity / self._last_equity - 1.0)
        self._last_equity = equity
        return self.check()

    # ------------------------------------------------------------------ assess

    def check(self) -> DriftReading:
        """Assess current drift state without recording new data."""
        n = len(self._returns)
        rolling = metrics.sharpe_ratio(
            list(self._returns), periods_per_year=self.periods_per_year
        )
        ratio = (rolling / self.validated_sharpe) if self.validated_sharpe else 0.0

        # Quarantine is sticky once tripped — only reset() clears it.
        if self._quarantined:
            state, reason = DriftState.QUARANTINED, self._quarantine_reason
        elif n < self.min_observations:
            state = DriftState.WARMING_UP
            reason = f"collecting live data ({n}/{self.min_observations})"
        elif rolling < self.floor:
            self._quarantined = True
            self._quarantine_reason = (
                f"rolling Sharpe {rolling:.2f} < floor {self.floor:.2f} "
                f"({ratio:.0%} of validated) — alpha decay"
            )
            state, reason = DriftState.QUARANTINED, self._quarantine_reason
        else:
            state = DriftState.ACTIVE
            reason = f"on track ({ratio:.0%} of validated Sharpe)"

        return DriftReading(
            state=state,
            rolling_sharpe=rolling,
            validated_sharpe=self.validated_sharpe,
            floor=self.floor,
            drift_ratio=ratio,
            observations=n,
            reason=reason,
        )

    # ------------------------------------------------------------------ control

    @property
    def is_quarantined(self) -> bool:
        return self._quarantined

    def reset(self, clear_history: bool = False) -> None:
        """
        Manually lift the quarantine after a human has investigated the decay.
        Optionally clear the return history to start the rolling window fresh.
        """
        self._quarantined = False
        self._quarantine_reason = ""
        if clear_history:
            self._returns.clear()
            self._last_equity = None

    @classmethod
    def from_gauntlet_report(
        cls,
        report,
        floor_ratio: float = DEFAULT_FLOOR_RATIO,
        **kwargs,
    ) -> "DriftMonitor":
        """
        Build a monitor straight from a GauntletReport. The report stores
        quarantine_sharpe_floor = floor_ratio * validated_sharpe, so the validated
        Sharpe is recovered as floor / floor_ratio.
        """
        validated = report.quarantine_sharpe_floor / floor_ratio if floor_ratio else 0.0
        return cls(report.strategy_name, validated, floor_ratio=floor_ratio, **kwargs)
