"""
tests/test_cost_model.py
========================
Hand-computed checks for the per-trade transaction cost model.

Run with:
    .venv/Scripts/python.exe -m pytest tests/test_cost_model.py -q
"""
from __future__ import annotations

import math

import pytest

from apex.validation.cost_model import (
    BPS_PER_UNIT,
    CostModel,
    apply_costs,
    net_trade_return,
)

# ---------------------------------------------------------------------------
# Constants / construction
# ---------------------------------------------------------------------------

def test_bps_constant() -> None:
    assert BPS_PER_UNIT == 10_000.0


def test_default_model_is_free() -> None:
    model = CostModel()
    assert model.cost_per_trade(10_000.0) == 0.0
    assert model.cost_fraction(10_000.0) == 0.0
    assert model.round_trip_cost_fraction(10_000.0) == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"commission_per_trade": -1.0},
        {"commission_rate": -0.0001},
        {"slippage_bps": -1.0},
        {"half_spread_bps": -0.5},
    ],
)
def test_negative_frictions_rejected(kwargs: dict) -> None:
    # Fail closed: a negative friction would be a phantom credit.
    with pytest.raises(ValueError):
        CostModel(**kwargs)


# ---------------------------------------------------------------------------
# Currency cost (cost_per_trade)
# ---------------------------------------------------------------------------

def test_cost_per_trade_hand_computed() -> None:
    # 5 bps commission rate, 10 bps slippage, 2 bps half-spread => 17 bps total
    # plus a $1 flat fee, on a $10,000 trade.
    model = CostModel(
        commission_per_trade=1.0,
        commission_rate=0.0005,   # 5 bps
        slippage_bps=10.0,        # 10 bps
        half_spread_bps=2.0,      # 2 bps
    )
    # variable fraction = 0.0005 + 0.0010 + 0.0002 = 0.0017
    # variable cost = 0.0017 * 10_000 = 17.0; plus flat 1.0 => 18.0
    assert model.cost_per_trade(10_000.0) == pytest.approx(18.0)


def test_cost_per_trade_uses_abs_notional() -> None:
    model = CostModel(commission_rate=0.001)
    assert model.cost_per_trade(-5_000.0) == pytest.approx(5.0)


def test_cost_per_trade_flat_only_independent_of_notional() -> None:
    model = CostModel(commission_per_trade=2.5)
    assert model.cost_per_trade(0.0) == pytest.approx(2.5)
    assert model.cost_per_trade(1_000_000.0) == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# Fractional cost (cost_fraction)
# ---------------------------------------------------------------------------

def test_cost_fraction_pure_bps_independent_of_notional() -> None:
    # No flat fee => fraction = sum of bps fractions, regardless of size.
    model = CostModel(slippage_bps=10.0, half_spread_bps=5.0)  # 15 bps = 0.0015
    assert model.cost_fraction(10_000.0) == pytest.approx(0.0015)
    assert model.cost_fraction(1.0) == pytest.approx(0.0015)


def test_cost_fraction_flat_fee_hurts_small_trades() -> None:
    model = CostModel(commission_per_trade=1.0)
    # On $100 the $1 fee is 1%; on $10,000 it is 0.01%.
    assert model.cost_fraction(100.0) == pytest.approx(0.01)
    assert model.cost_fraction(10_000.0) == pytest.approx(0.0001)


def test_cost_fraction_zero_notional_drops_only_flat_fee() -> None:
    # Flat fee can't be spread over zero notional, but the 10 bps slippage still
    # applies. Result is the bps fraction (0.0010), never NaN/inf from div-by-0.
    model = CostModel(commission_per_trade=1.0, slippage_bps=10.0)
    result = model.cost_fraction(0.0)
    assert result == pytest.approx(0.0010)
    assert not math.isnan(result)
    assert not math.isinf(result)


def test_cost_fraction_matches_cost_per_trade_ratio() -> None:
    model = CostModel(commission_per_trade=1.0, slippage_bps=7.0)
    notional = 25_000.0
    assert model.cost_fraction(notional) == pytest.approx(
        model.cost_per_trade(notional) / notional
    )


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_round_trip_is_double_one_way() -> None:
    model = CostModel(slippage_bps=10.0, half_spread_bps=5.0)  # 15 bps one way
    # Two half-spreads = full spread; round trip = 30 bps = 0.0030.
    assert model.round_trip_cost_fraction() == pytest.approx(0.0030)
    assert model.round_trip_cost_fraction(10_000.0) == pytest.approx(
        2.0 * model.cost_fraction(10_000.0)
    )


def test_round_trip_default_notional_zero() -> None:
    # Pure bps model: notional-independent, default arg works.
    model = CostModel(commission_rate=0.0002)
    assert model.round_trip_cost_fraction() == pytest.approx(0.0004)


# ---------------------------------------------------------------------------
# Net return adjustment
# ---------------------------------------------------------------------------

def test_net_trade_return_subtracts_round_trip() -> None:
    model = CostModel(slippage_bps=10.0, half_spread_bps=5.0)  # 15 bps one way
    # round trip = 30 bps = 0.0030
    assert net_trade_return(0.02, model) == pytest.approx(0.02 - 0.0030)


def test_net_trade_return_can_flip_negative() -> None:
    model = CostModel(slippage_bps=50.0, half_spread_bps=50.0)  # 100 bps one way
    # round trip = 200 bps = 0.02; a +1% gross trade becomes -1% net.
    assert net_trade_return(0.01, model) == pytest.approx(-0.01)


def test_net_trade_return_free_model_is_identity() -> None:
    assert net_trade_return(0.037, CostModel()) == pytest.approx(0.037)


# ---------------------------------------------------------------------------
# apply_costs (series)
# ---------------------------------------------------------------------------

def test_apply_costs_empty() -> None:
    assert apply_costs([], CostModel(slippage_bps=10.0)) == []


def test_apply_costs_bps_only() -> None:
    model = CostModel(slippage_bps=25.0)  # 25 bps one way => 50 bps round trip
    gross = [0.01, -0.005, 0.02]
    net = apply_costs(gross, model)
    assert net == pytest.approx([g - 0.0050 for g in gross])


def test_apply_costs_with_per_trade_notionals() -> None:
    model = CostModel(commission_per_trade=1.0)  # flat fee only
    gross = [0.02, 0.02]
    notionals = [100.0, 10_000.0]
    net = apply_costs(gross, model, notionals)
    # round trip flat cost fraction = 2 * (fee / notional)
    assert net[0] == pytest.approx(0.02 - 2.0 * (1.0 / 100.0))
    assert net[1] == pytest.approx(0.02 - 2.0 * (1.0 / 10_000.0))


def test_apply_costs_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        apply_costs([0.01, 0.02], CostModel(), [1000.0])


def test_apply_costs_none_notionals_assumes_zero() -> None:
    # Pure bps model: omitting notionals matches passing zeros.
    model = CostModel(half_spread_bps=10.0)
    gross = [0.01, 0.03]
    assert apply_costs(gross, model) == pytest.approx(
        apply_costs(gross, model, [0.0, 0.0])
    )
