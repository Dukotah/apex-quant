"""
Tests for scripts.perf_report — the standalone text performance report.

Focus is the PURE core (compute_perf_stats / build_perf_report): hand-computed
known values plus edge cases. The thin SQLite CLI is exercised once against a
real on-disk state DB.
"""
from __future__ import annotations

import math

from scripts.perf_report import (
    PerfStats,
    build_perf_report,
    compute_perf_stats,
)


def test_import_has_no_side_effects():
    # Importing the module must not require apex/state machinery; the symbols
    # exist and the dataclass is usable without any I/O.
    assert callable(compute_perf_stats)
    assert PerfStats.__name__ == "PerfStats"


def test_total_return_hand_computed():
    stats = compute_perf_stats([100.0, 110.0, 121.0])
    assert stats is not None
    # 121 / 100 - 1 = 0.21
    assert math.isclose(stats.total_return, 0.21, rel_tol=1e-12)
    assert stats.start_equity == 100.0
    assert stats.end_equity == 121.0
    assert stats.points == 3


def test_constant_returns_give_zero_sharpe():
    # [100,110,121] -> returns [0.1, 0.1], zero variance -> Sharpe defined as 0.0.
    stats = compute_perf_stats([100.0, 110.0, 121.0])
    assert stats is not None
    assert stats.sharpe == 0.0
    assert stats.sortino == 0.0


def test_max_drawdown_hand_computed():
    # peak 120 then trough 90 -> (120-90)/120 = 0.25, recovery to 130 after.
    stats = compute_perf_stats([100.0, 120.0, 90.0, 130.0])
    assert stats is not None
    assert math.isclose(stats.max_drawdown, 0.25, rel_tol=1e-12)


def test_positive_sharpe_for_uneven_gains():
    # Real variance in the returns -> a finite, positive Sharpe.
    stats = compute_perf_stats([100.0, 102.0, 101.0, 105.0, 104.0, 110.0])
    assert stats is not None
    assert stats.sharpe > 0.0
    assert math.isfinite(stats.sharpe)


def test_insufficient_data_returns_none():
    assert compute_perf_stats([]) is None
    assert compute_perf_stats([100.0]) is None


def test_determinism_same_input_same_output():
    curve = [100.0, 101.0, 99.0, 103.0]
    a = build_perf_report(curve, label="paper")
    b = build_perf_report(curve, label="paper")
    assert a == b


def test_report_contains_core_sections():
    out = build_perf_report([100000.0, 100500.0, 101000.0, 101800.0], label="paper")
    assert "PERFORMANCE REPORT (paper)" in out
    assert "total return" in out
    assert "Sharpe" in out
    assert "max drawdown" in out
    assert "+1.80%" in out  # 101800/100000 - 1


def test_report_handles_insufficient_data_gracefully():
    out = build_perf_report([100.0], label="paper")
    assert "not enough data" in out
    assert "PERFORMANCE REPORT (paper)" in out


def test_periods_per_year_affects_annualization():
    curve = [100.0, 101.0, 102.0, 103.0]
    daily = compute_perf_stats(curve, periods_per_year=252)
    annual = compute_perf_stats(curve, periods_per_year=1)
    assert daily is not None and annual is not None
    # More periods/year compounds a small per-period gain far harder.
    assert daily.annualized_return > annual.annualized_return


def test_cli_core_over_real_state_db(tmp_path):
    # Exercise the lazy SQLite read path through the actual run_once StateStore.
    from datetime import datetime, timedelta, timezone

    from scripts.perf_report import _load_equities
    from scripts.run_once import RunReport, StateStore

    db = tmp_path / "s.db"
    store = StateStore(db)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i, eq in enumerate([100000.0, 100500.0, 101000.0]):
        store.save_run(
            RunReport(timestamp=base + timedelta(days=i), mode="paper",
                      equity=eq, num_positions=0),
            {},
        )
    store.close()

    equities = _load_equities(str(db), "paper")
    assert equities == [100000.0, 100500.0, 101000.0]

    out = build_perf_report(equities, label="paper")
    assert "PERFORMANCE REPORT (paper)" in out

    # An empty/other mode yields an empty curve, not an error.
    assert _load_equities(str(db), "live") == []
