"""
apex.strategy.regime
====================
The VolatilityRegimeClassifier — a reusable *gate* component, NOT a strategy.

A regime classifier answers one question: "is the market calm or turbulent
right now?" Strategies (and risk-aware filters) consult it to decide whether to
trade, scale exposure, or stand aside. The classic use is the vol-filtered
RSI(2): only take mean-reversion entries when volatility is not elevated.

How it works:
  - Compute realized volatility (stdev of log returns) over a short `vol_window`.
  - Compute that same realized-vol value at every step across a longer
    `lookback` window, building a distribution of "what's normal for this asset."
  - Rank the current vol against that distribution → a percentile in [0, 1].
  - Map the percentile to an enum band: LOW / NORMAL / HIGH (or UNKNOWN when
    there isn't enough history to judge).

CONTRACT (mirrors apex.strategy.indicators):
  - Input: a sequence of closes (floats or Decimals — money math is Decimal
    elsewhere, but volatility is comparative/statistical, so we work in float
    here, matching apex.validation.metrics).
  - Insufficient data → RegimeResult with regime=UNKNOWN and percentile=None.
    NEVER returns a garbage classification.
  - Pure & deterministic: same closes → same result, always. No I/O, no clock,
    no randomness.

Tested in tests/test_regime.py against hand-computed values and edge cases.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence


class VolatilityRegime(str, Enum):
    """The volatility band the market is currently in."""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    UNKNOWN = "unknown"   # not enough history to classify


def _to_floats(data: Sequence) -> list[float]:
    """Accept Decimals or floats; volatility math runs in float (see module doc)."""
    return [float(x) for x in data]


def _log_returns(values: Sequence[float]) -> list[float]:
    """
    Period-over-period log returns. Returns a list one shorter than the input.
    Non-positive prices are impossible for real bars (Bar.__post_init__ guards
    them), but we skip pairs that would error to stay pure rather than raise.
    """
    out: list[float] = []
    for prev, curr in zip(values, values[1:]):
        if prev > 0 and curr > 0:
            out.append(math.log(curr / prev))
        else:
            out.append(0.0)
    return out


def realized_volatility(closes: Sequence, window: int) -> Optional[float]:
    """
    Realized volatility over the most recent `window` returns: the population
    standard deviation of the trailing log returns.

    Needs `window` returns, i.e. `window + 1` closes. Returns None if there
    isn't enough data — never a partial/garbage number.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    values = _to_floats(closes)
    rets = _log_returns(values)
    if len(rets) < window:
        return None
    recent = rets[-window:]
    mean = sum(recent) / window
    variance = sum((r - mean) ** 2 for r in recent) / window
    return math.sqrt(variance)


def _percentile_rank(value: float, distribution: Sequence[float]) -> float:
    """
    The fraction of `distribution` values that are <= `value`, in [0.0, 1.0].
    Uses the "<= " convention so the max of the distribution ranks at 1.0.
    `distribution` must be non-empty.
    """
    n = len(distribution)
    count = sum(1 for d in distribution if d <= value)
    return count / n


@dataclass(frozen=True)
class RegimeResult:
    """
    The verdict for one classification call. Immutable, like all data that
    crosses module boundaries.

    `regime`     — the enum band (UNKNOWN when undeterminable).
    `percentile` — current vol's rank within the lookback distribution, in
                   [0, 1], or None when UNKNOWN.
    `volatility` — the current realized-vol value (float), or None when UNKNOWN.
    """
    regime: VolatilityRegime
    percentile: Optional[float]
    volatility: Optional[float]

    def is_risk_on(self) -> bool:
        """
        Convenience for gating: True when the regime is benign enough to trade
        risk-seeking strategies (LOW or NORMAL). HIGH and UNKNOWN are risk-off —
        consistent with the project's fail-closed philosophy: when we can't tell,
        we treat it as risk-off.
        """
        return self.regime in (VolatilityRegime.LOW, VolatilityRegime.NORMAL)


class VolatilityRegimeClassifier:
    """
    Classifies the current volatility regime by percentile within a trailing
    lookback window. Stateless and reusable: construct once with thresholds,
    then call `classify(closes)` with any window of closes.

    Parameters
    ----------
    vol_window : int
        Number of log returns used to measure realized volatility at each step.
    lookback : int
        Number of realized-vol samples that form the comparison distribution.
        Larger = a more stable sense of "normal," slower to adapt.
    low_pct : float
        Percentile (0..1) below which volatility is considered LOW.
    high_pct : float
        Percentile (0..1) at or above which volatility is considered HIGH.
        Between low_pct and high_pct is NORMAL.

    To classify, the classifier needs `lookback` realized-vol samples, the first
    of which needs `vol_window + 1` closes, so the minimum is
    `vol_window + lookback` closes. With fewer, it returns UNKNOWN.
    """

    def __init__(
        self,
        vol_window: int = 20,
        lookback: int = 252,
        low_pct: float = 0.25,
        high_pct: float = 0.75,
    ) -> None:
        if vol_window <= 0:
            raise ValueError("vol_window must be positive")
        if lookback <= 0:
            raise ValueError("lookback must be positive")
        if not (0.0 <= low_pct <= high_pct <= 1.0):
            raise ValueError("require 0 <= low_pct <= high_pct <= 1")
        self.vol_window = vol_window
        self.lookback = lookback
        self.low_pct = low_pct
        self.high_pct = high_pct

    @property
    def min_closes(self) -> int:
        """Minimum number of closes required to produce a non-UNKNOWN result."""
        return self.vol_window + self.lookback

    def _vol_series(self, values: Sequence[float]) -> list[float]:
        """
        Realized vol computed at every step where a full vol_window of returns
        is available. Length = len(returns) - vol_window + 1 (>= 0).
        """
        rets = _log_returns(values)
        out: list[float] = []
        w = self.vol_window
        for end in range(w, len(rets) + 1):
            window = rets[end - w:end]
            mean = sum(window) / w
            variance = sum((r - mean) ** 2 for r in window) / w
            out.append(math.sqrt(variance))
        return out

    def classify(self, closes: Sequence) -> RegimeResult:
        """
        Classify the regime as of the most recent close.

        Returns a RegimeResult. If there isn't enough history
        (`< min_closes` closes), returns UNKNOWN with None percentile/vol.
        """
        values = _to_floats(closes)
        if len(values) < self.min_closes:
            return RegimeResult(VolatilityRegime.UNKNOWN, None, None)

        vol_series = self._vol_series(values)
        # The comparison distribution is the trailing `lookback` vol samples.
        distribution = vol_series[-self.lookback:]
        current = distribution[-1]

        percentile = _percentile_rank(current, distribution)

        if percentile < self.low_pct:
            regime = VolatilityRegime.LOW
        elif percentile >= self.high_pct:
            regime = VolatilityRegime.HIGH
        else:
            regime = VolatilityRegime.NORMAL

        return RegimeResult(regime, percentile, current)
