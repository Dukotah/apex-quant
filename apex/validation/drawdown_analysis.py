"""
apex.validation.drawdown_analysis
=================================
Drawdown *duration* analysis for an equity curve.

`metrics.max_drawdown` answers "how deep was the worst decline?". This module
answers the equally important "how LONG were you underwater?" — the question
that actually breaks investors' patience and gets strategies switched off at
the worst possible moment.

A drawdown period begins when the curve falls below its prior peak and ends when
the curve fully recovers back to (or above) that peak. The final period may end
still underwater (no recovery within the sample); we mark that explicitly rather
than pretending it recovered.

Pure, deterministic, dependency-light (stdlib only) so it runs anywhere,
including the free GitHub Actions runner. Following the convention of this layer
(see apex/validation/metrics.py), drawdown depths use float. Durations are
measured in periods (index distance), so they are ints.

All functions are pure and deterministic given their inputs. Tested in
tests/test_drawdown_analysis.py against hand-computed values.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class DrawdownPeriod:
    """
    One peak-to-recovery underwater episode in an equity curve.

    Indices refer to positions in the original equity curve.

    Attributes:
        peak_index: index of the prior high the curve fell from.
        trough_index: index of the lowest point reached during the episode.
        recovery_index: index where the curve first regained the peak, or None
            if it never recovered within the sample.
        peak_value: equity value at peak_index.
        trough_value: equity value at trough_index (the lowest point).
        depth: max decline during this episode as a positive fraction
            (0.25 = -25%). 0.0 if the peak was non-positive.
        duration: number of periods from the peak to recovery (recovery_index -
            peak_index). If unrecovered, distance from peak to the last index.
        recovery_duration: periods from the trough back to recovery
            (recovery_index - trough_index), or None if never recovered.
        recovered: True if the curve fully regained the peak within the sample.
    """
    peak_index: int
    trough_index: int
    recovery_index: int | None
    peak_value: float
    trough_value: float
    depth: float
    duration: int
    recovery_duration: int | None
    recovered: bool


def drawdown_series(equity_curve: Sequence[float]) -> list[float]:
    """
    Per-point drawdown as a positive fraction below the running peak.

    Element i is (peak - value) / peak using the highest value seen up to and
    including i. 0.0 at new highs (and whenever the running peak is non-positive,
    where the fraction is undefined — we fail closed to 0.0).
    """
    out: list[float] = []
    if not equity_curve:
        return out
    peak = equity_curve[0]
    for value in equity_curve:
        if value > peak:
            peak = value
        if peak > 0:
            out.append((peak - value) / peak)
        else:
            out.append(0.0)
    return out


def drawdown_periods(equity_curve: Sequence[float]) -> list[DrawdownPeriod]:
    """
    Decompose an equity curve into its distinct underwater episodes.

    A period starts the first time the curve dips below a peak and ends when it
    regains that peak (recovery_index points at the recovering index). Flat
    revisits of the exact peak count as recovery. The trailing episode may be
    unrecovered (recovered=False, recovery_index=None).

    Returns an empty list for an empty/single-point curve or a curve that only
    ever makes new highs (never underwater).
    """
    n = len(equity_curve)
    if n < 2:
        return []

    periods: list[DrawdownPeriod] = []
    peak = equity_curve[0]
    peak_index = 0
    in_drawdown = False
    trough_value = equity_curve[0]
    trough_index = 0

    for i in range(1, n):
        value = equity_curve[i]
        if value >= peak:
            if in_drawdown:
                # Recovered: the curve regained the prior peak at index i.
                periods.append(
                    _make_period(
                        peak_index=peak_index,
                        peak_value=peak,
                        trough_index=trough_index,
                        trough_value=trough_value,
                        recovery_index=i,
                    )
                )
                in_drawdown = False
            # New (or re-established) peak.
            peak = value
            peak_index = i
        else:
            # Below the peak → underwater.
            if not in_drawdown:
                in_drawdown = True
                trough_value = value
                trough_index = i
            elif value < trough_value:
                trough_value = value
                trough_index = i

    if in_drawdown:
        # Trailing episode never recovered within the sample.
        periods.append(
            _make_period(
                peak_index=peak_index,
                peak_value=peak,
                trough_index=trough_index,
                trough_value=trough_value,
                recovery_index=None,
                last_index=n - 1,
            )
        )

    return periods


def _make_period(
    *,
    peak_index: int,
    peak_value: float,
    trough_index: int,
    trough_value: float,
    recovery_index: int | None,
    last_index: int | None = None,
) -> DrawdownPeriod:
    """Build a DrawdownPeriod, computing depth/duration consistently."""
    if peak_value > 0:
        depth = (peak_value - trough_value) / peak_value
    else:
        depth = 0.0

    if recovery_index is not None:
        duration = recovery_index - peak_index
        recovery_duration: int | None = recovery_index - trough_index
        recovered = True
    else:
        end = last_index if last_index is not None else trough_index
        duration = end - peak_index
        recovery_duration = None
        recovered = False

    return DrawdownPeriod(
        peak_index=peak_index,
        trough_index=trough_index,
        recovery_index=recovery_index,
        peak_value=peak_value,
        trough_value=trough_value,
        depth=depth,
        duration=duration,
        recovery_duration=recovery_duration,
        recovered=recovered,
    )


def max_drawdown_duration(equity_curve: Sequence[float]) -> int:
    """
    Longest underwater stretch, in periods (peak → recovery, or peak → end if
    still underwater at the end of the sample).

    This is the "time to recover" pain metric: how many periods the worst episode
    kept you below your prior high-water mark. Returns 0 for a curve that is never
    underwater (or has fewer than 2 points).
    """
    periods = drawdown_periods(equity_curve)
    if not periods:
        return 0
    return max(p.duration for p in periods)


def max_recovery_time(equity_curve: Sequence[float]) -> int | None:
    """
    Longest trough-to-recovery time among *recovered* episodes, in periods.

    Returns None if no episode ever recovered within the sample (or there are no
    drawdowns at all) — we don't invent a recovery that didn't happen.
    """
    periods = drawdown_periods(equity_curve)
    recovered = [p.recovery_duration for p in periods if p.recovery_duration is not None]
    if not recovered:
        return None
    return max(recovered)


def average_drawdown(equity_curve: Sequence[float]) -> float:
    """
    Mean depth across distinct underwater episodes, as a positive fraction.

    Averages the per-episode max depths (not the point-by-point series), so a
    single long shallow dip and a single deep spike each count once. Returns 0.0
    when there are no drawdowns.
    """
    periods = drawdown_periods(equity_curve)
    if not periods:
        return 0.0
    return sum(p.depth for p in periods) / len(periods)


def average_drawdown_duration(equity_curve: Sequence[float]) -> float:
    """
    Mean duration (peak → recovery/end) across underwater episodes, in periods.

    Returns 0.0 when there are no drawdowns.
    """
    periods = drawdown_periods(equity_curve)
    if not periods:
        return 0.0
    return sum(p.duration for p in periods) / len(periods)


def average_recovery_time(equity_curve: Sequence[float]) -> float | None:
    """
    Mean trough-to-recovery time across *recovered* episodes, in periods.

    Returns None if nothing recovered within the sample.
    """
    periods = drawdown_periods(equity_curve)
    recovered = [p.recovery_duration for p in periods if p.recovery_duration is not None]
    if not recovered:
        return None
    return sum(recovered) / len(recovered)


@dataclass(frozen=True)
class DrawdownAnalysis:
    """Roll-up of the drawdown-duration story for an equity curve."""
    num_drawdowns: int
    max_drawdown_depth: float           # deepest episode (positive fraction)
    average_drawdown_depth: float       # mean episode depth
    max_drawdown_duration: int          # longest underwater stretch (periods)
    average_drawdown_duration: float    # mean underwater stretch (periods)
    max_recovery_time: int | None       # longest trough→recovery (periods)
    average_recovery_time: float | None # mean trough→recovery (periods)
    currently_underwater: bool          # ends below its prior high-water mark
    periods: tuple[DrawdownPeriod, ...]

    def summary(self) -> str:
        rec = "underwater" if self.currently_underwater else "at highs"
        return (
            f"Drawdowns: {self.num_drawdowns}, "
            f"max depth {self.max_drawdown_depth:.1%}, "
            f"max duration {self.max_drawdown_duration} periods, "
            f"now {rec}"
        )


def analyze_drawdowns(equity_curve: Sequence[float]) -> DrawdownAnalysis:
    """
    Full drawdown-duration analysis in one pass-friendly call.

    Bundles depth, duration, and recovery statistics plus the individual
    episodes. Safe on empty/flat/never-underwater curves (everything zeroes out,
    recovery times are None).
    """
    periods = drawdown_periods(equity_curve)
    recovered = [p.recovery_duration for p in periods if p.recovery_duration is not None]

    if periods:
        max_depth = max(p.depth for p in periods)
        avg_depth = sum(p.depth for p in periods) / len(periods)
        max_dur = max(p.duration for p in periods)
        avg_dur = sum(p.duration for p in periods) / len(periods)
    else:
        max_depth = 0.0
        avg_depth = 0.0
        max_dur = 0
        avg_dur = 0.0

    max_rec: int | None = max(recovered) if recovered else None
    avg_rec: float | None = (sum(recovered) / len(recovered)) if recovered else None

    currently_underwater = bool(periods) and not periods[-1].recovered

    return DrawdownAnalysis(
        num_drawdowns=len(periods),
        max_drawdown_depth=max_depth,
        average_drawdown_depth=avg_depth,
        max_drawdown_duration=max_dur,
        average_drawdown_duration=avg_dur,
        max_recovery_time=max_rec,
        average_recovery_time=avg_rec,
        currently_underwater=currently_underwater,
        periods=tuple(periods),
    )
