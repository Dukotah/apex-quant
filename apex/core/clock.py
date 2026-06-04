"""
apex.core.clock
==============
Time abstraction. THE reason backtests are deterministic and match live behavior.

NEVER call datetime.now() anywhere in strategy, risk, or engine logic. Always ask
the injected Clock for the time. In live mode it returns wall-clock UTC; in
backtest mode it returns the timestamp of the bar currently being processed.

This single discipline is what guarantees:
  - Backtests are reproducible (same data → same result, every time).
  - A strategy behaves identically in backtest and live (no "works in backtest,
    breaks live" surprises from time-dependent logic).
  - No look-ahead bias from accidentally reading the real clock during a replay.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone


class Clock(ABC):
    """Abstract time source. Inject everywhere time is needed."""

    @abstractmethod
    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        ...


class RealClock(Clock):
    """Wall-clock UTC. Used in live and paper modes."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class SimulatedClock(Clock):
    """
    Backtest clock. Returns the timestamp of the bar currently being processed.

    The engine calls set_time() as each bar is replayed; everything else reads
    now(). Enforces MONOTONICITY: time can never go backward. If a feed delivers
    an out-of-order bar, set_time raises rather than silently corrupting the
    timeline (which would invalidate the whole backtest).
    """

    def __init__(self, start: datetime | None = None) -> None:
        if start is not None and start.tzinfo is None:
            raise ValueError("SimulatedClock start must be timezone-aware (UTC)")
        self._current: datetime | None = start

    def now(self) -> datetime:
        if self._current is None:
            raise RuntimeError(
                "SimulatedClock.now() called before any time was set. "
                "The engine must call set_time() with the first bar."
            )
        return self._current

    def set_time(self, t: datetime) -> None:
        """Advance the simulated clock. Must be timezone-aware and non-decreasing."""
        if t.tzinfo is None:
            raise ValueError("SimulatedClock time must be timezone-aware (UTC)")
        if self._current is not None and t < self._current:
            raise ValueError(
                f"Time cannot move backward: tried to set {t} "
                f"but clock is at {self._current}. Out-of-order bar?"
            )
        self._current = t

    @property
    def is_set(self) -> bool:
        return self._current is not None
