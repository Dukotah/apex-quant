"""
Tests for scripts.health — the read-only one-screen system status.

Determinism: ``now`` is injected into gather_status, so the staleness math is exact
and hand-verifiable. The kill switch is read from APEX_HALT via monkeypatch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from apex.core.config import AppConfig, Broker, ExecutionMode
from apex.risk.risk_manager import RiskConfig
from scripts.health import (
    DEFAULT_STALE_AFTER_HOURS,
    gather_status,
    main,
    render,
)
from scripts.run_once import RunReport, StateStore

UTC = timezone.utc


def _config(**risk_kwargs) -> AppConfig:
    return AppConfig(
        mode=ExecutionMode.PAPER,
        broker=Broker.ALPACA,
        risk=RiskConfig(**risk_kwargs),
    )


def _seed_run(store, ts, mode="paper", positions=None):
    store.save_run(
        RunReport(timestamp=ts, mode=mode, equity=100000.0, num_positions=0),
        positions or {},
    )


# --------------------------------------------------------------------- staleness


def test_fresh_run_is_ok(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_HALT", raising=False)
    store = StateStore(tmp_path / "s.db")
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    _seed_run(store, now - timedelta(hours=2))  # 2h ago -> fresh
    status = gather_status(_config(), store, now=now)
    assert status.stale is False
    assert status.ok is True
    assert status.age_hours == 2.0


def test_stale_run_is_a_problem(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_HALT", raising=False)
    store = StateStore(tmp_path / "s.db")
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    _seed_run(store, now - timedelta(hours=DEFAULT_STALE_AFTER_HOURS + 1))
    status = gather_status(_config(), store, now=now)
    assert status.stale is True
    assert status.ok is False


def test_no_runs_recorded_is_stale(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_HALT", raising=False)
    store = StateStore(tmp_path / "s.db")
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    status = gather_status(_config(), store, now=now)
    assert status.last_run_ts is None
    assert status.stale is True  # never ran -> no trustworthy state
    assert status.ok is False


def test_custom_stale_threshold(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_HALT", raising=False)
    store = StateStore(tmp_path / "s.db")
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    _seed_run(store, now - timedelta(hours=5))
    # 5h old: stale under a 4h threshold, fresh under the default.
    assert gather_status(_config(), store, now=now, stale_after_hours=4).stale is True
    assert gather_status(_config(), store, now=now, stale_after_hours=48).stale is False


# ----------------------------------------------------------------- kill switch


def test_kill_switch_armed_is_a_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_HALT", "1")
    store = StateStore(tmp_path / "s.db")
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    _seed_run(store, now - timedelta(hours=1))  # fresh, but halted
    status = gather_status(_config(), store, now=now)
    assert status.halted is True
    assert status.ok is False


def test_kill_switch_off_values(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_HALT", "off")
    store = StateStore(tmp_path / "s.db")
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    _seed_run(store, now - timedelta(hours=1))
    assert gather_status(_config(), store, now=now).halted is False


# ----------------------------------------------------------------- positions


def test_open_positions_counted_from_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_HALT", raising=False)
    store = StateStore(tmp_path / "s.db")
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    positions = {
        "SPY": {"qty": "10", "avg_entry_price": "400", "current_price": "410"},
        "GLD": {"qty": "5", "avg_entry_price": "180", "current_price": "182"},
    }
    _seed_run(store, now - timedelta(hours=1), positions=positions)
    status = gather_status(_config(), store, now=now)
    assert status.open_positions == 2


# ----------------------------------------------------------------- config sanity


def test_whitelist_size_and_risk_caps(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_HALT", raising=False)
    store = StateStore(tmp_path / "s.db")
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    _seed_run(store, now - timedelta(hours=1))
    cfg = _config(
        symbol_whitelist=frozenset({"SPY", "GLD", "TLT"}),
        max_position_size_pct=Decimal("0.16"),
        max_drawdown_pct=Decimal("0.40"),
    )
    status = gather_status(cfg, store, now=now)
    assert status.whitelist_size == 3
    assert status.max_position_size_pct == "0.16"
    assert status.max_drawdown_pct == "0.40"


def test_no_whitelist_means_all_allowed(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_HALT", raising=False)
    store = StateStore(tmp_path / "s.db")
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    _seed_run(store, now - timedelta(hours=1))
    status = gather_status(_config(), store, now=now)
    assert status.whitelist_size is None
    out = render(status)
    assert "all (no whitelist)" in out


# ----------------------------------------------------------------- rendering


def test_render_contains_key_sections(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_HALT", raising=False)
    store = StateStore(tmp_path / "s.db")
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    _seed_run(store, now - timedelta(hours=1))
    out = render(gather_status(_config(max_position_size_pct=Decimal("0.16")), store, now=now))
    assert "SYSTEM HEALTH" in out
    assert "mode/broker  paper / alpaca" in out
    assert "kill switch  off" in out
    assert "16.0%" in out  # 0.16 -> 16.0%
    assert "OK — healthy" in out


def test_render_flags_stale_and_halt(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_HALT", "true")
    store = StateStore(tmp_path / "s.db")
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    status = gather_status(_config(), store, now=now)  # no runs + halt
    out = render(status)
    assert "ARMED" in out
    assert "PROBLEM" in out


# ----------------------------------------------------------------- exit codes


def test_main_exits_zero_when_healthy(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("APEX_HALT", raising=False)
    monkeypatch.setenv("APEX_MODE", "paper")
    monkeypatch.setenv("APEX_BROKER", "alpaca")
    db = tmp_path / "s.db"
    store = StateStore(db)
    _seed_run(store, datetime.now(UTC) - timedelta(hours=1))
    store.close()
    rc = main(["--state-path", str(db)])
    assert rc == 0
    assert "SYSTEM HEALTH" in capsys.readouterr().out


def test_main_exits_nonzero_when_halted(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_HALT", "1")
    monkeypatch.setenv("APEX_MODE", "paper")
    monkeypatch.setenv("APEX_BROKER", "alpaca")
    db = tmp_path / "s.db"
    store = StateStore(db)
    _seed_run(store, datetime.now(UTC) - timedelta(hours=1))
    store.close()
    assert main(["--state-path", str(db)]) == 1


def test_main_exits_nonzero_when_stale(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_HALT", raising=False)
    monkeypatch.setenv("APEX_MODE", "paper")
    monkeypatch.setenv("APEX_BROKER", "alpaca")
    db = tmp_path / "s.db"  # empty DB -> no runs -> stale
    assert main(["--state-path", str(db)]) == 1


def test_module_importable_without_side_effects():
    # Importing must not touch env, disk, or network. Re-import is a no-op.
    import importlib

    import scripts.health as health

    importlib.reload(health)
    assert hasattr(health, "gather_status")
