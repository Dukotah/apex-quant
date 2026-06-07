"""
apex.validation.cagr_mar
========================
CAGR (Compound Annual Growth Rate) and the MAR ratio (CAGR divided by maximum
drawdown) computed straight from an equity curve.

CAGR answers "what smooth annual rate would have grown the starting equity into
the ending equity over this span?" — it normalizes total growth by time so two
backtests of different lengths are comparable.

The MAR ratio (named for Managed Account Reports) divides CAGR by the worst
peak-to-trough drawdown. It is the classic "return per unit of worst-case pain"
yardstick: a MAR around/above 0.5 is respectable for a long-run strategy, and
above 1.0 is excellent (and rare). It is closely related to the Calmar ratio,
the difference being convention: Calmar canonically uses a trailing window
(often 36 months) while MAR uses the entire track record. This module computes
the full-history flavor.

Deliberately dependency-light (stdlib only) so it runs anywhere, including the
free GitHub Actions runner. Mirrors the float convention of
apex/validation/metrics.py: this is statistical/metric code, not money handling.

All functions are pure and deterministic given their inputs. Tested in
tests/test_cagr_mar.py against hand-computed values.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

TRADING_DAYS_PER_YEAR = 252


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """
    Worst peak-to-trough decline, as a positive fraction (0.25 = -25% drawdown).

    Kept local (rather than importing) so this module stays self-contained, but
    the math is identical to apex.validation.metrics.max_drawdown.
    """
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for value in equity_curve:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak
            if dd > worst:
                worst = dd
    return worst


def cagr(
    equity_curve: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    Compound Annual Growth Rate implied by an equity curve, as a fraction
    (0.15 = +15%/yr).

    Each step between consecutive points is treated as one period; the curve is
    assumed to span ``len(curve) - 1`` periods, annualized via ``periods_per_year``.

    CAGR = (end / start) ** (periods_per_year / periods) - 1

    Edge cases (fail closed — never garbage):
        * fewer than 2 points, or a non-positive starting value -> 0.0
          (no measurable growth span).
        * a non-positive ending value (total wipeout) -> -1.0
          (you lost everything; the rate is -100%).
    """
    if len(equity_curve) < 2 or equity_curve[0] <= 0:
        return 0.0
    periods = len(equity_curve) - 1
    end = equity_curve[-1]
    if end <= 0:
        return -1.0
    growth = end / equity_curve[0]
    return growth ** (periods_per_year / periods) - 1.0


def mar_ratio(
    equity_curve: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    MAR ratio: CAGR divided by maximum drawdown. Reward per unit of worst-case pain.

    Returns 0.0 when the max drawdown is zero (an unbroken up-only curve, or too
    few points to have a drawdown) — the ratio is undefined / infinite there, and
    we fail closed to a neutral 0.0 rather than emit ``inf``. A drawdown of zero
    on a real track record almost always means too little data to trust anyway.

    Note the MAR ratio can be negative: a losing strategy has a negative CAGR
    over a positive drawdown.
    """
    mdd = max_drawdown(equity_curve)
    if mdd == 0:
        return 0.0
    return cagr(equity_curve, periods_per_year) / mdd


@dataclass(frozen=True)
class CagrMarResult:
    """Bundled CAGR + MAR summary for an equity curve."""
    cagr: float
    max_drawdown: float
    mar_ratio: float

    def summary(self) -> str:
        return (
            f"CAGR {self.cagr:.1%}, max DD {self.max_drawdown:.1%}, "
            f"MAR {self.mar_ratio:.2f}"
        )


def cagr_mar(
    equity_curve: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> CagrMarResult:
    """
    Compute CAGR, max drawdown, and the MAR ratio in one pass-friendly call.

    Returns a frozen CagrMarResult. Handles insufficient data gracefully: an
    empty or single-point curve yields all-zero metrics.
    """
    g = cagr(equity_curve, periods_per_year)
    mdd = max_drawdown(equity_curve)
    mar = 0.0 if mdd == 0 else g / mdd
    return CagrMarResult(cagr=g, max_drawdown=mdd, mar_ratio=mar)
