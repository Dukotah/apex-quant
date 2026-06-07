"""Concrete strategy implementations + a name→class registry.

The registry makes research strategies **selectable by name** — for backtests,
the Gauntlet, and comparison tooling — without deploying them. Per the project
ethos (``CLAUDE.md`` rule 17, ``BACKLOG`` F8/F17), being in this registry means
only *"this strategy exists and is constructible by name"* — never *"validated"*
or *"deployed"*. Nothing here is added to the live roster
(``scripts/run_once.py``) until it clears all seven Gauntlet gates on real data
and proves genuinely uncorrelated to the deployed trend sleeve.

Canonical names are the module stems (snake_case) — the same identifiers used by
config, CLI flags, and report output, so a strategy has exactly one name across
the whole system.
"""

from __future__ import annotations

from typing import Dict, List, Type

from apex.core.models import Symbol
from apex.strategy.base_strategy import BaseStrategy
from apex.strategy.library.atr_channel_breakout import ATRChannelBreakoutStrategy
from apex.strategy.library.bollinger_breakout import BollingerBreakoutStrategy
from apex.strategy.library.connors_rsi_strategy import ConnorsRSIStrategy
from apex.strategy.library.donchian_breakout import DonchianBreakoutStrategy
from apex.strategy.library.keltner_trend import KeltnerTrendStrategy
from apex.strategy.library.macd_trend import MacdTrendStrategy
from apex.strategy.library.mean_reversion_zscore import MeanReversionZScoreStrategy
from apex.strategy.library.roc_momentum import ROCMomentumStrategy
from apex.strategy.library.stochastic_reversal import StochasticReversalStrategy
from apex.strategy.library.ts_momentum_blend import TimeSeriesMomentumBlend
from apex.strategy.library.volatility_breakout import VolatilityBreakoutStrategy

# Canonical name -> strategy class. Research candidates only: every entry here is
# UNVALIDATED until it clears the Gauntlet. The deployed roster lives elsewhere.
STRATEGY_REGISTRY: Dict[str, Type[BaseStrategy]] = {
    "atr_channel_breakout": ATRChannelBreakoutStrategy,
    "bollinger_breakout": BollingerBreakoutStrategy,
    "connors_rsi_strategy": ConnorsRSIStrategy,
    "donchian_breakout": DonchianBreakoutStrategy,
    "keltner_trend": KeltnerTrendStrategy,
    "macd_trend": MacdTrendStrategy,
    "mean_reversion_zscore": MeanReversionZScoreStrategy,
    "roc_momentum": ROCMomentumStrategy,
    "stochastic_reversal": StochasticReversalStrategy,
    "ts_momentum_blend": TimeSeriesMomentumBlend,
    "volatility_breakout": VolatilityBreakoutStrategy,
}


def available_strategies() -> List[str]:
    """Sorted list of registered (research) strategy names."""
    return sorted(STRATEGY_REGISTRY)


def get_strategy_class(name: str) -> Type[BaseStrategy]:
    """Look up a registered strategy class by canonical name.

    Raises ``ValueError`` (listing the valid choices) on an unknown name, so
    callers fail closed and loud rather than silently no-op.
    """
    try:
        return STRATEGY_REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown strategy {name!r}; choose one of {', '.join(available_strategies())}"
        ) from None


def build_strategy(name: str, symbols: List[Symbol], **params: object) -> BaseStrategy:
    """Instantiate a registered strategy by name over ``symbols``.

    ``name`` doubles as the instance's ``strategy_id``. Extra keyword arguments
    pass straight through to the constructor, so tuned parameters (lookbacks,
    thresholds, ATR multiples, …) can be supplied here. Construct the class
    directly if you need a ``strategy_id`` distinct from the registry name.
    """
    cls = get_strategy_class(name)
    return cls(name, symbols, **params)  # type: ignore[arg-type]


__all__ = [
    "STRATEGY_REGISTRY",
    "available_strategies",
    "get_strategy_class",
    "build_strategy",
    "ATRChannelBreakoutStrategy",
    "BollingerBreakoutStrategy",
    "ConnorsRSIStrategy",
    "DonchianBreakoutStrategy",
    "KeltnerTrendStrategy",
    "MacdTrendStrategy",
    "MeanReversionZScoreStrategy",
    "ROCMomentumStrategy",
    "StochasticReversalStrategy",
    "TimeSeriesMomentumBlend",
    "VolatilityBreakoutStrategy",
]
