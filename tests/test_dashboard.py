"""
Tests for scripts.dashboard.build_page — the pure HTML assembly. The HTTP server in main()
is run by the operator, not in CI.
"""

from __future__ import annotations

from scripts.dashboard import build_page, export_to


def test_build_page_includes_status_shipped_and_progress():
    shipped = [("Phase F1 — validate edge", "MERGED", "value edge real")]
    html = build_page("STATUS BLOCK HERE", shipped, "PROGRESS LINE 1\nPROGRESS LINE 2")
    assert "<!doctype html>" in html.lower()
    assert "Apex Quant" in html
    assert "STATUS BLOCK HERE" in html  # live status embedded
    assert "Phase F1" in html and "MERGED" in html  # shipped row rendered
    assert "PROGRESS LINE 1" in html  # progress log embedded
    assert "github.com/Dukotah/apex-quant" in html


def test_build_page_escapes_html():
    # User/data text must be HTML-escaped, never injected raw.
    html = build_page("<script>x</script>", [("a<b", "S&P", "d>e")], "log")
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
