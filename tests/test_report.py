"""
Tests for scripts.report — the paper-gate monitor (read-only over the state DB).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.report import GATE_DAYS, build_report
from scripts.run_once import RunReport, StateStore

UTC = timezone.utc


def _seed(store, equities, orders=0):
    base = datetime(2024, 1, 1, tzinfo=UTC)
    for i, eq in enumerate(equities):
        store.save_run(RunReport(timestamp=base + timedelta(days=i), mode="paper",
                                 equity=float(eq), num_positions=0,
                                 orders_submitted=orders), {})


def test_no_runs_message(tmp_path):
    store = StateStore(tmp_path / "s.db")
    assert "hasn't completed a cycle" in build_report(store, "paper")


def test_report_has_core_metrics(tmp_path):
    store = StateStore(tmp_path / "s.db")
    _seed(store, [100000, 100500, 101000, 101200, 101800], orders=2)
    out = build_report(store, "paper")
    assert "PAPER GATE REPORT" in out
    assert "total return" in out and "+1.80%" in out      # 101800/100000 - 1
    assert "30-day gate" in out
    assert f"5/{GATE_DAYS} days" in out                    # 5 cycles recorded
    assert "running" in out                                # < 30 days, gate not passed


def test_report_gate_passes_after_30_days_with_edge(tmp_path):
    store = StateStore(tmp_path / "s.db")
    # 32 days of noisy-but-positive gains -> high rolling Sharpe, gate criteria met.
    eq, v = [], 100000.0
    for i in range(32):
        v *= (1.006 if i % 2 == 0 else 1.002)    # net +0.4%/day with real variance
        eq.append(v)
    _seed(store, eq)
    out = build_report(store, "paper")
    assert f"{len(eq)}/{GATE_DAYS} days" in out
    assert "GATE PASSED" in out


def test_report_counts_activity_and_drawdown(tmp_path):
    store = StateStore(tmp_path / "s.db")
    _seed(store, [100000, 95000, 102000], orders=3)       # a dip then recovery
    out = build_report(store, "paper")
    assert "max drawdown" in out
    assert "9 orders" in out                                # 3 orders x 3 cycles
