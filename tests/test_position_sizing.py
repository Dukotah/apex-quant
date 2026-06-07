"""
Tests for apex.risk.position_sizing — advisory position-size calculators.

All expected values are hand-computed. Money/quantity math is Decimal.
"""
from __future__ import annotations

from decimal import Decimal

from apex.core.models import AssetClass, Symbol
from apex.risk.position_sizing import (
    atr_risk_size,
    fixed_fractional_size,
    kelly_fraction,
    round_quantity,
    volatility_target_size,
)

# A whole-unit equity (default fractionable=False) and a fractional one.
STOCK = Symbol(ticker="AAPL", asset_class=AssetClass.EQUITY)
CRYPTO = Symbol(ticker="BTC/USD", asset_class=AssetClass.CRYPTO, fractionable=True)
FUTURE = Symbol(
    ticker="ESZ4",
    asset_class=AssetClass.FUTURE,
    contract_multiplier=Decimal("50"),
)


# --------------------------------------------------------------------------
# round_quantity
# --------------------------------------------------------------------------

def test_round_quantity_floors_whole_units():
    assert round_quantity(Decimal("3.99"), STOCK) == Decimal("3")


def test_round_quantity_quantizes_fractionable():
    assert round_quantity(Decimal("1.234567"), CRYPTO) == Decimal("1.2346")


def test_round_quantity_zero_and_negative_fail_closed():
    assert round_quantity(Decimal("0"), STOCK) == Decimal("0")
    assert round_quantity(Decimal("-5"), STOCK) == Decimal("0")


# --------------------------------------------------------------------------
# fixed_fractional_size
# --------------------------------------------------------------------------

def test_fixed_fractional_known_value():
    # equity 100_000, risk 1% = $1000. entry 100, stop 95 -> per-unit risk $5.
    # raw_qty = 1000 / 5 = 200 shares.
    qty = fixed_fractional_size(
        equity=Decimal("100000"),
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        symbol=STOCK,
        risk_fraction=Decimal("0.01"),
    )
    assert qty == Decimal("200")


def test_fixed_fractional_uses_abs_distance_for_short():
    # stop above entry (short setup): |100 - 105| = 5 -> same 200 shares.
    qty = fixed_fractional_size(
        equity=Decimal("100000"),
        entry_price=Decimal("100"),
        stop_price=Decimal("105"),
        symbol=STOCK,
        risk_fraction=Decimal("0.01"),
    )
    assert qty == Decimal("200")


def test_fixed_fractional_respects_contract_multiplier():
    # ES multiplier 50. risk $1000, distance 10 pts -> per-unit risk = 10*50=500.
    # raw_qty = 1000 / 500 = 2 contracts.
    qty = fixed_fractional_size(
        equity=Decimal("100000"),
        entry_price=Decimal("5000"),
        stop_price=Decimal("4990"),
        symbol=FUTURE,
        risk_fraction=Decimal("0.01"),
    )
    assert qty == Decimal("2")


def test_fixed_fractional_fractionable_quantizes():
    # equity 50_000, 1% = 500; entry 30000 stop 29000 distance 1000 -> 0.5 BTC.
    qty = fixed_fractional_size(
        equity=Decimal("50000"),
        entry_price=Decimal("30000"),
        stop_price=Decimal("29000"),
        symbol=CRYPTO,
        risk_fraction=Decimal("0.01"),
    )
    assert qty == Decimal("0.5000")


def test_fixed_fractional_accepts_floats_and_ints():
    qty = fixed_fractional_size(
        equity=100000,
        entry_price=100.0,
        stop_price=95.0,
        symbol=STOCK,
        risk_fraction=0.01,
    )
    assert qty == Decimal("200")


def test_fixed_fractional_zero_distance_fails_closed():
    assert fixed_fractional_size(
        equity=Decimal("100000"),
        entry_price=Decimal("100"),
        stop_price=Decimal("100"),
        symbol=STOCK,
    ) == Decimal("0")


def test_fixed_fractional_bad_inputs_fail_closed():
    base = dict(entry_price=Decimal("100"), stop_price=Decimal("95"), symbol=STOCK)
    assert fixed_fractional_size(equity=Decimal("0"), **base) == Decimal("0")
    assert fixed_fractional_size(equity=Decimal("-1"), **base) == Decimal("0")
    assert fixed_fractional_size(equity=Decimal("100000"), entry_price=Decimal("0"),
                                 stop_price=Decimal("95"), symbol=STOCK) == Decimal("0")
    # risk fraction out of (0,1]
    assert fixed_fractional_size(equity=Decimal("100000"), entry_price=Decimal("100"),
                                 stop_price=Decimal("95"), symbol=STOCK,
                                 risk_fraction=Decimal("0")) == Decimal("0")
    assert fixed_fractional_size(equity=Decimal("100000"), entry_price=Decimal("100"),
                                 stop_price=Decimal("95"), symbol=STOCK,
                                 risk_fraction=Decimal("1.5")) == Decimal("0")
    assert fixed_fractional_size(equity=None, **base) == Decimal("0")


# --------------------------------------------------------------------------
# atr_risk_size
# --------------------------------------------------------------------------

def test_atr_risk_known_value():
    # risk $1000; ATR 2.5 * multiple 2 = stop distance 5 -> per-unit risk $5.
    # raw_qty = 1000 / 5 = 200.
    qty = atr_risk_size(
        equity=Decimal("100000"),
        entry_price=Decimal("100"),
        atr=Decimal("2.5"),
        symbol=STOCK,
        risk_fraction=Decimal("0.01"),
        atr_multiple=Decimal("2"),
    )
    assert qty == Decimal("200")


def test_atr_risk_respects_multiplier():
    # ES multiplier 50; ATR 5 * 2 = 10 pts; per-unit risk 10*50 = 500.
    # risk $1000 -> 2 contracts.
    qty = atr_risk_size(
        equity=Decimal("100000"),
        entry_price=Decimal("5000"),
        atr=Decimal("5"),
        symbol=FUTURE,
        risk_fraction=Decimal("0.01"),
        atr_multiple=Decimal("2"),
    )
    assert qty == Decimal("2")


def test_atr_risk_accepts_float_atr():
    qty = atr_risk_size(
        equity=100000,
        entry_price=100,
        atr=2.5,
        symbol=STOCK,
        risk_fraction=0.01,
        atr_multiple=2,
    )
    assert qty == Decimal("200")


def test_atr_risk_bad_inputs_fail_closed():
    assert atr_risk_size(equity=Decimal("100000"), entry_price=Decimal("100"),
                         atr=Decimal("0"), symbol=STOCK) == Decimal("0")
    assert atr_risk_size(equity=Decimal("100000"), entry_price=Decimal("100"),
                         atr=Decimal("-1"), symbol=STOCK) == Decimal("0")
    assert atr_risk_size(equity=Decimal("100000"), entry_price=Decimal("100"),
                         atr=Decimal("2"), symbol=STOCK,
                         atr_multiple=Decimal("0")) == Decimal("0")
    assert atr_risk_size(equity=Decimal("0"), entry_price=Decimal("100"),
                         atr=Decimal("2"), symbol=STOCK) == Decimal("0")


# --------------------------------------------------------------------------
# volatility_target_size
# --------------------------------------------------------------------------

def test_volatility_target_known_value():
    # target 10% / instrument 20% = weight 0.5; equity 100k -> $50k notional.
    # entry 100 -> 500 shares.
    qty = volatility_target_size(
        equity=Decimal("100000"),
        entry_price=Decimal("100"),
        instrument_volatility=Decimal("0.20"),
        symbol=STOCK,
        target_volatility=Decimal("0.10"),
    )
    assert qty == Decimal("500")


def test_volatility_target_clamped_by_max_fraction():
    # target 10% / instrument 5% = weight 2.0, but capped at max_fraction 1.0.
    # notional = full equity 100k; entry 100 -> 1000 shares.
    qty = volatility_target_size(
        equity=Decimal("100000"),
        entry_price=Decimal("100"),
        instrument_volatility=Decimal("0.05"),
        symbol=STOCK,
        target_volatility=Decimal("0.10"),
        max_fraction=Decimal("1.0"),
    )
    assert qty == Decimal("1000")


def test_volatility_target_higher_vol_smaller_size():
    low_vol = volatility_target_size(
        equity=Decimal("100000"), entry_price=Decimal("100"),
        instrument_volatility=Decimal("0.10"), symbol=STOCK,
        target_volatility=Decimal("0.10"))
    high_vol = volatility_target_size(
        equity=Decimal("100000"), entry_price=Decimal("100"),
        instrument_volatility=Decimal("0.40"), symbol=STOCK,
        target_volatility=Decimal("0.10"))
    assert high_vol < low_vol


def test_volatility_target_respects_multiplier_and_fractionable():
    # CRYPTO: target 10%/inst 50% = weight 0.2; equity 50k -> 10k notional;
    # entry 30000 -> 0.3333 BTC (quantized).
    qty = volatility_target_size(
        equity=Decimal("50000"),
        entry_price=Decimal("30000"),
        instrument_volatility=Decimal("0.50"),
        symbol=CRYPTO,
        target_volatility=Decimal("0.10"),
    )
    assert qty == Decimal("0.3333")


def test_volatility_target_bad_inputs_fail_closed():
    assert volatility_target_size(equity=Decimal("100000"), entry_price=Decimal("100"),
                                  instrument_volatility=Decimal("0"), symbol=STOCK) == Decimal("0")
    assert volatility_target_size(equity=Decimal("0"), entry_price=Decimal("100"),
                                  instrument_volatility=Decimal("0.2"), symbol=STOCK) == Decimal("0")
    assert volatility_target_size(equity=Decimal("100000"), entry_price=Decimal("0"),
                                  instrument_volatility=Decimal("0.2"), symbol=STOCK) == Decimal("0")


# --------------------------------------------------------------------------
# kelly_fraction
# --------------------------------------------------------------------------

def test_kelly_known_value():
    # p=0.6, b=2 -> 0.6 - 0.4/2 = 0.6 - 0.2 = 0.4
    assert kelly_fraction(Decimal("0.6"), Decimal("2")) == Decimal("0.4")


def test_kelly_negative_edge_returns_zero():
    # p=0.4, b=1 -> 0.4 - 0.6 = -0.2 -> clamped to 0
    assert kelly_fraction(Decimal("0.4"), Decimal("1")) == Decimal("0")


def test_kelly_capped():
    # p=0.9, b=10 -> 0.9 - 0.1/10 = 0.89, capped at 0.25.
    assert kelly_fraction(Decimal("0.9"), Decimal("10"), cap=Decimal("0.25")) == Decimal("0.25")


def test_kelly_bad_inputs_fail_closed():
    assert kelly_fraction(Decimal("0"), Decimal("2")) == Decimal("0")
    assert kelly_fraction(Decimal("1.5"), Decimal("2")) == Decimal("0")
    assert kelly_fraction(Decimal("0.6"), Decimal("0")) == Decimal("0")
    assert kelly_fraction(None, Decimal("2")) == Decimal("0")
