"""
Tests for apex.core.config — AppConfig, ExecutionMode, Broker.

Covers:
  - Enum values (ExecutionMode, Broker)
  - AppConfig default construction
  - AppConfig field types and immutability (frozen dataclass)
  - AppConfig.from_env() with various env-var combinations
  - Safety invariant: LIVE + SIMULATED raises ValueError
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.core.config import AppConfig, Broker, ExecutionMode
from apex.risk.risk_manager import RiskConfig

# ---- ExecutionMode enum ----


def test_execution_mode_values():
    assert ExecutionMode.BACKTEST.value == "backtest"
    assert ExecutionMode.PAPER.value == "paper"
    assert ExecutionMode.LIVE.value == "live"


def test_execution_mode_from_string():
    # The enum is a str-mixin so it round-trips cleanly.
    assert ExecutionMode("backtest") is ExecutionMode.BACKTEST
    assert ExecutionMode("paper") is ExecutionMode.PAPER
    assert ExecutionMode("live") is ExecutionMode.LIVE


def test_execution_mode_is_str():
    # ExecutionMode(str, Enum) → instances compare equal to plain strings.
    assert ExecutionMode.BACKTEST == "backtest"
    assert ExecutionMode.PAPER == "paper"
    assert ExecutionMode.LIVE == "live"


# ---- Broker enum ----


def test_broker_values():
    assert Broker.SIMULATED.value == "simulated"
    assert Broker.ALPACA.value == "alpaca"
    assert Broker.IBKR.value == "ibkr"


def test_broker_from_string():
    assert Broker("simulated") is Broker.SIMULATED
    assert Broker("alpaca") is Broker.ALPACA
    assert Broker("ibkr") is Broker.IBKR


def test_broker_is_str():
    assert Broker.SIMULATED == "simulated"
    assert Broker.ALPACA == "alpaca"


# ---- AppConfig default construction ----


def test_appconfig_defaults():
    cfg = AppConfig()
    assert cfg.mode is ExecutionMode.BACKTEST
    assert cfg.broker is Broker.SIMULATED
    assert cfg.initial_capital == Decimal("100000")
    assert cfg.slippage_pct == Decimal("0.001")
    assert cfg.commission_per_share == Decimal("0")
    assert isinstance(cfg.risk, RiskConfig)
    assert cfg.alpaca_key is None
    assert cfg.alpaca_secret is None


def test_appconfig_is_frozen():
    cfg = AppConfig()
    with pytest.raises((AttributeError, TypeError)):
        cfg.mode = ExecutionMode.PAPER  # type: ignore[misc]


def test_appconfig_custom_construction():
    cfg = AppConfig(
        mode=ExecutionMode.PAPER,
        broker=Broker.ALPACA,
        initial_capital=Decimal("250000"),
        slippage_pct=Decimal("0.002"),
        commission_per_share=Decimal("0.005"),
        alpaca_key="key123",
        alpaca_secret="sec456",
    )
    assert cfg.mode is ExecutionMode.PAPER
    assert cfg.broker is Broker.ALPACA
    assert cfg.initial_capital == Decimal("250000")
    assert cfg.slippage_pct == Decimal("0.002")
    assert cfg.commission_per_share == Decimal("0.005")
    assert cfg.alpaca_key == "key123"
    assert cfg.alpaca_secret == "sec456"


def test_appconfig_nested_risk_default():
    cfg = AppConfig()
    # RiskConfig's own defaults must be present.
    assert cfg.risk.max_drawdown_pct == Decimal("0.10")
    assert cfg.risk.require_stop_loss is True


def test_appconfig_nested_risk_custom():
    custom_risk = RiskConfig(max_drawdown_pct=Decimal("0.20"), require_stop_loss=False)
    cfg = AppConfig(risk=custom_risk)
    assert cfg.risk.max_drawdown_pct == Decimal("0.20")
    assert cfg.risk.require_stop_loss is False


# ---- AppConfig.from_env() ----


def _clean_env(monkeypatch):
    """Remove Apex env vars so tests start from a clean slate."""
    for key in ("APEX_MODE", "APEX_BROKER", "APEX_CAPITAL", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)


def test_from_env_defaults(monkeypatch):
    _clean_env(monkeypatch)
    cfg = AppConfig.from_env()
    assert cfg.mode is ExecutionMode.BACKTEST
    assert cfg.broker is Broker.SIMULATED
    assert cfg.initial_capital == Decimal("100000")
    assert cfg.alpaca_key is None
    assert cfg.alpaca_secret is None


def test_from_env_paper_mode(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("APEX_MODE", "paper")
    monkeypatch.setenv("APEX_BROKER", "alpaca")
    cfg = AppConfig.from_env()
    assert cfg.mode is ExecutionMode.PAPER
    assert cfg.broker is Broker.ALPACA


def test_from_env_custom_capital(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("APEX_CAPITAL", "50000")
    cfg = AppConfig.from_env()
    assert cfg.initial_capital == Decimal("50000")


def test_from_env_alpaca_credentials(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ALPACA_API_KEY", "MYKEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "MYSECRET")
    cfg = AppConfig.from_env()
    assert cfg.alpaca_key == "MYKEY"
    assert cfg.alpaca_secret == "MYSECRET"


def test_from_env_live_simulated_raises(monkeypatch):
    """LIVE mode with the SIMULATED broker must raise — the core safety invariant."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("APEX_MODE", "live")
    monkeypatch.setenv("APEX_BROKER", "simulated")
    with pytest.raises(ValueError, match="real broker"):
        AppConfig.from_env()


def test_from_env_live_real_broker_ok(monkeypatch):
    """LIVE mode + Alpaca broker is the only valid live combination."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("APEX_MODE", "live")
    monkeypatch.setenv("APEX_BROKER", "alpaca")
    cfg = AppConfig.from_env()
    assert cfg.mode is ExecutionMode.LIVE
    assert cfg.broker is Broker.ALPACA


def test_from_env_ibkr_broker(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("APEX_BROKER", "ibkr")
    cfg = AppConfig.from_env()
    assert cfg.broker is Broker.IBKR


def test_from_env_backtest_capital_large(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("APEX_CAPITAL", "1000000")
    cfg = AppConfig.from_env()
    assert cfg.initial_capital == Decimal("1000000")
