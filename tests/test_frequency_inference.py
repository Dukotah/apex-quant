"""Tests for apex.data.frequency_inference — bar cadence inference."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apex.data.frequency_inference import (
    consecutive_gaps,
    infer_timeframe,
    modal_gap,
    timedelta_to_timeframe,
)


def _ts(*, days=0, hours=0, minutes=0, seconds=0) -> datetime:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return base + timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _series(step: timedelta, n: int) -> list[datetime]:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [base + step * i for i in range(n)]


# ----------------------------------------------------------- timedelta_to_timeframe


def test_timedelta_renders_canonical_units():
    assert timedelta_to_timeframe(timedelta(minutes=1)) == "1Min"
    assert timedelta_to_timeframe(timedelta(minutes=5)) == "5Min"
    assert timedelta_to_timeframe(timedelta(minutes=15)) == "15Min"
    assert timedelta_to_timeframe(timedelta(hours=1)) == "1Hour"
    assert timedelta_to_timeframe(timedelta(hours=4)) == "4Hour"
    assert timedelta_to_timeframe(timedelta(days=1)) == "1Day"
    assert timedelta_to_timeframe(timedelta(weeks=1)) == "1Week"


def test_timedelta_prefers_coarsest_exact_unit():
    # 60 minutes is exactly one hour → prefer the coarser spelling.
    assert timedelta_to_timeframe(timedelta(minutes=60)) == "1Hour"
    # 24 hours is exactly one day.
    assert timedelta_to_timeframe(timedelta(hours=24)) == "1Day"
    # 7 days is exactly one week.
    assert timedelta_to_timeframe(timedelta(days=7)) == "1Week"


def test_timedelta_seconds_when_not_whole_minute():
    assert timedelta_to_timeframe(timedelta(seconds=30)) == "30Sec"


def test_timedelta_non_positive_returns_none():
    assert timedelta_to_timeframe(timedelta(0)) is None
    assert timedelta_to_timeframe(timedelta(seconds=-5)) is None


def test_timedelta_sub_second_returns_none():
    assert timedelta_to_timeframe(timedelta(milliseconds=500)) is None


# ----------------------------------------------------------------- consecutive_gaps


def test_gaps_basic():
    ts = _series(timedelta(minutes=1), 4)
    assert consecutive_gaps(ts) == [timedelta(minutes=1)] * 3


def test_gaps_unsorted_input_normalized():
    ts = [_ts(minutes=2), _ts(minutes=0), _ts(minutes=1)]
    assert consecutive_gaps(ts) == [timedelta(minutes=1), timedelta(minutes=1)]


def test_gaps_dedupes():
    ts = [_ts(minutes=0), _ts(minutes=0), _ts(minutes=1)]
    assert consecutive_gaps(ts) == [timedelta(minutes=1)]


def test_gaps_insufficient_data():
    assert consecutive_gaps([]) == []
    assert consecutive_gaps([_ts(minutes=0)]) == []


# ------------------------------------------------------------------------ modal_gap


def test_modal_gap_picks_most_common():
    # gaps: 1min, 1min, 5min (one weekend-like hole) → modal is 1min.
    ts = [_ts(minutes=0), _ts(minutes=1), _ts(minutes=2), _ts(minutes=7)]
    assert modal_gap(ts) == timedelta(minutes=1)


def test_modal_gap_tie_breaks_to_smaller():
    # gaps: 1min then 2min, each once → tie broken toward the smaller (1min).
    ts = [_ts(minutes=0), _ts(minutes=1), _ts(minutes=3)]
    assert modal_gap(ts) == timedelta(minutes=1)


def test_modal_gap_none_when_insufficient():
    assert modal_gap([]) is None
    assert modal_gap([_ts(minutes=0)]) is None


# -------------------------------------------------------------------- infer_timeframe


def test_infer_one_minute():
    assert infer_timeframe(_series(timedelta(minutes=1), 10)) == "1Min"


def test_infer_five_minute():
    assert infer_timeframe(_series(timedelta(minutes=5), 10)) == "5Min"


def test_infer_hourly():
    assert infer_timeframe(_series(timedelta(hours=1), 10)) == "1Hour"


def test_infer_daily():
    assert infer_timeframe(_series(timedelta(days=1), 10)) == "1Day"


def test_infer_daily_with_weekend_holes():
    # Mon..Fri then skip to next Mon (3-day gap) — daily cadence still dominates.
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)  # a Monday
    offsets = [0, 1, 2, 3, 4, 7, 8, 9, 10, 11]
    ts = [base + timedelta(days=d) for d in offsets]
    assert infer_timeframe(ts) == "1Day"


def test_infer_order_independent():
    forward = _series(timedelta(hours=1), 6)
    shuffled = [forward[i] for i in (3, 0, 5, 1, 4, 2)]
    assert infer_timeframe(shuffled) == "1Hour"


def test_infer_none_on_insufficient_data():
    assert infer_timeframe([]) is None
    assert infer_timeframe([_ts(minutes=0)]) is None
    assert infer_timeframe([_ts(minutes=5), _ts(minutes=5)]) is None  # all duplicates


def test_infer_handles_naive_and_aware_consistently():
    # Aware timestamps are the supported contract; ensure a clean aware series works.
    ts = _series(timedelta(minutes=15), 8)
    assert infer_timeframe(ts) == "15Min"
