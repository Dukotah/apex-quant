"""
Tests for scripts.monthly_report — the monthly-returns table (read-only).

The core (monthly_equity_endpoints / monthly_returns / build_monthly_table) is a
pure function of (timestamp, equity) pairs, so every value here is hand-computed.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from scripts.monthly_report import (
    build_monthly_table,
    monthly_equity_endpoints,
    monthly_returns,
)
from scripts.run_once import RunReport, StateStore

UTC = timezone.utc


# ------------------------------------------------------------------ pure core


def test_endpoints_keep_month_end_equity():
    pts = [
        ("2024-01-05T00:00:00+00:00", 100.0),
        ("2024-01-20T00:00:00+00:00", 110.0),  # later in Jan -> overwrites
        ("2024-02-10T00:00:00+00:00", 120.0),
        ("2024-02-28T00:00:00+00:00", 121.0),  # later in Feb -> overwrites
        ("2024-03-01T00:00:00+00:00", 150.0),
    ]
    assert monthly_equity_endpoints(pts) == [
        (2024, 1, 110.0),
        (2024, 2, 121.0),
        (2024, 3, 150.0),
    ]


def test_endpoints_empty():
    assert monthly_equity_endpoints([]) == []


def test_endpoints_skip_unparseable_ts_fail_closed():
    pts = [
        ("2024-01-15T00:00:00+00:00", 100.0),
        ("garbage", 999.0),  # skipped, not guessed
        ("2024-02-15T00:00:00+00:00", 105.0),
    ]
    assert monthly_equity_endpoints(pts) == [(2024, 1, 100.0), (2024, 2, 105.0)]


def test_monthly_returns_known_values():
    pts = [
        ("2024-01-31T00:00:00+00:00", 100.0),
        ("2024-02-29T00:00:00+00:00", 110.0),  # +10%
        ("2024-03-31T00:00:00+00:00", 99.0),  # 99/110 - 1 = -10%
    ]
    out = monthly_returns(pts)
    assert out[0] == (2024, 1, None)  # no prior month
    assert out[1][:2] == (2024, 2)
    assert math.isclose(out[1][2], 0.10, abs_tol=1e-12)
    assert out[2][:2] == (2024, 3)
    assert math.isclose(out[2][2], -0.10, abs_tol=1e-12)


def test_monthly_returns_with_starting_equity():
    pts = [("2024-01-31T00:00:00+00:00", 105.0)]
    out = monthly_returns(pts, starting_equity=100.0)
    assert math.isclose(out[0][2], 0.05, abs_tol=1e-12)  # 105/100 - 1


def test_monthly_returns_starting_equity_zero_is_none():
    pts = [("2024-01-31T00:00:00+00:00", 105.0)]
    out = monthly_returns(pts, starting_equity=0.0)
    assert out[0][2] is None  # divide-by-zero avoided


def test_monthly_returns_empty():
    assert monthly_returns([]) == []


# --------------------------------------------------------------- table render


def test_table_no_data_message():
    out = build_monthly_table([], mode="paper")
    assert "hasn't completed a cycle" in out


def test_table_has_structure_and_ytd():
    pts = [
        ("2024-01-31T00:00:00+00:00", 100.0),
        ("2024-02-29T00:00:00+00:00", 110.0),  # +10%
        ("2024-03-31T00:00:00+00:00", 121.0),  # +10% -> YTD compounds to +21%
    ]
    out = build_monthly_table(pts, mode="paper")
    assert "MONTHLY RETURNS" in out
    assert "mode paper" in out
    assert "Jan" in out and "Dec" in out and "YTD" in out
    assert "2024" in out
    assert "+10.0%" in out  # the monthly cells
    assert "+21.0%" in out  # YTD = 1.10 * 1.10 - 1


def test_table_first_month_blank_then_ytd_from_second():
    # No starting equity -> first month is '--', YTD uses only measurable months.
    pts = [
        ("2024-01-31T00:00:00+00:00", 100.0),  # first -> --
        ("2024-02-29T00:00:00+00:00", 105.0),  # +5%
    ]
    out = build_monthly_table(pts, mode="paper")
    assert "--" in out
    assert "+5.0%" in out


def test_table_spans_multiple_years():
    pts = [
        ("2024-12-31T00:00:00+00:00", 100.0),
        ("2025-01-31T00:00:00+00:00", 102.0),  # +2% (Jan 2025 vs Dec 2024)
    ]
    out = build_monthly_table(pts, mode="paper")
    assert "2024" in out and "2025" in out
    assert "+2.0%" in out


# --------------------------------------------------------- end-to-end via DB


def _seed(store, equities, mode="paper"):
    for i, eq in enumerate(equities):
        # one row per month so each lands in a distinct calendar month
        ts = datetime(2024, 1 + i, 15, tzinfo=UTC)
        store.save_run(RunReport(timestamp=ts, mode=mode, equity=float(eq), num_positions=0), {})


def test_load_points_from_state_store(tmp_path):
    from scripts.monthly_report import _load_points

    store = StateStore(tmp_path / "s.db")
    _seed(store, [100.0, 110.0, 121.0])
    store.close()
    pts = _load_points(str(tmp_path / "s.db"), "paper")
    assert len(pts) == 3
    assert pts[0][1] == 100.0 and pts[-1][1] == 121.0
    # ordered oldest->newest by ts
    assert pts[0][0] < pts[1][0] < pts[2][0]


def test_main_prints_table(tmp_path, capsys):
    from scripts.monthly_report import main

    store = StateStore(tmp_path / "s.db")
    _seed(store, [100.0, 110.0])
    store.close()
    rc = main(["--db", str(tmp_path / "s.db"), "--mode", "paper"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "MONTHLY RETURNS" in captured
    assert "+10.0%" in captured
