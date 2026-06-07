"""
Tests for scripts.dashboard.build_page — the pure HTML assembly. The HTTP server in main()
is run by the operator, not in CI.
"""

from __future__ import annotations

from scripts.dashboard import build_page, export_to

_STATE = {
    "mode": "paper",
    "broker": "alpaca",
    "equity": "99900.28",
    "peak_equity": "100000",
    "cash": "50000",
    "positions": {"SPY": {"qty": "10", "avg_entry_price": "90", "current_price": "100"}},
    "total_runs": 12,
    "first_ts": "2024-05-20T20:00",
    "last_ts": "2024-06-03T20:00",
}


def test_build_page_includes_status_shipped_and_progress():
    shipped = [("Phase F1 — validate edge", "MERGED", "value edge real")]
    html = build_page(_STATE, shipped, "PROGRESS LINE 1\nPROGRESS LINE 2")
    assert "<!doctype html>" in html.lower()
    assert "Apex Quant" in html
    assert "$99,900.28" in html  # structured equity rendered as a stat
    assert "12/30" in html  # paper-gate progress
    assert "SPY" in html  # open position row
    assert "Phase F1" in html and "MERGED" in html  # shipped card rendered
    assert "PROGRESS LINE 1" in html  # progress log embedded
    assert "github.com/Dukotah/apex-quant" in html


def test_build_page_halt_banner_shows_when_halted():
    halted = dict(_STATE, apex_halt_env="1")
    assert "HALTED" in build_page(halted, [], "log")
    assert "armed" in build_page(_STATE, [], "log")  # control: not halted


def test_build_page_escapes_html():
    # Data text (positions, shipped, progress) must be HTML-escaped, never injected raw.
    state = dict(_STATE, positions={})
    html = build_page(state, [("a<b", "S&P", "d>e")], "<script>x</script>")
    assert "<script>x</script>" not in html  # the raw tag must not survive
    assert "&lt;script&gt;" in html
    assert "a&lt;b" in html and "S&amp;P" in html


def test_export_to_writes_static_html(tmp_path):
    # The GitHub Pages export renders the full page to a file, creating parent dirs.
    out = export_to(tmp_path / "site" / "index.html")
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "<!doctype html>" in text.lower()
    assert "Apex Quant" in text
