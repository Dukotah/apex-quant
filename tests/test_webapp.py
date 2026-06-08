"""
Tests for scripts.webapp — the comprehensive static site generator. The pure
builders (_summary, _modules, build_site) are exercised; the HTTP server in main()
is run by the operator, not in CI.
"""

from __future__ import annotations

from scripts import webapp

_STATE = {
    "mode": "paper",
    "broker": "alpaca",
    "equity": "99900.28",
    "peak_equity": "100000",
    "cash": "50000",
    "total_runs": 12,
    "last_ts": "2024-06-03T20:00",
}


def test_summary_extracts_first_line(tmp_path):
    f = tmp_path / "mod.py"
    # Header path + underline are skipped; extraction stops at the blank line.
    f.write_text('"""\nmod.py\n======\nDoes a useful thing.\n\nMore detail later.\n"""\n')
    s = webapp._summary(f)
    assert s == "Does a useful thing."


def test_summary_handles_missing_or_bad_docstring(tmp_path):
    f = tmp_path / "nodoc.py"
    f.write_text("x = 1\n")
    assert webapp._summary(f) == ""


def test_modules_discovers_real_strategy_library():
    items = webapp._modules(["apex/strategy/library/*.py"])
    names = {name for _rel, name, _summ in items}
    assert "multi_asset_trend" in names  # the deployed strategy is present
    assert "__init__" not in names  # dunders excluded
    assert len(items) > 15  # the library is sizeable


def test_build_site_is_comprehensive():
    # Every declared subsystem section must render, driven by the real tree.
    sections = [
        (sid, title, lead, webapp._modules(globs)) for sid, title, globs, lead in webapp.SECTIONS
    ]
    html = webapp.build_site(_STATE, sections, webapp._SHIPPED, "PROGRESS LINE 1")

    assert "<!doctype html>" in html.lower()
    assert "$99,900.28" in html  # live overview rendered
    assert "12/30" in html  # paper-gate progress
    for sid, _title, _lead, _items in sections:
        assert f'id="{sid}"' in html  # each subsystem section present
    assert 'id="validation"' in html and "Gate 1" in html  # Gauntlet panel injected
    assert "PROGRESS LINE 1" in html
    # No unrendered template artifacts leaked into the output.
    assert "{html.escape" not in html and "{sid}" not in html


def test_build_site_marks_deployed_strategy_live():
    sections = [
        (sid, title, lead, webapp._modules(globs)) for sid, title, globs, lead in webapp.SECTIONS
    ]
    html = webapp.build_site(_STATE, sections, webapp._SHIPPED, "log")
    assert ">LIVE<" in html  # the deployed strategy carries a LIVE badge
