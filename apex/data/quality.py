"""
apex.data.quality
=================
Offline data-quality auditing for a single-symbol stream of ``Bar``s.

A backtest is only as trustworthy as the bars it replays. Vendor exports lie in
boringly predictable ways: a missing trading session leaves a *gap*; a botched
merge produces *duplicate* timestamps; an unsorted file yields *out-of-order*
bars; a halted/illiquid session shows *zero volume*; a bad print shows a
*zero-range* (open==high==low==close) bar or an *extreme jump* (a 10x close that
is almost always a split/adjustment artifact, not a real move).

``data_quality_report`` scans a chronological-ish list of bars and returns a
frozen ``QualityReport`` naming exactly which timestamps tripped which check,
plus an overall ``is_clean`` flag. It is **pure and deterministic**: no I/O, no
clock, no randomness. It never mutates the bars and never raises on dirty data —
detecting defects is the whole job, so a defect is a finding, not an error.

This is statistical/structural auditing, not P&L math: timestamps and counts are
the currency here, and the one float in sight (the jump ratio) is a tolerance
knob, never money. Prices themselves stay ``Decimal`` throughout, matching the
``Bar`` model.

Cadence for gap detection is derived from each bar's ``timeframe`` string
("1Min", "5Min", "15Min", "1Hour", "1Day", ...) so the audit adapts to any
granularity without configuration. Calendar gaps that a real market schedule
explains (weekends, holidays, overnight closes) are *not* something a pure,
calendar-unaware function can distinguish from true missing data — so gap
detection deliberately flags only spacing that is a clean integer multiple of
the cadence (i.e. one or more whole bars are missing between two otherwise
on-grid bars), which is the unambiguous signal. See ``_expected_cadence``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional, Sequence

from apex.core.models import Bar

# Default trip point for the extreme-jump check: a bar whose close moves by more
# than this ratio vs the previous close is almost certainly a bad print or an
# unadjusted split, not a tradable move. A float because it is a tolerance knob,
# never money — the prices it compares stay Decimal.
DEFAULT_JUMP_RATIO: float = 0.5  # 50% move bar-over-bar

# Apex timeframe units → the timedelta of one unit. Mirrors the spellings the
# feeds emit ("1Min", "5Min", "15Min", "1Hour", "4Hour", "1Day", "1Week").
_UNIT_DELTAS: dict[str, timedelta] = {
    "min": timedelta(minutes=1),
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "hr": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
}

# "5Min" / "15 min" / "1Day" / "4Hour" → (count, unit). Case-insensitive.
_TIMEFRAME_RE = re.compile(r"^\s*(\d+)?\s*([a-zA-Z]+)\s*$")


@dataclass(frozen=True)
class QualityReport:
    """
    Immutable result of auditing a list of bars. Each list holds the *offending*
    timestamps for that category (in scan order), and the matching ``*_count``
    is its length. ``is_clean`` is True iff every category is empty.

    ``bar_count`` and ``expected_cadence`` are reported for context — the cadence
    is the per-bar spacing gap detection used (``None`` when it could not be
    inferred, e.g. fewer than one bar or an unrecognized timeframe).
    """
    bar_count: int
    expected_cadence: Optional[timedelta]
    gaps: List[datetime] = field(default_factory=list)
    duplicate_timestamps: List[datetime] = field(default_factory=list)
    out_of_order_timestamps: List[datetime] = field(default_factory=list)
    nonpositive_volume_timestamps: List[datetime] = field(default_factory=list)
    zero_range_timestamps: List[datetime] = field(default_factory=list)
    extreme_jump_timestamps: List[datetime] = field(default_factory=list)

    @property
    def gap_count(self) -> int:
        return len(self.gaps)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicate_timestamps)

    @property
    def out_of_order_count(self) -> int:
        return len(self.out_of_order_timestamps)

    @property
    def nonpositive_volume_count(self) -> int:
        return len(self.nonpositive_volume_timestamps)

    @property
    def zero_range_count(self) -> int:
        return len(self.zero_range_timestamps)

    @property
    def extreme_jump_count(self) -> int:
        return len(self.extreme_jump_timestamps)

    @property
    def total_defects(self) -> int:
        return (
            self.gap_count
            + self.duplicate_count
            + self.out_of_order_count
            + self.nonpositive_volume_count
            + self.zero_range_count
            + self.extreme_jump_count
        )

    @property
    def is_clean(self) -> bool:
        return self.total_defects == 0


def _expected_cadence(timeframe: str) -> Optional[timedelta]:
    """
    Parse an Apex timeframe string ("5Min", "1Hour", "1Day", ...) into the
    timedelta of one bar. Returns ``None`` for an unrecognized timeframe so the
    caller can skip gap detection rather than guess.
    """
    match = _TIMEFRAME_RE.match(timeframe or "")
    if match is None:
        return None
    count = int(match.group(1)) if match.group(1) else 1
    unit = _UNIT_DELTAS.get(match.group(2).lower())
    if unit is None or count <= 0:
        return None
    return unit * count


def data_quality_report(
    bars: Sequence[Bar],
    *,
    jump_ratio: float = DEFAULT_JUMP_RATIO,
    cadence: Optional[timedelta] = None,
) -> QualityReport:
    """
    Audit a single-symbol, roughly-chronological list of ``Bar``s and report
    structural data-quality defects. Pure and deterministic — no I/O, no clock.

    Detects, per category, the offending bar timestamps:

      - **gaps**: between two consecutive in-order bars, whole bars are missing
        (the spacing is an exact integer multiple ``>= 2`` of the cadence). The
        timestamp of the *later* bar of the pair is recorded. Only meaningful
        when a cadence can be inferred; if it cannot, gaps are not reported.
      - **duplicate_timestamps**: a timestamp equal to one already seen.
      - **out_of_order_timestamps**: a timestamp strictly earlier than the
        previous bar's (the stream went backwards).
      - **nonpositive_volume_timestamps**: ``volume <= 0`` (a halted/synthetic
        bar; ``Bar`` already forbids *negative* volume, so this catches zero).
      - **zero_range_timestamps**: ``open == high == low == close`` — a frozen
        print with no intrabar movement, suspicious for liquid instruments.
      - **extreme_jump_timestamps**: ``|close/prev_close - 1| > jump_ratio``
        vs the previous *in-order* bar — almost always a bad tick or an
        unadjusted split rather than a real move.

    Args:
        bars: the bars to audit, ideally already chronological (out-of-order is
            itself a reported defect). Not mutated.
        jump_ratio: fractional close-over-close move that counts as "extreme"
            (default 0.5 = 50%). Must be > 0.
        cadence: override the per-bar spacing used for gap detection. When
            ``None``, it is inferred from the first bar's ``timeframe``.

    Returns:
        A frozen ``QualityReport``. An empty input yields a clean report.
    """
    bar_count = len(bars)
    if cadence is None and bar_count > 0:
        cadence = _expected_cadence(bars[0].timeframe)

    gaps: List[datetime] = []
    duplicates: List[datetime] = []
    out_of_order: List[datetime] = []
    nonpositive_volume: List[datetime] = []
    zero_range: List[datetime] = []
    extreme_jumps: List[datetime] = []

    seen: set[datetime] = set()
    prev_ts: Optional[datetime] = None
    prev_close: Optional[Decimal] = None

    jump_threshold = Decimal(str(jump_ratio))

    for bar in bars:
        ts = bar.timestamp

        # --- per-bar checks (independent of neighbours) ---
        if bar.volume <= 0:
            nonpositive_volume.append(ts)
        if bar.open == bar.high == bar.low == bar.close:
            zero_range.append(ts)

        # --- duplicate vs out-of-order vs gap (ordering checks) ---
        if ts in seen:
            duplicates.append(ts)
        seen.add(ts)

        if prev_ts is not None:
            if ts < prev_ts:
                out_of_order.append(ts)
            elif cadence is not None and ts > prev_ts:
                delta = ts - prev_ts
                # Whole-bar(s) missing: spacing is an exact multiple > 1 of cadence.
                quotient, remainder = divmod(delta, cadence)
                if remainder == timedelta(0) and quotient >= 2:
                    gaps.append(ts)

        # --- extreme jump vs the previous in-order close ---
        if (
            prev_close is not None
            and prev_close != 0
            and (prev_ts is None or ts >= prev_ts)
        ):
            move = abs(bar.close - prev_close) / abs(prev_close)
            if move > jump_threshold:
                extreme_jumps.append(ts)

        # Advance the "previous" cursors only on non-regressing timestamps so a
        # single out-of-order bar doesn't poison gap/jump detection afterwards.
        if prev_ts is None or ts >= prev_ts:
            prev_ts = ts
            prev_close = bar.close

    return QualityReport(
        bar_count=bar_count,
        expected_cadence=cadence,
        gaps=gaps,
        duplicate_timestamps=duplicates,
        out_of_order_timestamps=out_of_order,
        nonpositive_volume_timestamps=nonpositive_volume,
        zero_range_timestamps=zero_range,
        extreme_jump_timestamps=extreme_jumps,
    )
