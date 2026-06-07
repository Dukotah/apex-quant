"""Tests for apex.analytics.monthly_returns_table.

Hand-computed known values plus edge cases (empty input, single point, gap
years, out-of-order input, same-day returns, None for missing months).
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from apex.analytics.monthly_returns_table import (
    MONTH_ABBRS,
    format_table,
    monthly_returns,
    monthly_returns_table,
    year_range,
    yearly_returns,
)


def _close(a, b, tol=1e-12):
    return a is not None and b is not None and math.isclose(a, b, abs_tol=tol)


# --------------------------------------------------------------------------
# monthly_returns
# --------------------------------------------------------------------------


def test_monthly_returns_compounds_within_month():
    # Jan 2020: (1.10)(0.95) - 1 = 0.045
    data = [
        (date(2020, 1, 5), 0.10),
        (date(2020, 1, 20), -0.05),
    ]
    out = monthly_returns(data)
    assert set(out) == {(2020, 1)}
    assert _close(out[(2020, 1)], 0.045)


def test_monthly_returns_separates_months():
    data = [
        (date(2020, 1, 5), 0.10),
        (date(2020, 1, 20), -0.05),
        (date(2020, 2, 3), 0.02),
    ]
    out = monthly_returns(data)
    assert _close(out[(2020, 1)], 0.045)
    assert _close(out[(2020, 2)], 0.02)


def test_monthly_returns_order_independent():
    ordered = [
        (date(2020, 1, 5), 0.10),
        (date(2020, 1, 20), -0.05),
    ]
    shuffled = list(reversed(ordered))
    assert monthly_returns(ordered) == monthly_returns(shuffled)


def test_monthly_returns_same_day_compounds_in_input_order():
    # Compounding is commutative so the value matches regardless, but assert it.
    data = [
        (date(2020, 3, 1), 0.10),
        (date(2020, 3, 1), 0.20),
    ]
    out = monthly_returns(data)
    # (1.1)(1.2) - 1 = 0.32
    assert _close(out[(2020, 3)], 0.32)


def test_monthly_returns_empty():
    assert monthly_returns([]) == {}


def test_monthly_returns_zero_is_not_dropped():
    out = monthly_returns([(date(2021, 6, 1), 0.0)])
    assert out == {(2021, 6): 0.0}


# --------------------------------------------------------------------------
# yearly_returns
# --------------------------------------------------------------------------


def test_yearly_returns_compounds_months():
    data = [
        (date(2020, 1, 5), 0.10),
        (date(2020, 1, 20), -0.05),  # Jan -> 0.045
        (date(2020, 2, 3), 0.02),  # Feb -> 0.02
    ]
    out = yearly_returns(data)
    # (1.045)(1.02) - 1 = 0.0659
    assert _close(out[2020], 1.045 * 1.02 - 1.0)


def test_yearly_returns_multiple_years():
    data = [
        (date(2019, 12, 31), 0.05),
        (date(2020, 1, 1), 0.10),
    ]
    out = yearly_returns(data)
    assert set(out) == {2019, 2020}
    assert _close(out[2019], 0.05)
    assert _close(out[2020], 0.10)


def test_yearly_returns_empty():
    assert yearly_returns([]) == {}


# --------------------------------------------------------------------------
# monthly_returns_table
# --------------------------------------------------------------------------


def test_table_shape_and_none_for_missing_months():
    data = [
        (date(2020, 1, 5), 0.10),
        (date(2020, 1, 20), -0.05),
        (date(2020, 2, 3), 0.02),
    ]
    table = monthly_returns_table(data)
    assert set(table) == {2020}
    row = table[2020]
    assert len(row) == 13  # 12 months + total
    assert _close(row[0], 0.045)  # Jan
    assert _close(row[1], 0.02)  # Feb
    # Mar..Dec are missing -> None
    assert all(row[i] is None for i in range(2, 12))
    # Year total
    assert _close(row[12], 1.045 * 1.02 - 1.0)


def test_table_year_total_consistent_with_yearly_returns():
    data = [
        (date(2021, 3, 1), 0.07),
        (date(2021, 9, 15), -0.03),
        (date(2021, 11, 30), 0.04),
    ]
    table = monthly_returns_table(data)
    expected = yearly_returns(data)[2021]
    assert _close(table[2021][12], expected)


def test_table_empty():
    assert monthly_returns_table([]) == {}


def test_table_only_years_with_data_are_keys():
    # 2020 and 2022 have data, 2021 does not -> 2021 not a key.
    data = [
        (date(2020, 5, 1), 0.01),
        (date(2022, 5, 1), 0.02),
    ]
    table = monthly_returns_table(data)
    assert set(table) == {2020, 2022}


# --------------------------------------------------------------------------
# year_range
# --------------------------------------------------------------------------


def test_year_range_fills_gaps():
    data = [
        (date(2020, 5, 1), 0.01),
        (date(2022, 5, 1), 0.02),
    ]
    assert year_range(data) == [2020, 2021, 2022]


def test_year_range_single_year():
    assert year_range([(date(2020, 1, 1), 0.0)]) == [2020]


def test_year_range_empty():
    assert year_range([]) == []


# --------------------------------------------------------------------------
# format_table
# --------------------------------------------------------------------------


def test_format_table_empty():
    assert format_table([]) == ""


def test_format_table_header_and_values():
    data = [
        (date(2020, 1, 5), 0.10),
        (date(2020, 1, 20), -0.05),  # Jan -> 4.50%
        (date(2020, 2, 3), 0.02),  # Feb -> 2.00%
    ]
    text = format_table(data, na="-", decimals=2)
    lines = text.splitlines()
    header = lines[0].split()
    assert header[0] == "Year"
    assert header[1:13] == list(MONTH_ABBRS)
    assert header[13] == "Total"

    row = lines[1].split()
    assert row[0] == "2020"
    assert row[1] == "4.50"  # Jan
    assert row[2] == "2.00"  # Feb
    # Missing months show the na placeholder.
    assert row[3] == "-"


def test_format_table_fills_year_gaps_with_na():
    data = [
        (date(2020, 6, 1), 0.01),
        (date(2022, 6, 1), 0.02),
    ]
    text = format_table(data, fill_year_gaps=True, na="NA")
    years_shown = [line.split()[0] for line in text.splitlines()[1:]]
    assert years_shown == ["2020", "2021", "2022"]
    # The 2021 row should be entirely NA (no data that year).
    row_2021 = [ln for ln in text.splitlines() if ln.split()[0] == "2021"][0]
    cells = row_2021.split()[1:]
    assert all(c == "NA" for c in cells)


def test_format_table_no_gap_fill_skips_empty_years():
    data = [
        (date(2020, 6, 1), 0.01),
        (date(2022, 6, 1), 0.02),
    ]
    text = format_table(data, fill_year_gaps=False)
    years_shown = [line.split()[0] for line in text.splitlines()[1:]]
    assert years_shown == ["2020", "2022"]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
