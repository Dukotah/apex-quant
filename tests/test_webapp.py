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


def _render_html(progress: str = "PROGRESS LINE 1"):
    sections = [
        (sid, title, lead, webapp._modules(globs)) for sid, title, globs, lead in webapp.SECTIONS
    ]
    return webapp.build_site(_STATE, sections, webapp._SHIPPED, progress)


def test_build_site_has_plain_language_structure():
    html = _render_html()
    assert "<!doctype html>" in html.lower()
    # Friendly top-level sections for a newcomer.
    for sid in ("what", "how", "inside", "gauntlet", "faq", "glossary", "status", "roadmap"):
        assert f'id="{sid}"' in html
    # Six capability tiles.
    for cid in ("strategies", "safety", "validation", "data", "analytics", "ops"):
        assert f'id="{cid}"' in html
    assert "$99,900.28" in html  # live status in the hero
    assert "12/30" in html  # paper-trial progress
    assert "Gate 1" in html  # the testing section
    assert "Is real money at risk" in html  # plain-language FAQ present
    assert "Paper trading" in html  # plain-language glossary present
    assert "PROGRESS LINE 1" in html  # recent updates folded in
    # No unrendered template artifacts leaked into the output.
    assert "{html.escape" not in html and "{sid}" not in html


def test_build_site_is_comprehensive_under_expanders():
    # Progressive disclosure: every subsystem's modules still render (in the detail
    # expanders), so nothing is lost in the simplified view.
    html = _render_html()
    for token in (
        "multi asset trend",  # strategies
        "deflated sharpe",  # validation
        "capital allocation",  # risk
        "monte carlo",  # validation/stats
        "alpaca",  # execution / data
    ):
        assert token in html, token
    # The detail is hidden by default behind <details> expanders.
    assert "<details>" in html and "building blocks" in html


def test_build_site_marks_deployed_strategy_live():
    sections = [
        (sid, title, lead, webapp._modules(globs)) for sid, title, globs, lead in webapp.SECTIONS
    ]
    html = webapp.build_site(_STATE, sections, webapp._SHIPPED, "log")
    assert ">LIVE<" in html  # the deployed strategy carries a LIVE badge
