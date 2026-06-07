"""
apex.data.dollar_volume_bars
============================
Aggregate finer ``Bar``s into **volume bars** or **dollar-volume bars**.

Standard time bars (1Min, 1Day, …) sample the market at a fixed clock cadence,
which over-samples quiet periods and under-samples bursts of activity. *Volume*
and *dollar-volume* bars instead sample at a fixed quantum of traded activity:
a new aggregated bar is emitted every time the cumulative volume (shares) or
cumulative dollar volume (price × shares) since the last emission crosses a
fixed threshold. The result is a bar series with far more statistically uniform
information content per bar — the classic motivation from López de Prado's
*Advances in Financial Machine Learning*.

This module is a **pure, deterministic, offline transform**: it takes a sequence
of already-validated ``Bar``s and returns a new sequence of aggregated ``Bar``s.
No I/O, no clock, no randomness. The same input always yields the same output.

Aggregation rules (deterministic):
  - Input bars are processed in the order given (the caller is responsible for
    chronological ordering — ``HistoricalDataFeed`` already sorts). Bars are
    accumulated until the running total meets-or-exceeds the threshold, at which
    point one aggregated bar is emitted and the accumulator resets.
  - The emitted bar's OHLC are: ``open`` of the first contributing bar, ``high``
    = max of contributing highs, ``low`` = min of contributing lows, ``close``
    of the last contributing bar. ``volume`` = sum of contributing volumes.
  - The emitted bar's ``timestamp`` is the timestamp of the **last** contributing
    bar (its close time), matching the ``Bar`` close-time convention.
  - A single source bar that alone exceeds the threshold emits as its own
    aggregated bar (we never split a source bar — a ``Bar`` is an atomic fact).
  - **Dollar volume per source bar** is measured as ``close * volume`` — a single
    representative price for the bar times its share volume. This is the standard
    proxy when only OHLCV (not tick-level) data is available, and it is exact and
    reproducible.

Insufficient / leftover data (handled gracefully, never garbage):
  - Empty input → empty output.
  - A trailing partial accumulation that never reaches the threshold is, by
    default, **dropped** (``emit_partial=False``) — an incomplete bar is not a
    fact about a completed quantum of activity. Set ``emit_partial=True`` to flush
    it as a final, smaller bar (useful for "include the tail" backtests).
  - Source bars with zero volume contribute nothing to the running total but are
    still folded into the current accumulator's OHLC (they are real price action).

All money/price/volume math is ``Decimal`` — this is the data layer and follows
the Decimal-for-money golden rule (mirrors ``normalizer`` / ``historical_feed``).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Iterable, List, Optional, Sequence

from apex.core.models import Bar, Symbol


class BarMetric(str, Enum):
    """Which traded-activity quantum the threshold is measured in."""

    VOLUME = "volume"  # cumulative share/contract volume
    DOLLAR_VOLUME = "dollar"  # cumulative close*volume (notional)


def _to_threshold(value: object, *, field: str = "threshold") -> Decimal:
    """Coerce a threshold to a strictly-positive ``Decimal`` (str() first)."""
    try:
        dec = Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001 — any parse failure is a bad threshold
        raise ValueError(f"{field} is not a number: {value!r}") from exc
    if dec <= 0:
        raise ValueError(f"{field} must be > 0, got {dec}")
    return dec


def bar_dollar_volume(bar: Bar) -> Decimal:
    """Dollar (notional) volume contributed by a single source bar: close*volume."""
    return bar.close * bar.volume


@dataclass
class _Accumulator:
    """Mutable running aggregate of contributing source bars (internal only)."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    timestamp: object  # datetime of the last contributing bar
    symbol: Symbol
    running: Decimal  # running total in the chosen metric

    @classmethod
    def start(cls, bar: Bar, contribution: Decimal) -> "_Accumulator":
        return cls(
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            timestamp=bar.timestamp,
            symbol=bar.symbol,
            running=contribution,
        )

    def add(self, bar: Bar, contribution: Decimal) -> None:
        if bar.high > self.high:
            self.high = bar.high
        if bar.low < self.low:
            self.low = bar.low
        self.close = bar.close
        self.volume += bar.volume
        self.timestamp = bar.timestamp
        self.running += contribution

    def to_bar(self, timeframe: str) -> Bar:
        return Bar(
            symbol=self.symbol,
            timestamp=self.timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            timeframe=timeframe,
        )


def _contribution(bar: Bar, metric: BarMetric) -> Decimal:
    if metric is BarMetric.VOLUME:
        return bar.volume
    return bar_dollar_volume(bar)


def aggregate_bars(
    bars: Iterable[Bar],
    threshold: object,
    metric: BarMetric = BarMetric.DOLLAR_VOLUME,
    *,
    timeframe: Optional[str] = None,
    emit_partial: bool = False,
) -> List[Bar]:
    """
    Aggregate ``bars`` into volume or dollar-volume bars at ``threshold``.

    Args:
      bars: source ``Bar``s in the order to be aggregated (caller ensures
        chronological order; all bars should share one ``Symbol``).
      threshold: positive quantum (shares for ``VOLUME``, notional for
        ``DOLLAR_VOLUME``). Coerced to ``Decimal`` via ``str()``.
      metric: ``BarMetric.VOLUME`` or ``BarMetric.DOLLAR_VOLUME`` (default).
      timeframe: label for emitted bars; defaults to a descriptive string like
        ``"dollar@1000000"`` / ``"volume@50000"``.
      emit_partial: if True, flush a trailing sub-threshold accumulation as a
        final smaller bar; if False (default), drop it.

    Returns:
      A new list of aggregated ``Bar``s. Empty input (or all-dropped partial)
      yields an empty list.

    Raises:
      ValueError: if ``threshold`` is not a positive number, or if the input
        mixes more than one ``Symbol`` (aggregation across instruments is
        meaningless and would silently corrupt OHLC).
    """
    thr = _to_threshold(threshold)
    label = timeframe if timeframe is not None else f"{metric.value}@{thr.normalize():f}"

    out: List[Bar] = []
    acc: Optional[_Accumulator] = None

    for bar in bars:
        if acc is not None and bar.symbol.ticker != acc.symbol.ticker:
            raise ValueError(
                "aggregate_bars received bars for multiple symbols "
                f"({acc.symbol.ticker!r} and {bar.symbol.ticker!r}); "
                "aggregate one instrument at a time"
            )
        contribution = _contribution(bar, metric)
        if acc is None:
            acc = _Accumulator.start(bar, contribution)
        else:
            acc.add(bar, contribution)

        if acc.running >= thr:
            out.append(acc.to_bar(label))
            acc = None

    if acc is not None and emit_partial:
        out.append(acc.to_bar(label))

    return out


def aggregate_volume_bars(
    bars: Iterable[Bar],
    threshold: object,
    *,
    timeframe: Optional[str] = None,
    emit_partial: bool = False,
) -> List[Bar]:
    """Convenience: aggregate by cumulative share/contract volume."""
    return aggregate_bars(
        bars,
        threshold,
        BarMetric.VOLUME,
        timeframe=timeframe,
        emit_partial=emit_partial,
    )


def aggregate_dollar_volume_bars(
    bars: Iterable[Bar],
    threshold: object,
    *,
    timeframe: Optional[str] = None,
    emit_partial: bool = False,
) -> List[Bar]:
    """Convenience: aggregate by cumulative dollar (notional) volume."""
    return aggregate_bars(
        bars,
        threshold,
        BarMetric.DOLLAR_VOLUME,
        timeframe=timeframe,
        emit_partial=emit_partial,
    )


def total_metric(bars: Sequence[Bar], metric: BarMetric = BarMetric.DOLLAR_VOLUME) -> Decimal:
    """
    Sum the chosen metric over ``bars`` (helper for picking a threshold, e.g.
    ``total / desired_bar_count``). Empty input returns ``Decimal('0')``.
    """
    total = Decimal("0")
    for bar in bars:
        total += _contribution(bar, metric)
    return total
