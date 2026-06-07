"""
apex.data.outlier_filter
========================
Deterministic bad-tick / outlier rejection for a single-symbol ``Bar`` series.

Vendor feeds occasionally emit a corrupt print: a fat-fingered quote, a stale
cache replayed at the wrong scale, a decimal-point slip. One bogus close can
poison every downstream calculation — a moving average, an ATR, a P&L mark — so
this filter sits at the data boundary (alongside ``normalizer``) and drops bars
whose close jumps implausibly far from the last *accepted* bar.

"Implausibly far" is measured in units of the series' own recent volatility, so
the filter adapts to the instrument: a $0.50 jump is normal for a $400 stock and
absurd for a penny stock. Two volatility yardsticks are offered:

  - ``"atr"``  — rolling Average True Range (Wilder), the same measure used in
    ``apex.strategy.indicators.atr``. Reacts to gaps and intrabar range.
  - ``"mad"``  — rolling Median Absolute Deviation of closes, a robust scale
    estimate that a single spike barely perturbs (its whole point).

A bar is rejected when ``abs(close - prev_accepted_close) > threshold * scale``,
where ``scale`` is computed over the trailing window of *accepted* bars only —
so a rejected spike never inflates the band and never masks a second spike.

Determinism & purity (golden rules):
  - No I/O, no clock, no randomness. Same input → same output, always.
  - ``Bar`` is frozen; this module NEVER mutates a bar. It returns brand-new
    lists referencing the original (accepted) bar objects.
  - Prices stay ``Decimal`` (money). The volatility *scale* is computed in
    ``float`` to mirror the indicators layer's convention, then compared against
    a ``float`` of the price gap — a deliberate, documented crossing of the
    Decimal/float boundary that touches no accounting value.
  - Within the warmup window the filter has no reliable scale yet, so bars pass
    through untouched (fail *open* on data cleaning — we never invent volatility).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from apex.core.models import Bar

Method = Literal["atr", "mad"]


@dataclass(frozen=True)
class RejectedBar:
    """A bar the filter dropped, with enough context to log or audit the decision."""
    bar: Bar
    reason: str

    @property
    def timestamp(self):  # type: ignore[no-untyped-def]
        """Convenience: the rejected bar's (UTC) timestamp."""
        return self.bar.timestamp


@dataclass(frozen=True)
class FilterResult:
    """Outcome of :func:`filter_outliers`: the kept bars and the dropped ones."""
    cleaned: list[Bar]
    rejected: list[RejectedBar]


def _median(values: Sequence[float]) -> float:
    """Median of a non-empty sequence (pure, deterministic)."""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _mad_scale(closes: Sequence[float]) -> float:
    """
    Median Absolute Deviation of a window of closes.

    Robust: a lone spike shifts the median (and thus the deviations) only
    marginally, so the band it produces still flags that spike. Returns the
    raw MAD (not scaled to a normal-consistent sigma) — the user's ``threshold``
    absorbs any constant factor.
    """
    med = _median(closes)
    deviations = [abs(c - med) for c in closes]
    return _median(deviations)


def _atr_scale(window: Sequence[Bar]) -> float:
    """
    Average True Range over a window of consecutive bars (simple mean of true
    ranges within the window). Mirrors the true-range definition in
    ``apex.strategy.indicators.atr`` but operates on the trailing accepted
    window directly so it can be recomputed after each rejection.

    Needs at least two bars (true range references the prior close); with fewer
    it returns 0.0 and the caller treats the window as warmup.
    """
    if len(window) < 2:
        return 0.0
    true_ranges: list[float] = []
    for i in range(1, len(window)):
        high = float(window[i].high)
        low = float(window[i].low)
        prev_close = float(window[i - 1].close)
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    return sum(true_ranges) / len(true_ranges)


def filter_outliers(
    bars: Sequence[Bar],
    *,
    threshold: float = 5.0,
    window: int = 14,
    warmup: int = 14,
    method: Method = "atr",
) -> FilterResult:
    """
    Reject bars whose close jumps beyond ``threshold * scale`` from the prior
    *accepted* bar, where ``scale`` is a rolling volatility estimate over the
    trailing ``window`` accepted bars.

    Parameters
    ----------
    bars:
        Chronologically ordered single-symbol bars. Assumed already sorted by
        timestamp (the data feed/normalizer's job); not re-sorted here.
    threshold:
        Multiplier on the volatility scale. A bar is rejected when the absolute
        close-to-close gap exceeds ``threshold * scale``. Larger = more lenient.
        Must be positive.
    window:
        Number of trailing *accepted* bars used to estimate the scale. Must be
        positive.
    warmup:
        Number of leading bars that always pass through unfiltered (no reliable
        scale yet). Bars also pass through whenever fewer than ``window``
        accepted bars exist or the computed scale is non-positive (a flat
        window has no meaningful band). Must be >= 0.
    method:
        ``"atr"`` (rolling Average True Range) or ``"mad"`` (rolling Median
        Absolute Deviation of closes).

    Returns
    -------
    FilterResult
        ``cleaned`` — kept bars in original order (same frozen objects).
        ``rejected`` — :class:`RejectedBar` entries (bar + human reason).

    Notes
    -----
    Empty input yields empty results. Bars are never mutated; new lists are
    built. The scale is computed over accepted bars only, so a rejected spike
    cannot widen the band and hide a neighbouring spike.
    """
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    if window <= 0:
        raise ValueError("window must be positive")
    if warmup < 0:
        raise ValueError("warmup must be non-negative")
    if method not in ("atr", "mad"):
        raise ValueError(f"unknown method {method!r} (expected 'atr' or 'mad')")

    cleaned: list[Bar] = []
    rejected: list[RejectedBar] = []

    for index, bar in enumerate(bars):
        # Warmup: pass through untouched, no reliable scale yet.
        if index < warmup:
            cleaned.append(bar)
            continue

        # Need a prior accepted bar and a full window of accepted bars.
        if len(cleaned) < window:
            cleaned.append(bar)
            continue

        recent = cleaned[-window:]
        if method == "atr":
            scale = _atr_scale(recent)
        else:  # "mad"
            scale = _mad_scale([float(b.close) for b in recent])

        # A non-positive scale (flat window) gives no meaningful band → accept.
        if scale <= 0.0:
            cleaned.append(bar)
            continue

        prev_close = float(cleaned[-1].close)
        gap = abs(float(bar.close) - prev_close)
        limit = threshold * scale

        if gap > limit:
            reason = (
                f"close jump {gap:.6g} exceeds {threshold:g} * "
                f"{method.upper()} {scale:.6g} (= {limit:.6g}) "
                f"from prior accepted close {prev_close:.6g}"
            )
            rejected.append(RejectedBar(bar=bar, reason=reason))
            continue

        cleaned.append(bar)

    return FilterResult(cleaned=cleaned, rejected=rejected)
