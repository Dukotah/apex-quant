"""
tests.test_sleeve_attribution
=============================
Pure, fast tests for apex.analytics.sleeve_attribution. Hand-computed known
values for a profitable sleeve, a losing sleeve, a mixed multi-sleeve book, and
the empty / degenerate edge cases the golden rules require.

The FIFO matching rule and commission attribution are pinned with explicit
arithmetic so a regression in the matching logic fails here, not silently in a
live P&L breakdown.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from apex.analytics.sleeve_attribution import (
    SleeveAttribution,
    attribute_fills,
    match_round_trips,
)
from apex.core.events import FillEvent
from apex.core.models import AssetClass, OrderSide, Symbol

UTC = timezone.utc


def _sym(ticker: str, multiplier: str = "1") -> Symbol:
    return Symbol(ticker, AssetClass.ETF, contract_multiplier=Decimal(multiplier))


def _fill(
    symbol: Symbol,
    side: OrderSide,
    qty: str,
    price: str,
    commission: str = "0",
) -> FillEvent:
    return FillEvent(
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        fill_price=Decimal(price),
        commission=Decimal(commission),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )


# ============================================================ match_round_trips


def test_single_winning_round_trip():
    sym = _sym("SPY")
    fills = [
        _fill(sym, OrderSide.BUY, "10", "100"),
        _fill(sym, OrderSide.SELL, "10", "110"),
    ]
    # (110 - 100) * 10 = 100
    assert match_round_trips(fills) == [Decimal("100")]


def test_single_losing_round_trip():
    sym = _sym("SPY")
    fills = [
        _fill(sym, OrderSide.BUY, "10", "100"),
        _fill(sym, OrderSide.SELL, "10", "90"),
    ]
    # (90 - 100) * 10 = -100
    assert match_round_trips(fills) == [Decimal("-100")]


def test_short_round_trip():
    sym = _sym("SPY")
    fills = [
        _fill(sym, OrderSide.SELL, "5", "100"),  # open short
        _fill(sym, OrderSide.BUY, "5", "80"),  # cover at lower price -> win
    ]
    # short: (entry - exit) * qty = (100 - 80) * 5 = 100
    assert match_round_trips(fills) == [Decimal("100")]


def test_fifo_partial_close_then_remainder():
    sym = _sym("SPY")
    fills = [
        _fill(sym, OrderSide.BUY, "10", "100"),  # lot A
        _fill(sym, OrderSide.BUY, "10", "120"),  # lot B
        _fill(sym, OrderSide.SELL, "15", "130"),  # closes all of A, 5 of B (FIFO)
    ]
    # A fully closed: (130 - 100) * 10 = 300
    # B 5 units:      (130 - 120) * 5  = 50
    assert match_round_trips(fills) == [Decimal("300"), Decimal("50")]


def test_flip_splits_close_and_open():
    sym = _sym("SPY")
    fills = [
        _fill(sym, OrderSide.BUY, "10", "100"),  # long 10
        _fill(sym, OrderSide.SELL, "15", "110"),  # close 10 long, open 5 short
        _fill(sym, OrderSide.BUY, "5", "105"),  # cover the 5 short
    ]
    # close long: (110 - 100) * 10 = 100
    # cover short: (110 - 105) * 5 = 25
    assert match_round_trips(fills) == [Decimal("100"), Decimal("25")]


def test_contract_multiplier_applied():
    sym = _sym("ES", multiplier="50")
    fills = [
        _fill(sym, OrderSide.BUY, "1", "100"),
        _fill(sym, OrderSide.SELL, "1", "102"),
    ]
    # (102 - 100) * 1 * 50 = 100
    assert match_round_trips(fills) == [Decimal("100")]


def test_commission_reduces_realized_pnl():
    sym = _sym("SPY")
    fills = [
        _fill(sym, OrderSide.BUY, "10", "100", commission="5"),
        _fill(sym, OrderSide.SELL, "10", "110", commission="5"),
    ]
    # gross 100, minus entry 5, minus exit 5 = 90
    assert match_round_trips(fills) == [Decimal("90")]


def test_commission_prorated_on_partial_close():
    sym = _sym("SPY")
    fills = [
        _fill(sym, OrderSide.BUY, "10", "100", commission="10"),  # entry comm 10
        _fill(sym, OrderSide.SELL, "5", "110", commission="4"),  # exit comm 4
    ]
    # one closed trade for 5 units:
    #   gross (110-100)*5 = 50
    #   entry comm share: 10 * 5/10 = 5
    #   exit comm share:  4 * 5/5  = 4
    #   net = 50 - 5 - 4 = 41
    assert match_round_trips(fills) == [Decimal("41")]


def test_open_lot_only_yields_no_trades():
    sym = _sym("SPY")
    fills = [_fill(sym, OrderSide.BUY, "10", "100")]  # never closed
    assert match_round_trips(fills) == []


def test_zero_qty_fill_ignored():
    sym = _sym("SPY")
    fills = [
        _fill(sym, OrderSide.BUY, "0", "100"),  # ignored
        _fill(sym, OrderSide.BUY, "10", "100"),
        _fill(sym, OrderSide.SELL, "10", "110"),
    ]
    assert match_round_trips(fills) == [Decimal("100")]


# ============================================================ attribute_fills


def test_attribute_empty_is_empty():
    assert attribute_fills([]) == {}


def test_profitable_sleeve():
    sym = _sym("SPY")
    fills = [
        _fill(sym, OrderSide.BUY, "10", "100"),
        _fill(sym, OrderSide.SELL, "10", "110"),  # +100
        _fill(sym, OrderSide.BUY, "10", "110"),
        _fill(sym, OrderSide.SELL, "10", "115"),  # +50
    ]
    result = attribute_fills(fills, capital_base=Decimal("10000"))
    attr = result["SPY"]
    assert attr.realized_pnl == Decimal("150")
    assert attr.trade_count == 2
    assert attr.win_count == 2
    assert attr.loss_count == 0
    assert attr.win_rate == Decimal("1")
    # 150 / 10000
    assert attr.return_contribution == Decimal("0.015")


def test_losing_sleeve():
    sym = _sym("TLT")
    fills = [
        _fill(sym, OrderSide.BUY, "10", "100"),
        _fill(sym, OrderSide.SELL, "10", "95"),  # -50
        _fill(sym, OrderSide.BUY, "10", "95"),
        _fill(sym, OrderSide.SELL, "10", "90"),  # -50
    ]
    result = attribute_fills(fills, capital_base=Decimal("10000"))
    attr = result["TLT"]
    assert attr.realized_pnl == Decimal("-100")
    assert attr.trade_count == 2
    assert attr.win_count == 0
    assert attr.loss_count == 2
    assert attr.win_rate == Decimal("0")
    assert attr.return_contribution == Decimal("-0.01")


def test_mixed_book_per_sleeve_breakdown():
    spy = _sym("SPY")
    tlt = _sym("TLT")
    gld = _sym("GLD")
    fills = [
        # SPY: one win (+100), one loss (-20) -> net +80, win_rate 0.5
        _fill(spy, OrderSide.BUY, "10", "100"),
        _fill(spy, OrderSide.SELL, "10", "110"),
        _fill(spy, OrderSide.BUY, "10", "110"),
        _fill(spy, OrderSide.SELL, "10", "108"),
        # TLT: pure loss (-50)
        _fill(tlt, OrderSide.BUY, "5", "100"),
        _fill(tlt, OrderSide.SELL, "5", "90"),
        # GLD: open only, no closed trade
        _fill(gld, OrderSide.BUY, "3", "200"),
    ]
    result = attribute_fills(fills, capital_base=Decimal("100000"))

    assert set(result) == {"SPY", "TLT", "GLD"}

    spy_attr = result["SPY"]
    assert spy_attr.realized_pnl == Decimal("80")
    assert spy_attr.trade_count == 2
    assert spy_attr.win_count == 1
    assert spy_attr.loss_count == 1
    assert spy_attr.win_rate == Decimal("0.5")

    tlt_attr = result["TLT"]
    assert tlt_attr.realized_pnl == Decimal("-50")
    assert tlt_attr.win_count == 0
    assert tlt_attr.loss_count == 1

    gld_attr = result["GLD"]
    assert gld_attr.realized_pnl == Decimal("0")
    assert gld_attr.trade_count == 0
    assert gld_attr.win_rate == Decimal("0")

    # Sums reconcile: total realized P&L and total return contribution.
    total_pnl = sum(a.realized_pnl for a in result.values())
    assert total_pnl == Decimal("30")  # 80 - 50 + 0
    total_contrib = sum(a.return_contribution for a in result.values())
    assert total_contrib == Decimal("30") / Decimal("100000")


def test_zero_capital_base_gives_zero_contribution():
    sym = _sym("SPY")
    fills = [
        _fill(sym, OrderSide.BUY, "10", "100"),
        _fill(sym, OrderSide.SELL, "10", "110"),
    ]
    result = attribute_fills(fills)  # default capital_base = 0
    attr = result["SPY"]
    assert attr.realized_pnl == Decimal("100")  # P&L still computed
    assert attr.return_contribution == Decimal("0")  # but no divide-by-zero


def test_negative_capital_base_gives_zero_contribution():
    sym = _sym("SPY")
    fills = [
        _fill(sym, OrderSide.BUY, "10", "100"),
        _fill(sym, OrderSide.SELL, "10", "110"),
    ]
    result = attribute_fills(fills, capital_base=Decimal("-5"))
    assert result["SPY"].return_contribution == Decimal("0")


def test_result_is_frozen():
    attr = SleeveAttribution(
        ticker="SPY",
        realized_pnl=Decimal("0"),
        trade_count=0,
        win_count=0,
        loss_count=0,
        win_rate=Decimal("0"),
        return_contribution=Decimal("0"),
    )
    try:
        attr.realized_pnl = Decimal("1")  # type: ignore[misc]
    except AttributeError:
        pass
    else:  # pragma: no cover - frozen dataclass must reject mutation
        raise AssertionError("SleeveAttribution should be frozen")
