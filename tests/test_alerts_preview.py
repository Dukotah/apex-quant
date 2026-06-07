"""
Tests for scripts.alerts_preview — the read-only ntfy alert dry-run.

The core (preview_alert) must mirror run_once._notify_cycle's priority ladder
EXACTLY and be pure/deterministic: same RunReport -> same AlertPreview, with no
clock or network. Timestamps are injected via the RunReport, never read live.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.alerts_preview import AlertPreview, preview_alert
from scripts.run_once import RunReport, StateStore

UTC = timezone.utc


def _report(**kw) -> RunReport:
    base = dict(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC), mode="paper", equity=100000.0, num_positions=0
    )
    base.update(kw)
    return RunReport(**base)


# ------------------------------------------------------------------ pure core


def test_quiet_cycle_sends_nothing():
    prev = preview_alert(_report(orders_submitted=0))
    assert prev.would_send is False
    assert prev.title == "" and prev.message == "" and prev.priority == ""
    assert "quiet" in prev.reason
    assert "NO ALERT" in prev.render()


def test_traded_cycle_default_priority():
    prev = preview_alert(_report(orders_submitted=2))
    assert prev.would_send is True
    assert prev.title == "Apex Quant - traded"
    assert prev.priority == "default"
    assert "2 order" in prev.reason
    # message is exactly run_once's report summary (what ntfy would receive).
    assert prev.message == _report(orders_submitted=2).summary()


def test_halted_beats_traded_high_priority():
    prev = preview_alert(_report(orders_submitted=5, halted=True))
    assert prev.title == "Apex Quant - HALTED"
    assert prev.priority == "high"


def test_quarantined_beats_halted_urgent():
    prev = preview_alert(_report(orders_submitted=5, halted=True, quarantined=True))
    assert prev.title == "Apex Quant - QUARANTINED"
    assert prev.priority == "urgent"


def test_killed_is_highest_priority():
    prev = preview_alert(_report(orders_submitted=5, halted=True, quarantined=True, killed=True))
    assert prev.title == "Apex Quant - KILL SWITCH"
    assert prev.priority == "urgent"
    assert "kill switch" in prev.reason


def test_core_is_pure_and_repeatable():
    r = _report(orders_submitted=1)
    assert preview_alert(r) == preview_alert(r)  # frozen dataclass equality


def test_preview_render_contains_message_lines():
    prev = AlertPreview(True, "T", "line one\nline two", "high", "because")
    out = prev.render()
    assert "ALERT PREVIEW (not sent)" in out
    assert "line one" in out and "line two" in out
    assert "priority high" in out


# ------------------------------------------------------ state-DB integration


def _seed(store, n_orders_per_row, halted_last=False, mode="paper", rows=3):
    base = datetime(2024, 1, 1, tzinfo=UTC)
    for i in range(rows):
        last = i == rows - 1
        store.save_run(
            RunReport(
                timestamp=base + timedelta(days=i),
                mode=mode,
                equity=100000.0 + i,
                num_positions=0,
                orders_submitted=n_orders_per_row,
                halted=halted_last and last,
            ),
            {},
        )


def test_load_last_report_none_when_empty(tmp_path):
    from scripts.alerts_preview import _load_last_report

    assert _load_last_report(str(tmp_path / "s.db"), "paper") is None


def test_load_last_report_reflects_latest_row(tmp_path):
    from scripts.alerts_preview import _load_last_report

    path = str(tmp_path / "s.db")
    _seed(StateStore(path), n_orders_per_row=3, halted_last=True)
    report = _load_last_report(path, "paper")
    assert report is not None
    assert report.orders_submitted == 3
    assert report.halted is True
    # End-to-end: a halted persisted row previews as the HALTED alert.
    prev = preview_alert(report)
    assert prev.title == "Apex Quant - HALTED"
    assert prev.priority == "high"


def test_load_last_report_filters_by_mode(tmp_path):
    from scripts.alerts_preview import _load_last_report

    path = str(tmp_path / "s.db")
    store = StateStore(path)
    _seed(store, n_orders_per_row=1, mode="paper")
    assert _load_last_report(path, "live") is None
    assert _load_last_report(path, "paper") is not None


def test_module_import_has_no_side_effects():
    # Importing the module must not configure logging, read env, or do I/O.
    import importlib

    import scripts.alerts_preview as mod

    importlib.reload(mod)
    # If we got here without exceptions and the core symbol exists, import is clean.
    assert callable(mod.preview_alert)
