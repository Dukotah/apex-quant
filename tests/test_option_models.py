"""
Tests for apex.core.option.

Covers the load-bearing OCC-21 round-trip (the canonical, reversible contract
identity strategy code keys on), __post_init__ validation for every model, and
the multi-leg OptionOrder invariants. All offline + deterministic.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Symbol
from apex.core.option import (
    OptionContract,
    OptionGreeks,
    OptionLeg,
    OptionOrder,
    OptionQuote,
    OptionRight,
    OptionType,
)

UTC = timezone.utc
SPY = Symbol("SPY", AssetClass.ETF, contract_multiplier=Decimal("100"))


def _contract(strike="450", otype=OptionType.CALL, expiry=date(2024, 9, 20)):
    return OptionContract(
        underlying=SPY,
        expiry=expiry,
        strike=Decimal(strike),
        option_type=otype,
    )


# ----------------------------------------------------------------- OCC symbol


def test_occ_symbol_known_value():
    # The canonical example from the spec.
    assert _contract().occ_symbol == "SPY   240920C00450000"


def test_occ_symbol_put():
    c = _contract(otype=OptionType.PUT)
    assert c.occ_symbol == "SPY   240920P00450000"
    assert len(c.occ_symbol) == 21


@pytest.mark.parametrize(
    "strike,otype",
    [
        ("450", OptionType.CALL),
        ("450", OptionType.PUT),
        ("7.5", OptionType.CALL),
        ("0.5", OptionType.PUT),
        ("1234.567", OptionType.CALL),
        ("99999.999", OptionType.PUT),
    ],
)
def test_occ_round_trip(strike, otype):
    original = _contract(strike=strike, otype=otype)
    occ = original.occ_symbol
    assert len(occ) == 21
    parsed = OptionContract.parse_occ(occ, contract_multiplier=Decimal("100"))
    assert parsed.underlying.ticker == "SPY"
    assert parsed.expiry == original.expiry
    assert parsed.strike == original.strike
    assert parsed.option_type == original.option_type
    # And the parsed contract re-encodes to the identical string (true round-trip).
    assert parsed.occ_symbol == occ


def test_parse_occ_rejects_garbage():
    with pytest.raises(ValueError):
        OptionContract.parse_occ("not-an-occ-symbol")


def test_parse_occ_short_root():
    # A 1-char root left-justified to 6 chars round-trips.
    c = OptionContract(
        Symbol("F", AssetClass.EQUITY), date(2025, 1, 17), Decimal("12"), OptionType.CALL
    )
    assert OptionContract.parse_occ(c.occ_symbol).underlying.ticker == "F"


# ----------------------------------------------------------------- OptionContract validation


def test_contract_rejects_zero_strike():
    with pytest.raises(ValueError):
        _contract(strike="0")


def test_contract_rejects_negative_strike():
    with pytest.raises(ValueError):
        _contract(strike="-5")


def test_contract_rejects_non_decimal_strike():
    with pytest.raises(TypeError):
        OptionContract(SPY, date(2024, 9, 20), 450, OptionType.CALL)  # float, not Decimal


def test_contract_rejects_insane_expiry():
    with pytest.raises(ValueError):
        _contract(expiry=date(1990, 1, 1))


def test_contract_rejects_long_root():
    with pytest.raises(ValueError):
        OptionContract(
            Symbol("TOOLONG", AssetClass.EQUITY), date(2024, 9, 20), Decimal("1"), OptionType.CALL
        )


def test_contract_rejects_overfine_strike():
    with pytest.raises(ValueError):
        _contract(strike="450.0001")  # finer than $0.001


def test_contract_rejects_oversized_strike():
    with pytest.raises(ValueError):
        _contract(strike="100000")  # overflows 8-digit OCC field


def test_contract_is_frozen():
    c = _contract()
    with pytest.raises(Exception):
        c.strike = Decimal("1")  # type: ignore[misc]


# ----------------------------------------------------------------- OptionQuote


def _quote(**kw):
    defaults = dict(
        contract=_contract(),
        bid=Decimal("5.00"),
        ask=Decimal("5.20"),
        last=Decimal("5.10"),
        timestamp=datetime(2024, 9, 1, 15, 0, tzinfo=UTC),
    )
    defaults.update(kw)
    return OptionQuote(**defaults)


def test_quote_mid():
    assert _quote().mid == Decimal("5.10")


def test_quote_greeks_optional():
    assert _quote().greeks is None
    g = OptionGreeks(delta=0.5, gamma=0.1, theta=-0.02, vega=0.3, implied_vol=0.25)
    assert _quote(greeks=g).greeks.delta == 0.5


def test_quote_rejects_naive_timestamp():
    with pytest.raises(ValueError):
        _quote(timestamp=datetime(2024, 9, 1, 15, 0))


def test_quote_rejects_crossed():
    with pytest.raises(ValueError):
        _quote(bid=Decimal("5.30"), ask=Decimal("5.00"))


def test_quote_rejects_negative_price():
    with pytest.raises(ValueError):
        _quote(bid=Decimal("-1"))


# ----------------------------------------------------------------- OptionLeg / OptionOrder


def test_leg_defaults_ratio_one():
    leg = OptionLeg(_contract(), OptionRight.BUY)
    assert leg.ratio == 1


def test_leg_rejects_bad_ratio():
    with pytest.raises(ValueError):
        OptionLeg(_contract(), OptionRight.BUY, ratio=0)


def test_single_leg_order():
    order = OptionOrder(legs=(OptionLeg(_contract(), OptionRight.BUY),), quantity=2)
    assert not order.is_multi_leg
    assert order.quantity == 2
    assert order.limit_price is None


def test_vertical_spread_order():
    long_leg = OptionLeg(_contract(strike="450"), OptionRight.BUY)
    short_leg = OptionLeg(_contract(strike="455"), OptionRight.SELL)
    order = OptionOrder(legs=(long_leg, short_leg), quantity=1, limit_price=Decimal("2.50"))
    assert order.is_multi_leg
    assert len(order.legs) == 2


def test_order_rejects_empty_legs():
    with pytest.raises(ValueError):
        OptionOrder(legs=(), quantity=1)


def test_order_rejects_nonpositive_quantity():
    with pytest.raises(ValueError):
        OptionOrder(legs=(OptionLeg(_contract(), OptionRight.BUY),), quantity=0)


def test_order_rejects_mixed_underlyings():
    spy_leg = OptionLeg(_contract(), OptionRight.BUY)
    qqq = Symbol("QQQ", AssetClass.ETF)
    qqq_leg = OptionLeg(
        OptionContract(qqq, date(2024, 9, 20), Decimal("400"), OptionType.CALL),
        OptionRight.SELL,
    )
    with pytest.raises(ValueError):
        OptionOrder(legs=(spy_leg, qqq_leg), quantity=1)


def test_order_rejects_non_tuple_legs():
    with pytest.raises(TypeError):
        OptionOrder(legs=[OptionLeg(_contract(), OptionRight.BUY)], quantity=1)  # type: ignore[arg-type]
