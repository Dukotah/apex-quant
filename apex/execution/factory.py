"""
apex.execution.factory
======================
The ONE place the paper/live decision is made.

`make_execution_engine(config)` reads the immutable AppConfig's mode + broker
and returns the matching execution engine. Strategy, risk, and data code never
import a concrete engine — they only ever see BaseExecutionEngine through this
factory, so flipping APEX_MODE from paper to live changes nothing downstream.

  mode=backtest                     → SimulatedExecutionEngine
  mode=paper,  broker=simulated     → SimulatedExecutionEngine
  mode=paper,  broker=alpaca        → (Alpaca engine — not built yet)
  mode=live,   broker=alpaca/ibkr   → (live engine — not built yet)
  mode=live,   broker=simulated     → refused upstream by AppConfig.from_env()
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from apex.core.config import AppConfig, Broker, ExecutionMode
from apex.core.events import FillEvent
from apex.execution.base_execution import BaseExecutionEngine
from apex.execution.simulated import SimulatedExecutionEngine

logger = logging.getLogger(__name__)


def make_execution_engine(
    config: AppConfig,
    on_fill: Optional[Callable[[FillEvent], None]] = None,
) -> BaseExecutionEngine:
    """
    Return the execution engine selected by `config`. Fails closed: an
    unsupported / not-yet-built combination raises NotImplementedError rather
    than silently falling back to the simulator in a live context.
    """
    # Backtest always uses the simulator regardless of broker.
    if config.mode == ExecutionMode.BACKTEST:
        logger.info("Execution: SimulatedExecutionEngine (backtest mode).")
        return SimulatedExecutionEngine(
            slippage_pct=config.slippage_pct,
            commission_per_share=config.commission_per_share,
            on_fill=on_fill,
        )

    if config.mode == ExecutionMode.PAPER:
        if config.broker == Broker.SIMULATED:
            logger.info("Execution: SimulatedExecutionEngine (paper mode, simulated broker).")
            return SimulatedExecutionEngine(
                slippage_pct=config.slippage_pct,
                commission_per_share=config.commission_per_share,
                on_fill=on_fill,
            )
        raise NotImplementedError(
            f"Paper execution against broker '{config.broker.value}' is not built yet. "
            "Use APEX_BROKER=simulated for now (the Alpaca engine is a future module)."
        )

    if config.mode == ExecutionMode.LIVE:
        raise NotImplementedError(
            "Live execution is not built yet. The Alpaca/IBKR engine and its "
            "reconciliation/idempotency safeguards must land before live trading."
        )

    raise NotImplementedError(f"No execution engine for mode '{config.mode}'.")
