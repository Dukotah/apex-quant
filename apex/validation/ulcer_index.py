"""
apex.validation.ulcer_index
===========================
The Ulcer Index (UI) and Ulcer Performance Index (UPI, a.k.a. the Martin ratio)
from an equity curve.

Where max drawdown reports only the single worst peak-to-trough decline, the
Ulcer Index captures BOTH the depth AND the duration of every drawdown. It is
the root-mean-square of the percentage drawdowns measured from the running peak,
so a curve that spends a long time underwater scores worse than one that dips
once and recovers — even if both share the same single worst trough. That makes
it a better proxy for the actual *pain* of holding a strategy.

  drawdown_t = (peak_t - value_t) / peak_t        (a non-negative fraction)
  UI         = sqrt( mean( drawdown_t^2 ) )        (a non-negative fraction)
  UPI        = (annualized_return - risk_free) / UI

This is statistical/metric code, so it follows the same convention as
apex.validation.metrics: pure float math on stdlib (math + the metrics module),
no I/O, deterministic. Tested in tests/test_ulcer_index.py against hand-computed
values.
"""
from __future__ import annotations

import math
import statistics
from typing import Sequence

from apex.validation import metrics

TRADING_DAYS_PER_YEAR = 252


def drawdown_series(equity_curve: Sequence[float]) -> list[float]:
    """
    Per-point drawdown from the running peak, as non-negative fractions
    (0.10 = 10% below the high-water mark at that point).

    Returns an empty list for an empty curve. A non-positive peak (<= 0)
    yields a 0.0 drawdown at that point — we cannot express a meaningful
    percentage off a zero/negative peak, so we fail closed to "no drawdown".
    """
    out: list[float] = []
    if not equity_curve:
        return out
    peak = equity_curve[0]
    for value in equity_curve:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak
            out.append(dd if dd > 0.0 else 0.0)
        else:
            out.append(0.0)
    return out


def ulcer_index(equity_curve: Sequence[float]) -> float:
    """
    The Ulcer Index: root-mean-square of the percentage drawdowns from the
    running peak, expressed as a non-negative fraction (0.05 = 5%).

    Lower is better; 0.0 means the curve never dipped below its high-water mark.
    Returns 0.0 for an empty or single-point curve (no meaningful drawdown).
    """
    dds = drawdown_series(equity_curve)
    if not dds:
        return 0.0
    mean_sq = statistics.fmean([d * d for d in dds])
    return math.sqrt(mean_sq)


def ulcer_performance_index(
    equity_curve: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    The Ulcer Performance Index (UPI / Martin ratio): excess annualized return
    per unit of Ulcer Index. The drawdown-pain analogue of the Sharpe ratio.

      UPI = (annualized_return - risk_free_rate) / ulcer_index

    Higher is better. Returns 0.0 when the Ulcer Index is 0 (no drawdowns →
    the ratio is undefined; we fail closed rather than return infinity) or when
    the curve is too short to imply a return.
    """
    if len(equity_curve) < 2:
        return 0.0
    ui = ulcer_index(equity_curve)
    if ui == 0.0:
        return 0.0
    excess = metrics.annualized_return(equity_curve, periods_per_year) - risk_free_rate
    return excess / ui
