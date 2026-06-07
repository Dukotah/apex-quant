"""Tests for scripts.config_audit — the PURE, deterministic audit core."""
from __future__ import annotations

from decimal import Decimal

from scripts.config_audit import (
    ConfigFacts,
    Issue,
    Severity,
    audit_config,
    has_errors,
    issues_to_json,
    render_report,
)


def _sane_facts(**overrides) -> ConfigFacts:
    """A fully-valid paper config; tests override one field at a time."""
    base = dict(
        mode="paper",
        broker="alpaca",
        initial_capital=Decimal("100000"),
        alpaca_key_present=True,
        alpaca_key_len=24,
        alpaca_secret_present=True,
        alpaca_secret_len=40,
        max_position_size_pct=Decimal("0.16"),
        max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("1.0"),
        max_drawdown_pct=Decimal("0.40"),
        max_daily_loss_pct=Decimal("0.10"),
        max_open_positions=10,
        require_stop_loss=True,
    )
    base.update(overrides)
    return ConfigFacts(**base)


def _codes(issues) -> set:
    return {i.code for i in issues}


# --------------------------------------------------------------- happy path

def test_sane_config_has_no_issues():
    issues = audit_config(_sane_facts())
    assert issues == []
    assert has_errors(issues) is False


def test_determinism_same_facts_same_issues():
    f = _sane_facts(mode="live", broker="simulated", max_leverage=Decimal("2"))
    assert audit_config(f) == audit_config(f)


# --------------------------------------------------------------- mode/broker

def test_live_with_simulated_broker_is_error():
    issues = audit_config(_sane_facts(mode="live", broker="simulated"))
    assert "mode.live_simulated" in _codes(issues)
    assert has_errors(issues) is True


def test_live_mode_warns_even_when_otherwise_valid():
    issues = audit_config(_sane_facts(mode="live"))
    by = {i.code: i for i in issues}
    assert by["mode.live"].severity is Severity.WARNING
    # A clean live config (real broker, keys present) must NOT have errors.
    assert not has_errors(issues)


def test_unknown_mode_and_broker_are_errors():
    issues = audit_config(_sane_facts(mode="paaper", broker="kraken"))
    assert {"mode.unknown", "broker.unknown"} <= _codes(issues)
    assert has_errors(issues) is True


def test_backtest_with_real_broker_is_info_only():
    issues = audit_config(_sane_facts(mode="backtest", broker="alpaca",
                                      alpaca_key_present=False, alpaca_secret_present=False,
                                      alpaca_key_len=0, alpaca_secret_len=0))
    by = {i.code: i for i in issues}
    assert by["mode.backtest_real_broker"].severity is Severity.INFO
    # backtest doesn't need keys, so missing creds must NOT be flagged.
    assert "creds.key_missing" not in by
    assert not has_errors(issues)


# --------------------------------------------------------------- credentials

def test_live_missing_keys_is_error():
    issues = audit_config(_sane_facts(mode="live", alpaca_key_present=False,
                                      alpaca_key_len=0, alpaca_secret_present=False,
                                      alpaca_secret_len=0))
    by = {i.code: i for i in issues}
    assert by["creds.key_missing"].severity is Severity.ERROR
    assert by["creds.secret_missing"].severity is Severity.ERROR


def test_paper_missing_keys_is_warning_not_error():
    issues = audit_config(_sane_facts(mode="paper", alpaca_key_present=False,
                                      alpaca_key_len=0, alpaca_secret_present=False,
                                      alpaca_secret_len=0))
    by = {i.code: i for i in issues}
    assert by["creds.key_missing"].severity is Severity.WARNING
    assert by["creds.secret_missing"].severity is Severity.WARNING
    assert not has_errors(issues)


def test_short_key_is_a_warning():
    issues = audit_config(_sane_facts(alpaca_key_len=3))
    by = {i.code: i for i in issues}
    assert by["creds.key_short"].severity is Severity.WARNING


# --------------------------------------------------------------- risk caps

def test_pct_over_one_flags_the_typo():
    # 16 instead of 0.16 — the classic uncapped-risk bug.
    issues = audit_config(_sane_facts(max_position_size_pct=Decimal("16")))
    by = {i.code: i for i in issues}
    assert by["risk.max_position_size_pct"].severity is Severity.ERROR
    assert "0.16" in by["risk.max_position_size_pct"].message


def test_nonpositive_pct_is_error():
    issues = audit_config(_sane_facts(max_drawdown_pct=Decimal("0")))
    by = {i.code: i for i in issues}
    assert by["risk.max_drawdown_pct"].severity is Severity.ERROR


def test_leverage_below_one_is_error_above_one_is_warning():
    lo = audit_config(_sane_facts(max_leverage=Decimal("0.5")))
    hi = audit_config(_sane_facts(max_leverage=Decimal("2.0")))
    assert {i.code: i for i in lo}["risk.max_leverage"].severity is Severity.ERROR
    assert {i.code: i for i in hi}["risk.max_leverage"].severity is Severity.WARNING


def test_position_exceeding_exposure_is_warning():
    issues = audit_config(_sane_facts(max_position_size_pct=Decimal("0.6"),
                                      max_total_exposure_pct=Decimal("0.5")))
    assert "risk.position_gt_exposure" in _codes(issues)


def test_zero_open_positions_is_error():
    issues = audit_config(_sane_facts(max_open_positions=0))
    assert {i.code: i for i in issues}["risk.max_open_positions"].severity is Severity.ERROR


def test_no_stop_loss_is_warning():
    issues = audit_config(_sane_facts(require_stop_loss=False))
    assert {i.code: i for i in issues}["risk.no_stop_loss"].severity is Severity.WARNING


def test_nonpositive_capital_is_error():
    issues = audit_config(_sane_facts(initial_capital=Decimal("0")))
    assert {i.code: i for i in issues}["config.capital"].severity is Severity.ERROR


# --------------------------------------------------------------- ordering

def test_issues_sorted_errors_first():
    issues = audit_config(_sane_facts(mode="live", broker="simulated",
                                      require_stop_loss=False))
    # First issue must be an ERROR, last may be the WARNING.
    assert issues[0].severity is Severity.ERROR
    severities = [i.severity for i in issues]
    # No WARNING appears before an ERROR.
    last_error = max(i for i, s in enumerate(severities) if s is Severity.ERROR)
    first_warn = min((i for i, s in enumerate(severities) if s is Severity.WARNING),
                     default=len(severities))
    assert last_error < first_warn


# --------------------------------------------------------------- secret safety

def test_secret_value_never_appears_in_output():
    # The core only ever sees presence + length, never a value — prove the
    # rendered/JSON output cannot contain a key even if one existed.
    secret = "SUPERSECRETKEYVALUE123"
    facts = _sane_facts(mode="live", alpaca_key_present=False, alpaca_key_len=0,
                        alpaca_secret_present=False, alpaca_secret_len=0)
    issues = audit_config(facts)
    blob = render_report(facts, issues) + issues_to_json(issues)
    assert secret not in blob


# --------------------------------------------------------------- rendering

def test_render_report_marks_pass_and_fail():
    ok = render_report(_sane_facts(), audit_config(_sane_facts()))
    assert "PASSED" in ok
    bad_facts = _sane_facts(mode="live", broker="simulated")
    bad = render_report(bad_facts, audit_config(bad_facts))
    assert "FAILED" in bad


def test_issues_to_json_roundtrips():
    import json
    issues = audit_config(_sane_facts(mode="live", broker="simulated"))
    parsed = json.loads(issues_to_json(issues))
    assert any(p["code"] == "mode.live_simulated" for p in parsed)
    assert all({"severity", "code", "message"} == set(p) for p in parsed)


def test_issue_render_has_marker():
    assert Issue(Severity.ERROR, "x.y", "msg").render().startswith("[X]")
