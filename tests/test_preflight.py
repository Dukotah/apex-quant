"""
tests/test_preflight.py
=======================
Unit tests for scripts/preflight.py.

We exercise every check by supplying fake inputs (a patched environ dict, fake
paths, a hand-built RiskConfig, or an injected fake execution engine) — no
network, no real broker, no real CSV data, no full backtest. All checks but
check_broker_reachable are pure; that one is kept offline via its engine_factory
seam.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from apex.risk.risk_manager import RiskConfig
from scripts.preflight import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_WARN,
    check_broker_reachable,
    check_config_loads,
    check_dirs_and_db,
    check_env_vars,
    check_halt_state,
    check_risk_config,
    run_all_checks,
)


# A fake execution engine implementing the seam check_broker_reachable needs.
class _FakeEngine:
    def __init__(self, equity=Decimal("100000"), raise_on=None):
        self._equity = equity
        self._raise_on = raise_on  # one of: "connect", "equity", None
        self.connected = False
        self.disconnected = False

    def connect(self):
        if self._raise_on == "connect":
            raise RuntimeError("broker unreachable: connection refused")
        self.connected = True

    def get_account_equity(self):
        if self._raise_on == "equity":
            raise RuntimeError("403 forbidden: api key revoked")
        return self._equity

    def disconnect(self):
        self.disconnected = True


# ============================================================= check_env_vars


class TestCheckEnvVars:
    def test_backtest_mode_no_keys_needed(self):
        env = {"APEX_MODE": "backtest"}
        _, status, detail = check_env_vars(env)
        assert status == STATUS_PASS
        assert "no broker credentials required" in detail

    def test_backtest_mode_default_when_absent(self):
        _, status, _ = check_env_vars({})  # no APEX_MODE key at all
        assert status == STATUS_PASS

    def test_paper_mode_both_keys_present(self):
        env = {
            "APEX_MODE": "paper",
            "ALPACA_API_KEY": "pk_test",
            "ALPACA_SECRET_KEY": "sk_test",
        }
        _, status, detail = check_env_vars(env)
        assert status == STATUS_PASS
        # Must say PRESENT, must NOT print the actual values
        assert "PRESENT" in detail
        assert "pk_test" not in detail
        assert "sk_test" not in detail

    def test_live_mode_both_keys_present(self):
        env = {
            "APEX_MODE": "live",
            "ALPACA_API_KEY": "real_key",
            "ALPACA_SECRET_KEY": "real_secret",
        }
        _, status, detail = check_env_vars(env)
        assert status == STATUS_PASS
        assert "PRESENT" in detail
        assert "real_key" not in detail
        assert "real_secret" not in detail

    def test_paper_mode_missing_api_key(self):
        env = {
            "APEX_MODE": "paper",
            # ALPACA_API_KEY absent
            "ALPACA_SECRET_KEY": "sk_test",
        }
        _, status, detail = check_env_vars(env)
        assert status == STATUS_FAIL
        assert "ALPACA_API_KEY" in detail

    def test_paper_mode_missing_secret_key(self):
        env = {
            "APEX_MODE": "paper",
            "ALPACA_API_KEY": "pk_test",
            # ALPACA_SECRET_KEY absent
        }
        _, status, detail = check_env_vars(env)
        assert status == STATUS_FAIL
        assert "ALPACA_SECRET_KEY" in detail

    def test_live_mode_both_keys_missing(self):
        env = {"APEX_MODE": "live"}
        _, status, detail = check_env_vars(env)
        assert status == STATUS_FAIL
        assert "ALPACA_API_KEY" in detail
        assert "ALPACA_SECRET_KEY" in detail

    def test_paper_mode_empty_string_key_treated_as_missing(self):
        env = {
            "APEX_MODE": "paper",
            "ALPACA_API_KEY": "   ",  # whitespace only
            "ALPACA_SECRET_KEY": "sk",
        }
        _, status, detail = check_env_vars(env)
        assert status == STATUS_FAIL
        assert "ALPACA_API_KEY" in detail

    def test_secret_value_never_leaked_in_fail_detail(self):
        """Even in a failure message, secret values must not appear."""
        env = {
            "APEX_MODE": "paper",
            "ALPACA_API_KEY": "SUPER_SECRET_KEY",
        }
        _, _, detail = check_env_vars(env)
        assert "SUPER_SECRET_KEY" not in detail

    def test_check_name_is_env_credentials(self):
        name, _, _ = check_env_vars({})
        assert name == "env.credentials"


# ============================================================ check_halt_state


class TestCheckHaltState:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "YES", "True", "ON"])
    def test_truthy_values_produce_warn(self, value: str):
        _, status, detail = check_halt_state({"APEX_HALT": value})
        assert status == STATUS_WARN
        assert "active" in detail.lower()

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_falsy_values_produce_pass(self, value: str):
        _, status, _ = check_halt_state({"APEX_HALT": value})
        assert status == STATUS_PASS

    def test_absent_key_produces_pass(self):
        _, status, _ = check_halt_state({})
        assert status == STATUS_PASS

    def test_check_name_is_halt_state(self):
        name, _, _ = check_halt_state({})
        assert name == "halt.state"


# ============================================================ check_dirs_and_db


class TestCheckDirsAndDb:
    def test_all_present_is_pass(self, tmp_path: Path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        db_path = state_dir / "apex_state.db"
        db_path.write_bytes(b"")  # empty file, just needs to exist
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        _, status, detail = check_dirs_and_db(state_dir, db_path, data_dir)
        assert status == STATUS_PASS
        assert str(db_path) in detail

    def test_missing_state_dir_is_fail(self, tmp_path: Path):
        state_dir = tmp_path / "state"  # NOT created
        db_path = state_dir / "apex_state.db"
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        _, status, detail = check_dirs_and_db(state_dir, db_path, data_dir)
        assert status == STATUS_FAIL
        assert "state" in detail

    def test_missing_data_dir_is_fail(self, tmp_path: Path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        db_path = state_dir / "apex_state.db"
        data_dir = tmp_path / "data"  # NOT created

        _, status, detail = check_dirs_and_db(state_dir, db_path, data_dir)
        assert status == STATUS_FAIL
        assert "data" in detail

    def test_db_absent_is_warn_not_fail(self, tmp_path: Path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        db_path = state_dir / "apex_state.db"  # NOT created
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        _, status, detail = check_dirs_and_db(state_dir, db_path, data_dir)
        assert status == STATUS_WARN
        assert "not found" in detail

    def test_check_name_is_dirs_state_db(self, tmp_path: Path):
        name, _, _ = check_dirs_and_db(tmp_path / "s", tmp_path / "s/db", tmp_path / "d")
        assert name == "dirs.state_db"


# ============================================================ check_risk_config


class TestCheckRiskConfig:
    def test_default_risk_config_passes(self):
        cfg = RiskConfig()
        _, status, detail = check_risk_config(cfg)
        assert status == STATUS_PASS
        assert "require_stop_loss=True" in detail

    def test_require_stop_loss_false_fails(self):
        cfg = RiskConfig(require_stop_loss=False)
        _, status, detail = check_risk_config(cfg)
        assert status == STATUS_FAIL
        assert "require_stop_loss" in detail

    def test_drawdown_zero_fails(self):
        cfg = RiskConfig(max_drawdown_pct=Decimal("0"))
        _, status, detail = check_risk_config(cfg)
        assert status == STATUS_FAIL
        assert "max_drawdown_pct" in detail

    def test_drawdown_above_one_fails(self):
        cfg = RiskConfig(max_drawdown_pct=Decimal("1.5"))
        _, status, detail = check_risk_config(cfg)
        assert status == STATUS_FAIL
        assert "max_drawdown_pct" in detail

    def test_drawdown_exactly_one_passes(self):
        cfg = RiskConfig(max_drawdown_pct=Decimal("1"))
        _, status, _ = check_risk_config(cfg)
        assert status == STATUS_PASS

    def test_daily_loss_zero_fails(self):
        cfg = RiskConfig(max_daily_loss_pct=Decimal("0"))
        _, status, detail = check_risk_config(cfg)
        assert status == STATUS_FAIL
        assert "max_daily_loss_pct" in detail

    def test_daily_loss_above_one_fails(self):
        cfg = RiskConfig(max_daily_loss_pct=Decimal("1.1"))
        _, status, detail = check_risk_config(cfg)
        assert status == STATUS_FAIL
        assert "max_daily_loss_pct" in detail

    def test_no_arg_uses_default_risk_config(self):
        """check_risk_config() with no argument should construct RiskConfig()."""
        _, status, _ = check_risk_config()
        assert status == STATUS_PASS

    def test_multiple_violations_reported_together(self):
        cfg = RiskConfig(
            require_stop_loss=False,
            max_drawdown_pct=Decimal("0"),
            max_daily_loss_pct=Decimal("2"),
        )
        _, status, detail = check_risk_config(cfg)
        assert status == STATUS_FAIL
        assert "require_stop_loss" in detail
        assert "max_drawdown_pct" in detail
        assert "max_daily_loss_pct" in detail

    def test_check_name_is_risk_config_sane(self):
        name, _, _ = check_risk_config()
        assert name == "risk.config_sane"


# ============================================================ check_config_loads


class TestCheckConfigLoads:
    def test_default_env_loads(self, monkeypatch):
        """APEX_MODE not set → defaults to backtest → should pass."""
        monkeypatch.delenv("APEX_MODE", raising=False)
        monkeypatch.delenv("APEX_BROKER", raising=False)
        _, status, _ = check_config_loads()
        assert status == STATUS_PASS

    def test_live_with_simulated_broker_fails(self, monkeypatch):
        """LIVE mode + simulated broker → AppConfig raises → FAIL."""
        monkeypatch.setenv("APEX_MODE", "live")
        monkeypatch.setenv("APEX_BROKER", "simulated")
        _, status, detail = check_config_loads()
        assert status == STATUS_FAIL
        assert "AppConfig.from_env() raised" in detail

    def test_check_name_is_config_loads(self, monkeypatch):
        monkeypatch.delenv("APEX_MODE", raising=False)
        name, _, _ = check_config_loads()
        assert name == "config.loads"


# ====================================================== check_broker_reachable


class TestCheckBrokerReachable:
    def test_backtest_mode_is_skipped_pass(self):
        called = []
        _, status, detail = check_broker_reachable(
            {"APEX_MODE": "backtest"},
            engine_factory=lambda: called.append(1) or _FakeEngine(),
        )
        assert status == STATUS_PASS
        assert "no live broker" in detail
        assert called == []  # factory must NOT be invoked when skipped

    def test_no_mode_defaults_to_skipped_pass(self):
        _, status, _ = check_broker_reachable({})
        assert status == STATUS_PASS

    def test_simulated_broker_is_skipped_pass(self):
        called = []
        _, status, detail = check_broker_reachable(
            {"APEX_MODE": "paper", "APEX_BROKER": "simulated"},
            engine_factory=lambda: called.append(1) or _FakeEngine(),
        )
        assert status == STATUS_PASS
        assert "simulated" in detail
        assert called == []

    def test_paper_alpaca_reachable_is_pass(self):
        engine = _FakeEngine(equity=Decimal("100000"))
        _, status, detail = check_broker_reachable(
            {"APEX_MODE": "paper", "APEX_BROKER": "alpaca"},
            engine_factory=lambda: engine,
        )
        assert status == STATUS_PASS
        assert "reachable" in detail
        assert "100000" in detail
        assert engine.connected and engine.disconnected

    def test_live_alpaca_reachable_is_pass(self):
        engine = _FakeEngine(equity=Decimal("50000"))
        _, status, _ = check_broker_reachable(
            {"APEX_MODE": "live", "APEX_BROKER": "alpaca"},
            engine_factory=lambda: engine,
        )
        assert status == STATUS_PASS

    def test_alpaca_crypto_broker_is_checked(self):
        engine = _FakeEngine(equity=Decimal("1000"))
        _, status, _ = check_broker_reachable(
            {"APEX_MODE": "paper", "APEX_BROKER": "alpaca_crypto"},
            engine_factory=lambda: engine,
        )
        assert status == STATUS_PASS

    def test_connect_failure_is_fail(self):
        engine = _FakeEngine(raise_on="connect")
        _, status, detail = check_broker_reachable(
            {"APEX_MODE": "paper", "APEX_BROKER": "alpaca"},
            engine_factory=lambda: engine,
        )
        assert status == STATUS_FAIL
        assert "round-trip failed" in detail

    def test_revoked_key_round_trip_is_fail(self):
        engine = _FakeEngine(raise_on="equity")
        _, status, detail = check_broker_reachable(
            {"APEX_MODE": "live", "APEX_BROKER": "alpaca"},
            engine_factory=lambda: engine,
        )
        assert status == STATUS_FAIL
        assert "round-trip failed" in detail
        # Best-effort disconnect still runs even when the equity call raised.
        assert engine.disconnected

    def test_factory_construction_failure_is_fail(self):
        def boom():
            raise RuntimeError("could not build engine")

        _, status, detail = check_broker_reachable(
            {"APEX_MODE": "paper", "APEX_BROKER": "alpaca"},
            engine_factory=boom,
        )
        assert status == STATUS_FAIL
        assert "round-trip failed" in detail

    def test_unfunded_account_is_warn(self):
        engine = _FakeEngine(equity=Decimal("0"))
        _, status, detail = check_broker_reachable(
            {"APEX_MODE": "paper", "APEX_BROKER": "alpaca"},
            engine_factory=lambda: engine,
        )
        assert status == STATUS_WARN
        assert "unfunded" in detail

    def test_negative_equity_is_warn(self):
        engine = _FakeEngine(equity=Decimal("-5"))
        _, status, _ = check_broker_reachable(
            {"APEX_MODE": "paper", "APEX_BROKER": "alpaca"},
            engine_factory=lambda: engine,
        )
        assert status == STATUS_WARN

    def test_check_name_is_broker_reachable(self):
        name, _, _ = check_broker_reachable({})
        assert name == "broker.reachable"


# ============================================================== run_all_checks


class TestRunAllChecks:
    def test_returns_six_results(self, monkeypatch):
        monkeypatch.delenv("APEX_MODE", raising=False)
        monkeypatch.delenv("APEX_BROKER", raising=False)
        results = run_all_checks()
        assert len(results) == 6

    def test_each_result_is_three_strings(self, monkeypatch):
        monkeypatch.delenv("APEX_MODE", raising=False)
        monkeypatch.delenv("APEX_BROKER", raising=False)
        for item in run_all_checks():
            assert len(item) == 3
            name, status, detail = item
            assert isinstance(name, str) and name
            assert status in (STATUS_PASS, STATUS_WARN, STATUS_FAIL)
            assert isinstance(detail, str)

    def test_expected_check_names_present(self, monkeypatch):
        monkeypatch.delenv("APEX_MODE", raising=False)
        monkeypatch.delenv("APEX_BROKER", raising=False)
        names = [name for name, _, _ in run_all_checks()]
        assert "config.loads" in names
        assert "env.credentials" in names
        assert "halt.state" in names
        assert "dirs.state_db" in names
        assert "risk.config_sane" in names
        assert "broker.reachable" in names


# ============================================================ main() exit codes


class TestMain:
    def test_main_exits_0_when_all_pass(self, monkeypatch, tmp_path):
        """Patch run_all_checks to return all PASSes → exit code 0."""
        import scripts.preflight as pf

        monkeypatch.setattr(
            pf,
            "run_all_checks",
            lambda: [
                ("check.a", STATUS_PASS, "ok"),
                ("check.b", STATUS_PASS, "ok"),
            ],
        )
        assert pf.main() == 0

    def test_main_exits_0_with_only_warns(self, monkeypatch):
        import scripts.preflight as pf

        monkeypatch.setattr(
            pf,
            "run_all_checks",
            lambda: [
                ("check.a", STATUS_PASS, "ok"),
                ("check.b", STATUS_WARN, "something to watch"),
            ],
        )
        assert pf.main() == 0

    def test_main_exits_1_on_any_fail(self, monkeypatch):
        import scripts.preflight as pf

        monkeypatch.setattr(
            pf,
            "run_all_checks",
            lambda: [
                ("check.a", STATUS_PASS, "ok"),
                ("check.b", STATUS_FAIL, "broken"),
            ],
        )
        assert pf.main() == 1
