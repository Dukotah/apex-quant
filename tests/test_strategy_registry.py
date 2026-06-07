"""Tests for the strategy registry in ``apex.strategy.library`` (F1/F12 wiring).

The registry's contract: every research candidate is selectable by a canonical
name, resolves to a ``BaseStrategy`` subclass, and is constructible over a
universe using only ``(name, symbols)`` — proving the shared constructor
convention holds. Being registered says nothing about validation; these tests
assert *wiring*, not edge.
"""

from __future__ import annotations

import pytest

from apex.core.models import AssetClass, Symbol
from apex.strategy.base_strategy import BaseStrategy
from apex.strategy.library import (
    STRATEGY_REGISTRY,
    TimeSeriesMomentumBlend,
    available_strategies,
    build_strategy,
    get_strategy_class,
)

UNIVERSE = [
    Symbol("SPY", AssetClass.ETF),
    Symbol("QQQ", AssetClass.ETF),
]

EXPECTED_NAMES = {
    "atr_channel_breakout",
    "bollinger_breakout",
    "connors_rsi_strategy",
    "donchian_breakout",
    "keltner_trend",
    "macd_trend",
    "mean_reversion_zscore",
    "roc_momentum",
    "stochastic_reversal",
    "ts_momentum_blend",
    "volatility_breakout",
}


def test_registry_holds_expected_names():
    assert set(STRATEGY_REGISTRY) == EXPECTED_NAMES


def test_available_strategies_is_sorted_and_complete():
    names = available_strategies()
    assert names == sorted(EXPECTED_NAMES)
    assert names == sorted(names)  # explicitly sorted


def test_ts_momentum_blend_is_registered():
    # The F1 headline: TimeSeriesMomentumBlend is selectable by name.
    assert get_strategy_class("ts_momentum_blend") is TimeSeriesMomentumBlend


@pytest.mark.parametrize("name", sorted(EXPECTED_NAMES))
def test_every_class_subclasses_base_strategy(name):
    assert issubclass(get_strategy_class(name), BaseStrategy)


@pytest.mark.parametrize("name", sorted(EXPECTED_NAMES))
def test_every_strategy_builds_by_name(name):
    strat = build_strategy(name, UNIVERSE)
    assert isinstance(strat, BaseStrategy)
    # name doubles as the instance id, and the universe round-trips.
    assert strat.strategy_id == name
    assert {s.ticker for s in strat.symbols} == {"SPY", "QQQ"}


def test_build_strategy_passes_through_params():
    # Tuned params reach the constructor (ts_momentum_blend exposes lookbacks).
    strat = build_strategy("ts_momentum_blend", UNIVERSE, lookbacks=[10, 20])
    assert isinstance(strat, TimeSeriesMomentumBlend)
    assert strat.lookbacks == [10, 20]


def test_unknown_name_fails_closed_with_choices():
    with pytest.raises(ValueError) as exc:
        get_strategy_class("does_not_exist")
    msg = str(exc.value)
    assert "does_not_exist" in msg
    # The error lists valid choices so the caller can recover.
    assert "ts_momentum_blend" in msg


def test_build_unknown_name_also_raises():
    with pytest.raises(ValueError):
        build_strategy("nope", UNIVERSE)
