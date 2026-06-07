"""Tests for apex.risk.leverage_metrics — gross/net leverage from positions."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Position, Symbol
from apex.risk.leverage_metrics import (
    LeverageSnapshot,
    compute_leverage,
    gross_exposure,
    gross_leverage,
    leverage_from_portfolio,
    long_exposure,
    net_exposure,
    net_leverage,
    short_exposure,
)


def _sym(ticker: str = "AAPL", multiplier: str = "1") -> Symbol:
    return Symbol(
        ticker=ticker,
        asset_class=AssetClass.EQUITY,
        contract_multiplier=Decimal(multiplier),
    )


def _pos(ticker: str, qty: str, price: str, multiplier: str = "1") -> Position:
    sym = _sym(ticker, multiplier)
    return Position(
        symbol=sym,
        quantity=Decimal(qty),
        avg_entry_price=Decimal(price),
        current_price=Decimal(price),
    )


# ---------------------------------------------------------------------------
# Exposure primitives (hand-computed)
# ---------------------------------------------------------------------------

def test_exposures_long_only():
    # 100 @ 10 = 1000 long; 50 @ 20 = 1000 long. Total 2000 long, 0 short.
    positions = [_pos("AAA", "100", "10"), _pos("BBB", "50", "20")]
    assert gross_exposure(positions) == Decimal("2000")
    assert net_exposure(positions) == Decimal("2000")
    assert long_exposure(positions) == Decimal("2000")
    assert short_exposure(positions) == Decimal("0")


def test_exposures_with_short():
    # +100 @ 10 = +1000 long ; -30 @ 20 = -600 (short notional 600).
    positions = [_pos("AAA", "100", "10"), _pos("BBB", "-30", "20")]
    assert gross_exposure(positions) == Decimal("1600")   # 1000 + 600
    assert net_exposure(positions) == Decimal("400")      # 1000 - 600
    assert long_exposure(positions) == Decimal("1000")
    assert short_exposure(positions) == Decimal("600")    # positive magnitude


def test_market_neutral_book():
    # +50 @ 20 = +1000 ; -100 @ 10 = -1000. Gross 2000, net 0.
    positions = [_pos("AAA", "50", "20"), _pos("BBB", "-100", "10")]
    assert gross_exposure(positions) == Decimal("2000")
    assert net_exposure(positions) == Decimal("0")


def test_empty_book_exposures_zero():
    assert gross_exposure([]) == Decimal("0")
    assert net_exposure([]) == Decimal("0")
    assert long_exposure([]) == Decimal("0")
    assert short_exposure([]) == Decimal("0")


def test_contract_multiplier_scales_notional():
    # 2 contracts @ 100 with multiplier 50 = 2*100*50 = 10000 notional.
    positions = [_pos("ESZ4", "2", "100", multiplier="50")]
    assert gross_exposure(positions) == Decimal("10000")
    assert net_exposure(positions) == Decimal("10000")


# ---------------------------------------------------------------------------
# Leverage ratios (hand-computed)
# ---------------------------------------------------------------------------

def test_gross_and_net_leverage_long_only():
    # gross 2000, equity 1000 -> 2.0x gross, 2.0x net.
    positions = [_pos("AAA", "100", "10"), _pos("BBB", "50", "20")]
    assert gross_leverage(positions, Decimal("1000")) == Decimal("2")
    assert net_leverage(positions, Decimal("1000")) == Decimal("2")


def test_net_leverage_signed_for_short_book():
    # net -600 against equity 1200 -> -0.5x net, gross 600/1200 = 0.5x.
    positions = [_pos("AAA", "-30", "20")]
    assert net_leverage(positions, Decimal("1200")) == Decimal("-0.5")
    assert gross_leverage(positions, Decimal("1200")) == Decimal("0.5")


def test_market_neutral_net_leverage_zero_gross_positive():
    positions = [_pos("AAA", "50", "20"), _pos("BBB", "-100", "10")]
    assert net_leverage(positions, Decimal("1000")) == Decimal("0")
    assert gross_leverage(positions, Decimal("1000")) == Decimal("2")


def test_flat_book_zero_leverage_against_positive_equity():
    assert gross_leverage([], Decimal("1000")) == Decimal("0")
    assert net_leverage([], Decimal("1000")) == Decimal("0")


# ---------------------------------------------------------------------------
# Fail-closed on degenerate equity
# ---------------------------------------------------------------------------

def test_non_positive_equity_returns_none():
    positions = [_pos("AAA", "100", "10")]
    assert gross_leverage(positions, Decimal("0")) is None
    assert net_leverage(positions, Decimal("0")) is None
    assert gross_leverage(positions, Decimal("-500")) is None
    assert net_leverage(positions, Decimal("-500")) is None


def test_equity_accepts_int_float_str_without_float_error():
    # 100 @ 10 = 1000 gross. Passing equity as different numeric types should
    # all give exactly 2x without binary-float contamination.
    positions = [_pos("AAA", "100", "10")]
    assert gross_leverage(positions, 500) == Decimal("2")
    assert gross_leverage(positions, "500") == Decimal("2")
    assert gross_leverage(positions, 500.0) == Decimal("2")


# ---------------------------------------------------------------------------
# compute_leverage / LeverageSnapshot
# ---------------------------------------------------------------------------

def test_compute_leverage_full_snapshot():
    positions = [_pos("AAA", "100", "10"), _pos("BBB", "-30", "20")]
    snap = compute_leverage(positions, Decimal("1000"))
    assert isinstance(snap, LeverageSnapshot)
    assert snap.equity == Decimal("1000")
    assert snap.gross_exposure == Decimal("1600")
    assert snap.net_exposure == Decimal("400")
    assert snap.long_exposure == Decimal("1000")
    assert snap.short_exposure == Decimal("600")
    assert snap.gross_leverage == Decimal("1.6")
    assert snap.net_leverage == Decimal("0.4")
    assert snap.is_levered is True
    assert snap.is_market_neutral is False


def test_compute_leverage_consumes_generator_once():
    positions = (_pos(t, "10", "10") for t in ("AAA", "BBB"))
    snap = compute_leverage(positions, Decimal("1000"))
    # 10@10 *2 = 200 gross. If the generator were iterated only once for some
    # fields, exposures would be wrong; assert all derived fields agree.
    assert snap.gross_exposure == Decimal("200")
    assert snap.long_exposure == Decimal("200")
    assert snap.net_exposure == Decimal("200")


def test_snapshot_fail_closed_and_flags():
    snap = compute_leverage([_pos("AAA", "100", "10")], Decimal("0"))
    assert snap.gross_leverage is None
    assert snap.net_leverage is None
    assert snap.is_levered is False               # unknown -> not levered
    assert snap.gross_exposure == Decimal("1000")  # exposures still computed


def test_market_neutral_flag():
    positions = [_pos("AAA", "50", "20"), _pos("BBB", "-100", "10")]
    snap = compute_leverage(positions, Decimal("1000"))
    assert snap.is_market_neutral is True
    assert snap.is_levered is True  # gross 2.0x


def test_not_levered_when_under_one_x():
    snap = compute_leverage([_pos("AAA", "10", "10")], Decimal("1000"))
    assert snap.gross_leverage == Decimal("0.1")
    assert snap.is_levered is False


def test_to_dict_round_trips_values():
    snap = compute_leverage([_pos("AAA", "100", "10")], Decimal("1000"))
    d = snap.to_dict()
    assert d["gross_leverage"] == Decimal("1")
    assert d["net_exposure"] == Decimal("1000")
    assert set(d.keys()) == {
        "equity", "gross_exposure", "net_exposure", "long_exposure",
        "short_exposure", "gross_leverage", "net_leverage",
    }


# ---------------------------------------------------------------------------
# Portfolio adapter (duck-typed, no real Portfolio import needed)
# ---------------------------------------------------------------------------

class _FakePortfolio:
    def __init__(self, positions, equity):
        self._positions = {p.symbol.ticker: p for p in positions}
        self._equity = equity

    @property
    def open_positions(self):
        return dict(self._positions)

    @property
    def equity(self):
        return self._equity


def test_leverage_from_portfolio_adapter():
    pf = _FakePortfolio(
        [_pos("AAA", "100", "10"), _pos("BBB", "-30", "20")],
        Decimal("1000"),
    )
    snap = leverage_from_portfolio(pf)
    assert snap.gross_leverage == Decimal("1.6")
    assert snap.net_leverage == Decimal("0.4")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
