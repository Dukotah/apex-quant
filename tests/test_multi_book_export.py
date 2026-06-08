"""
tests/test_multi_book_export.py
===============================
Tests for the multi-book status assembly in export_status: the deployed book stays
the top-level snapshot (back-compat) AND becomes books[0]; each experiment DB
becomes an experiment book entry with leaderboard summary stats. Driven by temp
state DBs — no network, no live engine.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.export_status import build_book_entry, build_multi_status
from scripts.run_once import RunReport, StateStore

UTC = timezone.utc
NOW = datetime(2024, 7, 1, tzinfo=UTC)


def _seed(store: StateStore, equities):
    """Record one paper run per equity point, ascending timestamps."""
    base = datetime(2024, 6, 1, tzinfo=UTC)
    for i, eq in enumerate(equities):
        report = RunReport(
            timestamp=base + timedelta(days=i), mode="paper", equity=float(eq), num_positions=0
        )
        store.save_run(report, {})


def test_build_multi_status_assembles_books(tmp_path):
    dep = StateStore(tmp_path / "dep.db")
    e1 = StateStore(tmp_path / "e1.db")
    try:
        _seed(dep, [100000, 101000, 102000])  # +2%
        _seed(e1, [100000, 99000, 100500])  # dipped then recovered
        snap = build_multi_status(
            now=NOW, deployed_store=dep, experiments=[(e1, "rsi2_spy", "RSI(2) SPY")]
        )

        # Back-compat: the single-book top-level fields are all still present.
        for key in ("account", "equityCurve", "positions", "paperGate", "strategies"):
            assert key in snap

        books = snap["books"]
        assert len(books) == 2
        assert books[0]["kind"] == "deployed"
        assert books[0]["id"] == "deployed"
        assert books[1]["kind"] == "experiment"
        assert books[1]["id"] == "rsi2_spy"
        assert books[1]["name"] == "RSI(2) SPY"

        summary = books[0]["summary"]
        assert set(summary) >= {
            "totalReturnPct",
            "sharpe",
            "maxDrawdownPct",
            "dayPnlPct",
            "sessions",
        }
        assert summary["sessions"] == 3
        assert summary["totalReturnPct"] > 0  # deployed book grew
        # The dipped experiment book recorded a real drawdown.
        assert books[1]["summary"]["maxDrawdownPct"] > 0
    finally:
        dep.close()
        e1.close()


def test_empty_book_is_valid(tmp_path):
    empty = StateStore(tmp_path / "empty.db")
    try:
        entry = build_book_entry(
            empty, book_id="new_book", name="New Book", kind="experiment", now=NOW
        )
        assert entry["id"] == "new_book"
        assert entry["summary"]["sessions"] == 0
        assert entry["positions"] == []
    finally:
        empty.close()
