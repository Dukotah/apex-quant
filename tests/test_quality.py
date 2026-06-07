"""
tests.test_quality
===================
Unit tests for ``apex.data.quality.data_quality_report``.

Strategy: build a clean, on-grid single-symbol bar series and assert it reports
``is_clean``; then seed exactly one defect of each category into an otherwise
clean copy and assert that category — and only the expected categories — fires,
against hand-computed expectations.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from apex.core.models import AssetClass, Bar, Symbol
from apex.data.quality import (
    QualityReport,
    _expected_cadence,
    data_quality_report,
)

SYM = Symbol(ticker="TEST", asset_class=AssetClass.EQUITY)
START = datetime(2024, 1, 1, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def _bar(
    ts: datetime,
    *,
    o: str = "100",
    h: str = "101",
    low: str = "99",
    c: str = "100",
    v: str = "1000",
    timeframe: str = "1Day",
) -> Bar:
    return Bar(
        symbol=SYM,
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(v),
        timeframe=timeframe,
    )


def _clean_series(n: int = 10) -> list[Bar]:
    """n on-grid daily bars, distinct timestamps, modest moves, positive volume."""
    # high/low must envelope the rising close, or the Bar invariant rejects it.
    return [
        _bar(START + i * DAY, o=str(100 + i), h=str(101 + i), low=str(99 + i), c=str(100 + i))
        for i in range(n)
    ]


# ----------------------------------------------------------------- happy path


def test_clean_series_is_clean() -> None:
    report = data_quality_report(_clean_series())
    assert isinstance(report, QualityReport)
    assert report.is_clean is True
    assert report.total_defects == 0
    assert report.bar_count == 10
    assert report.expected_cadence == DAY


def test_empty_is_clean() -> None:
    report = data_quality_report([])
    assert report.is_clean is True
    assert report.bar_count == 0
    assert report.expected_cadence is None


def test_report_is_frozen() -> None:
    report = data_quality_report(_clean_series())
    try:
        report.bar_count = 99  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("QualityReport should be immutable (frozen)")


# ------------------------------------------------------------- cadence parsing


def test_expected_cadence_variants() -> None:
    assert _expected_cadence("1Day") == timedelta(days=1)
    assert _expected_cadence("5Min") == timedelta(minutes=5)
    assert _expected_cadence("15Min") == timedelta(minutes=15)
    assert _expected_cadence("1Hour") == timedelta(hours=1)
    assert _expected_cadence("4Hour") == timedelta(hours=4)
    assert _expected_cadence("1Week") == timedelta(weeks=1)
    assert _expected_cadence("Day") == timedelta(days=1)  # implicit count of 1
    assert _expected_cadence("bogus") is None
    assert _expected_cadence("") is None
    assert _expected_cadence("0Day") is None


# ----------------------------------------------------------- one defect each


def test_detects_gap() -> None:
    bars = _clean_series(5)
    # Drop the bar at index 2 → between idx1 (day1) and idx3 (day3) two days pass.
    del bars[2]
    report = data_quality_report(bars)
    assert report.gap_count == 1
    # The later bar of the missing-span pair is recorded.
    assert report.gaps == [START + 3 * DAY]
    # Nothing else should fire.
    assert report.duplicate_count == 0
    assert report.out_of_order_count == 0
    assert report.is_clean is False


def test_detects_duplicate() -> None:
    bars = _clean_series(5)
    dup_ts = bars[2].timestamp
    bars.insert(3, _bar(dup_ts, c="102", h="103"))
    report = data_quality_report(bars)
    assert report.duplicate_count == 1
    assert report.duplicate_timestamps == [dup_ts]
    assert report.gap_count == 0


def test_detects_out_of_order() -> None:
    bars = _clean_series(5)
    # Swap two adjacent bars so the stream goes backwards once.
    bars[2], bars[3] = bars[3], bars[2]
    report = data_quality_report(bars)
    assert report.out_of_order_count == 1
    # The earlier-than-previous timestamp is the one now sitting at index 3.
    assert report.out_of_order_timestamps == [START + 2 * DAY]


def test_detects_nonpositive_volume() -> None:
    bars = _clean_series(5)
    bad_ts = bars[2].timestamp
    bars[2] = _bar(bad_ts, c="102", h="103", v="0")
    report = data_quality_report(bars)
    assert report.nonpositive_volume_count == 1
    assert report.nonpositive_volume_timestamps == [bad_ts]


def test_detects_zero_range() -> None:
    bars = _clean_series(5)
    bad_ts = bars[2].timestamp
    bars[2] = _bar(bad_ts, o="100", h="100", low="100", c="100")
    report = data_quality_report(bars)
    assert report.zero_range_count == 1
    assert report.zero_range_timestamps == [bad_ts]


def test_detects_extreme_jump() -> None:
    bars = _clean_series(5)
    # idx1 close is 101; make idx2 close 200 → move = (200-101)/101 ≈ 0.98 > 0.5.
    bad_ts = bars[2].timestamp
    bars[2] = _bar(bad_ts, o="200", h="201", low="199", c="200")
    report = data_quality_report(bars)
    assert report.extreme_jump_count == 1
    assert report.extreme_jump_timestamps == [bad_ts]


def test_jump_ratio_is_configurable() -> None:
    bars = _clean_series(5)
    bad_ts = bars[2].timestamp
    # ~0.98 move: under a 1.5 threshold it is NOT flagged.
    bars[2] = _bar(bad_ts, o="200", h="201", low="199", c="200")
    report = data_quality_report(bars, jump_ratio=1.5)
    assert report.extreme_jump_count == 0
    assert report.is_clean is True


# ------------------------------------------------------------- combinations


def test_all_defects_at_once() -> None:
    """A messy series should catch every category simultaneously."""
    bars = [
        _bar(START + 0 * DAY, c="100"),
        _bar(START + 1 * DAY, c="101"),
        _bar(START + 1 * DAY, c="101"),  # duplicate of day1
        _bar(START + 3 * DAY, c="101"),  # gap (day2 missing)
        _bar(START + 2 * DAY, c="101"),  # out-of-order (before day3)
        _bar(START + 4 * DAY, c="101", v="0"),  # zero volume
        _bar(START + 5 * DAY, o="100", h="100", low="100", c="100"),  # zero range
        _bar(START + 6 * DAY, o="300", h="301", low="299", c="300"),  # extreme jump
    ]
    report = data_quality_report(bars)
    assert report.duplicate_count == 1
    assert report.gap_count == 1
    assert report.out_of_order_count == 1
    assert report.nonpositive_volume_count == 1
    assert report.zero_range_count == 1
    assert report.extreme_jump_count == 1
    assert report.is_clean is False
    assert report.total_defects == 6


def test_minute_cadence_gap() -> None:
    """Gap detection adapts to a non-daily timeframe."""
    base = START
    minute = timedelta(minutes=5)
    bars = [
        _bar(base + 0 * minute, c="100", timeframe="5Min"),
        _bar(base + 1 * minute, c="100", timeframe="5Min"),
        _bar(base + 3 * minute, c="100", timeframe="5Min"),  # 5Min bar missing
    ]
    report = data_quality_report(bars)
    assert report.expected_cadence == minute
    assert report.gap_count == 1
    assert report.gaps == [base + 3 * minute]


def test_non_grid_spacing_is_not_a_gap() -> None:
    """A non-integer-multiple spacing is ambiguous and must not be flagged."""
    bars = [
        _bar(START, c="100", timeframe="1Day"),
        # 1.5 days later: not a clean multiple of the cadence → not a gap.
        _bar(START + timedelta(days=1, hours=12), c="100", timeframe="1Day"),
    ]
    report = data_quality_report(bars)
    assert report.gap_count == 0
    assert report.is_clean is True
