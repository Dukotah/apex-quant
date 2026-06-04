"""
apex.core.config
================
Central configuration. The single source of truth for runtime behavior.

The MODE flag here is THE switch the spec calls for: change this one value
and the execution factory routes orders to paper or live, with zero changes
to any strategy, risk, or data code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional

from apex.risk.risk_manager import RiskConfig


class ExecutionMode(str, Enum):
    BACKTEST = "backtest"   # historical replay + simulated fills
    PAPER = "paper"         # live data + simulated fills (no real money)
    LIVE = "live"           # live data + real broker (REAL MONEY)


class Broker(str, Enum):
    SIMULATED = "simulated"
    ALPACA = "alpaca"
    IBKR = "ibkr"


@dataclass(frozen=True)
class AppConfig:
    """
    Immutable application config. Build once at startup from env/yaml.

    THE SWITCH: `mode` + `broker` together decide which execution engine runs.
    Everything downstream is mode-agnostic.
    """
    mode: ExecutionMode = ExecutionMode.BACKTEST
    broker: Broker = Broker.SIMULATED

    initial_capital: Decimal = Decimal("100000")

    # Simulated-fill assumptions (used in backtest + paper).
    slippage_pct: Decimal = Decimal("0.001")        # 0.1%
    commission_per_share: Decimal = Decimal("0")    # Alpaca is commission-free

    # Risk config is nested here so it's loaded immutably at startup.
    risk: RiskConfig = field(default_factory=RiskConfig)

    # Broker credentials are read from env, never hardcoded.
    alpaca_key: Optional[str] = None
    alpaca_secret: Optional[str] = None

    @staticmethod
    def from_env() -> "AppConfig":
        """Load config from environment variables with safe defaults."""
        mode = ExecutionMode(os.getenv("APEX_MODE", "backtest"))
        broker = Broker(os.getenv("APEX_BROKER", "simulated"))

        # SAFETY: live mode must be explicit and paired with a real broker.
        if mode == ExecutionMode.LIVE and broker == Broker.SIMULATED:
            raise ValueError("LIVE mode requires a real broker, not 'simulated'")

        return AppConfig(
            mode=mode,
            broker=broker,
            initial_capital=Decimal(os.getenv("APEX_CAPITAL", "100000")),
            alpaca_key=os.getenv("ALPACA_API_KEY"),
            alpaca_secret=os.getenv("ALPACA_SECRET_KEY"),
        )
