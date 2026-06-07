"""Tests for apex.data.returns_aggregator — pure, fast, hand-computed values."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from apex.data.returns_aggregator import (
    PeriodReturn,
    compound,
    to_monthly,
    to_weekly,
    to_yearly,
)

APPROX = 1e-12


# --------------------------------------------------------------------- compound


def test_compound_basic_hand_computed():
    # (1.01)(0.99) - 1 = 0.9999 - 1 = -0.0001
    assert compound([0.01, -0.01]) == pytest.approx(-0.0001, abs=APPROX)


def test_compound_growth():
    # (1.10)(1.10) - 1 = 1.21 - 1 = 0.21
    assert compound([0.10, 0.10]) == pytest.approx(0.21, abs=APPROX)


def test_compound_empty_is_zero():
    assert compound([]) == 0.0


def test_compound_single():
    assert compound([0.05]) == pytest.approx(0.05, abs=APPROX)


def test_compound_minus_one_wipes_out():
    # A -100% day takes the factor to 0 → return -1.0 overall.
    assert compound([0.5, -1.0, 0.5]) == pytest.approx(-1.0, abs=APPROX)


def test_compound_rejects_below_minus_one():
    with pytest.raises(ValueError):
        compound([-1.5])


def test_compound_rejects_nan():
    with pytest.raises(ValueError):
        compound([float("nan")])


# --------------------------------------------------------------------- monthly


def test_monthly_groups_and_compounds():
    daily = [
        (date(2024, 1, 5), 0.10),
        (date(2024, 1, 20), 0.10),  # Jan: (1.1)(1.1)-1 = 0.21
        (date(2024, 2, 3), 0.05),  # Feb: 0.05
    ]
    out = to_monthly(daily)
    assert [p.label for p in out] == ["2024-01", "2024-02"]
    assert out[0].ret == pytest.approx(0.21, abs=APPROX)
    assert out[0].count == 2
    assert out[0].start == date(2024, 1, 5)
    assert out[0].end == date(2024, 1, 20)
    assert out[1].ret == pytest.approx(0.05, abs=APPROX)
    assert out[1].count == 1


def test_monthly_chronological_even_if_input_unsorted():
    daily = [
        (date(2024, 3, 1), 0.02),
        (date(2024, 1, 1), 0.01),
        (date(2024, 2, 1), 0.03),
    ]
    out = to_monthly(daily)
    assert [p.label for p in out] == ["2024-01", "2024-02", "2024-03"]


def test_monthly_spans_years():
    daily = [
        (date(2023, 12, 31), 0.01),
        (date(2024, 1, 1), 0.02),
    ]
    out = to_monthly(daily)
    assert [p.label for p in out] == ["2023-12", "2024-01"]
    assert out[0].ret == pytest.approx(0.01, abs=APPROX)
    assert out[1].ret == pytest.approx(0.02, abs=APPROX)


# ---------------------------------------------------------------------- yearly


def test_yearly_compounds_full_year():
    # Three +10% periods in one year → 1.1^3 - 1 = 0.331
    daily = [
        (date(2024, 1, 1), 0.10),
        (date(2024, 6, 1), 0.10),
        (date(2024, 12, 1), 0.10),
        (date(2025, 1, 1), -0.05),
    ]
    out = to_yearly(daily)
    assert [p.label for p in out] == ["2024", "2025"]
    assert out[0].ret == pytest.approx(1.1**3 - 1, abs=APPROX)
    assert out[0].count == 3
    assert out[1].ret == pytest.approx(-0.05, abs=APPROX)


# ---------------------------------------------------------------------- weekly


def test_weekly_iso_week_grouping():
    # 2024-01-01 is a Monday → ISO 2024-W01. 2024-01-07 is Sunday, still W01.
    # 2024-01-08 is Monday → W02.
    daily = [
        (date(2024, 1, 1), 0.01),
        (date(2024, 1, 7), 0.02),  # same ISO week W01
        (date(2024, 1, 8), 0.03),  # ISO week W02
    ]
    out = to_weekly(daily)
    assert [p.label for p in out] == ["2024-W01", "2024-W02"]
    assert out[0].ret == pytest.approx(1.01 * 1.02 - 1, abs=APPROX)
    assert out[0].count == 2
    assert out[1].ret == pytest.approx(0.03, abs=APPROX)


def test_weekly_iso_year_boundary():
    # 2025-01-01 is a Wednesday belonging to ISO week 2025-W01.
    # 2024-12-30 (Mon) and 2024-12-31 (Tue) also belong to ISO 2025-W01.
    daily = [
        (date(2024, 12, 30), 0.01),
        (date(2024, 12, 31), 0.01),
        (date(2025, 1, 1), 0.01),
    ]
    out = to_weekly(daily)
    assert len(out) == 1
    assert out[0].label == "2025-W01"
    assert out[0].count == 3
    assert out[0].ret == pytest.approx(1.01**3 - 1, abs=APPROX)


# ----------------------------------------------------------------- edge cases


def test_empty_input_returns_empty():
    assert to_weekly([]) == []
    assert to_monthly([]) == []
    assert to_yearly([]) == []


def test_accepts_datetime_and_iso_string_dates():
    daily = [
        (datetime(2024, 1, 5, 16, 0, tzinfo=timezone.utc), 0.10),
        ("2024-01-20", 0.10),
        ("2024-01-25T16:00:00Z", 0.10),
    ]
    out = to_monthly(daily)
    assert len(out) == 1
    assert out[0].label == "2024-01"
    assert out[0].count == 3
    assert out[0].ret == pytest.approx(1.1**3 - 1, abs=APPROX)


def test_period_return_is_frozen():
    daily = [(date(2024, 1, 1), 0.01)]
    p = to_monthly(daily)[0]
    assert isinstance(p, PeriodReturn)
    with pytest.raises(Exception):
        p.ret = 0.5  # type: ignore[misc]


def test_aggregate_rejects_bad_return():
    with pytest.raises(ValueError):
        to_monthly([(date(2024, 1, 1), -2.0)])


def test_minus_one_hundred_percent_day_in_bucket():
    # A -100% day inside a month wipes the whole month to -1.0 regardless of others.
    daily = [
        (date(2024, 1, 1), 0.20),
        (date(2024, 1, 2), -1.0),
        (date(2024, 1, 3), 0.20),
    ]
    out = to_monthly(daily)
    assert out[0].ret == pytest.approx(-1.0, abs=APPROX)
