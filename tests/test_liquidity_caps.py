"""Tests for apex.risk.liquidity_caps (advisory liquidity sizing)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Symbol
from apex.risk.liquidity_caps import (
    LiquidityCapConfig,
    apply_liquidity_cap,
    average_daily_volume,
    liquidity_cap_quantity,
)

D = Decimal


def _equity(fractionable: bool = False) -> Symbol:
    return Symbol(
        ticker="AAPL",
        asset_class=AssetClass.EQUITY,
        fractionable=fractionable,
    )


def _crypto() -> Symbol:
    return Symbol(
        ticker="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        fractionable=True,
    )


# --------------------------------------------------------------------------
# average_daily_volume
# --------------------------------------------------------------------------


def test_adv_simple_mean():
    vols = [D("100"), D("200"), D("300"), D("400"), D("500")]
    # mean of all 5 = 1500 / 5 = 300
    assert average_daily_volume(vols, window=20, min_observations=5) == D("300")


def test_adv_windowing_uses_only_recent():
    # window=3 => last three: 300, 400, 500 => mean 400
    vols = [D("100"), D("200"), D("300"), D("400"), D("500")]
    assert average_daily_volume(vols, window=3, min_observations=3) == D("400")


def test_adv_insufficient_data_returns_none():
    vols = [D("100"), D("200")]
    assert average_daily_volume(vols, window=20, min_observations=5) is None


def test_adv_empty_returns_none():
    assert average_daily_volume([], window=20, min_observations=5) is None


def test_adv_negative_volume_fails_closed():
    vols = [D("100"), D("-50"), D("200"), D("300"), D("400")]
    assert average_daily_volume(vols, window=20, min_observations=5) is None


def test_adv_bad_window_or_min_returns_none():
    vols = [D("100")] * 10
    assert average_daily_volume(vols, window=0, min_observations=5) is None
    assert average_daily_volume(vols, window=20, min_observations=0) is None


def test_adv_window_shorter_than_min_with_enough_total():
    # Plenty of data but window trims below min_observations -> None.
    vols = [D("100")] * 10
    assert average_daily_volume(vols, window=2, min_observations=5) is None


# --------------------------------------------------------------------------
# liquidity_cap_quantity
# --------------------------------------------------------------------------


def test_cap_basic_participation():
    vols = [D("1000")] * 20  # ADV = 1000
    cfg = LiquidityCapConfig(participation_pct=D("0.10"), adv_window=20, min_observations=5)
    # 1000 * 0.10 = 100
    assert liquidity_cap_quantity(vols, cfg) == D("100")


def test_cap_whole_units_for_non_fractionable():
    vols = [D("1000")] * 20  # ADV = 1000
    cfg = LiquidityCapConfig(participation_pct=D("0.0333"), adv_window=20, min_observations=5)
    # 1000 * 0.0333 = 33.3 -> floored to 33 whole shares
    assert liquidity_cap_quantity(vols, cfg, symbol=_equity()) == D("33")


def test_cap_fractional_kept_for_fractionable():
    vols = [D("1000")] * 20
    cfg = LiquidityCapConfig(participation_pct=D("0.0333"), adv_window=20, min_observations=5)
    assert liquidity_cap_quantity(vols, cfg, symbol=_crypto()) == D("33.3")


def test_cap_insufficient_data_fails_closed_to_zero():
    vols = [D("1000"), D("1000")]  # below min_observations
    assert liquidity_cap_quantity(vols) == D("0")


def test_cap_zero_adv_fails_closed():
    vols = [D("0")] * 20
    assert liquidity_cap_quantity(vols) == D("0")


def test_cap_invalid_participation_fails_closed():
    vols = [D("1000")] * 20
    assert liquidity_cap_quantity(vols, LiquidityCapConfig(participation_pct=D("0"))) == D("0")
    assert liquidity_cap_quantity(vols, LiquidityCapConfig(participation_pct=D("-0.1"))) == D("0")
    assert liquidity_cap_quantity(vols, LiquidityCapConfig(participation_pct=D("1.5"))) == D("0")


def test_cap_participation_of_one_allowed():
    vols = [D("1000")] * 20
    cfg = LiquidityCapConfig(participation_pct=D("1"), adv_window=20, min_observations=5)
    assert liquidity_cap_quantity(vols, cfg) == D("1000")


def test_cap_default_config_values():
    # Defaults: 10% participation, window 20, min 5.
    vols = [D("500")] * 30
    assert liquidity_cap_quantity(vols) == D("50")


# --------------------------------------------------------------------------
# apply_liquidity_cap
# --------------------------------------------------------------------------


def test_apply_shrinks_above_cap():
    vols = [D("1000")] * 20  # cap = 100
    assert apply_liquidity_cap(D("250"), vols) == D("100")


def test_apply_keeps_below_cap():
    vols = [D("1000")] * 20  # cap = 100
    assert apply_liquidity_cap(D("40"), vols) == D("40")


def test_apply_at_cap_boundary():
    vols = [D("1000")] * 20  # cap = 100
    assert apply_liquidity_cap(D("100"), vols) == D("100")


def test_apply_illiquid_data_fails_closed_to_zero():
    vols = [D("1000")]  # insufficient -> cap 0
    assert apply_liquidity_cap(D("50"), vols) == D("0")


def test_apply_non_positive_desired_is_zero():
    vols = [D("1000")] * 20
    assert apply_liquidity_cap(D("0"), vols) == D("0")
    assert apply_liquidity_cap(D("-10"), vols) == D("0")


def test_apply_respects_whole_units():
    vols = [D("1000")] * 20
    cfg = LiquidityCapConfig(participation_pct=D("0.0555"), adv_window=20, min_observations=5)
    # cap raw = 55.5 -> whole 55; desired 200 -> capped to 55
    assert apply_liquidity_cap(D("200"), vols, cfg, symbol=_equity()) == D("55")


def test_apply_determinism():
    vols = [D("123"), D("456"), D("789"), D("321"), D("654"), D("987")]
    a = apply_liquidity_cap(D("100"), vols)
    b = apply_liquidity_cap(D("100"), vols)
    assert a == b


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
