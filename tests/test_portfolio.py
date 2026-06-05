"""
Tests for apex.risk.portfolio.Portfolio.

All numbers are hand-computed so that regressions surface immediately.
Every monetary figure uses exact Decimal arithmetic — never float.

Coverage:
  - BUY fill: cash decreases, position opens, equity updates.
  - on_market: marks position to bar close, updates equity and unrealized P&L.
  - SELL fill: cash increases, realized P&L booked, position closes.
  - peak_equity tracks the highest equity ever seen.
  - drawdown = (peak - current_equity) / peak  (fraction 0..1).
  - exposure = sum(abs(market_value)) for open positions.
  - open_positions reflects holdings; closed positions are removed.
  - day_start_equity resets via start_new_day().
  - last_price updates on both fills and market events.
  - Adding to a long recomputes avg_entry_price correctly.
  - Partial sell leaves a residual position with correct state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from apex.core.events import FillEvent, MarketEvent
from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.risk.portfolio import Portfolio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SYM = Symbol("AAPL", AssetClass.EQUITY)
SYM2 = Symbol("TSLA", AssetClass.EQUITY)

_DT = datetime(2024, 6, 1, 14, 30, tzinfo=timezone.utc)


def _fill(
    symbol: Symbol,
    side: OrderSide,
    quantity: str,
    price: str,
    commission: str = "0",
) -> FillEvent:
    return FillEvent(
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        fill_price=Decimal(price),
        commission=Decimal(commission),
        timestamp=_DT,
    )


def _bar(symbol: Symbol, close: str) -> MarketEvent:
    c = Decimal(close)
    bar = Bar(
        symbol=symbol,
        timestamp=_DT,
        open=c,
        high=c,
        low=c,
        close=c,
        volume=Decimal("1000"),
    )
    return MarketEvent(bar=bar)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_initial_state():
    p = Portfolio(Decimal("10000"))
    assert p.equity == Decimal("10000")
    assert p.cash == Decimal("10000")
    assert p.peak_equity == Decimal("10000")
    assert p.day_start_equity == Decimal("10000")
    assert p.open_positions == {}
    assert p.exposure == Decimal("0")
    assert p.realized_pnl == Decimal("0")
    assert p.unrealized_pnl == Decimal("0")
    assert p.drawdown == Decimal("0")


def test_negative_capital_rejected():
    with pytest.raises(ValueError):
        Portfolio(Decimal("-1"))


# ---------------------------------------------------------------------------
# Buy fill
# ---------------------------------------------------------------------------

def test_buy_reduces_cash_and_opens_position():
    """
    Buy 10 AAPL @ 150, commission 1.
    cash = 10000 - 10*150 - 1 = 10000 - 1501 = 8499
    equity = 8499 + 10*150 = 8499 + 1500 = 9999
    """
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "10", "150", "1"))

    assert p.cash == Decimal("8499")
    assert Decimal("AAPL") if False else True   # just gate
    pos = p.open_positions["AAPL"]
    assert pos.quantity == Decimal("10")
    assert pos.avg_entry_price == Decimal("150")
    assert pos.current_price == Decimal("150")
    assert p.equity == Decimal("9999")
    assert p.exposure == Decimal("1500")


# ---------------------------------------------------------------------------
# Mark to market
# ---------------------------------------------------------------------------

def test_mark_to_market_updates_equity_and_unrealized():
    """
    Buy 10 AAPL @ 150 (equity=9999 after commission=1).
    Mark up to 160.
    unrealized = (160-150)*10 = 100
    equity = 8499 + 10*160 = 8499 + 1600 = 10099
    """
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "10", "150", "1"))

    p.on_market(_bar(SYM, "160"))

    assert p.last_price["AAPL"] == Decimal("160")
    assert p.unrealized_pnl == Decimal("100")   # (160-150)*10
    assert p.equity == Decimal("10099")         # 8499 + 1600
    assert p.exposure == Decimal("1600")        # 10 * 160


def test_peak_equity_advances_on_mark_up():
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "10", "150", "0"))
    p.on_market(_bar(SYM, "160"))
    # equity = 10000 - 10*150 + 10*160 = 10000 + 100 = 10100
    assert p.peak_equity == Decimal("10100")
    assert p.drawdown == Decimal("0")   # still at peak


def test_drawdown_after_price_falls():
    """
    Buy 10 @ 150 (no commission).  Peak = 10000 (unchanged, position at cost).
    Mark up to 200: equity = 10000 + (200-150)*10 = 10500.  New peak = 10500.
    Mark down to 100: equity = 10000 + (100-150)*10 = 9500.
    drawdown = (10500 - 9500) / 10500 = 1000/10500 ≈ 0.09524...
    """
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "10", "150", "0"))
    p.on_market(_bar(SYM, "200"))
    assert p.peak_equity == Decimal("10500")
    p.on_market(_bar(SYM, "100"))
    expected_dd = Decimal("1000") / Decimal("10500")
    assert p.drawdown == expected_dd
    assert p.equity == Decimal("9500")


# ---------------------------------------------------------------------------
# Sell fill / realized P&L
# ---------------------------------------------------------------------------

def test_sell_realizes_pnl_and_updates_cash():
    """
    Buy 10 AAPL @ 100 (no commission).
    cash = 10000 - 1000 = 9000. equity = 10000.
    Mark to 120.
    Sell 10 @ 120 (no commission).
    realized_pnl = (120-100)*10 = 200
    cash = 9000 + 10*120 = 9000 + 1200 = 10200
    equity = 10200 (no open positions)
    """
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "10", "100", "0"))
    p.on_market(_bar(SYM, "120"))
    p.on_fill(_fill(SYM, OrderSide.SELL, "10", "120", "0"))

    assert p.realized_pnl == Decimal("200")
    assert p.cash == Decimal("10200")
    assert p.equity == Decimal("10200")
    assert p.unrealized_pnl == Decimal("0")
    assert "AAPL" not in p.open_positions


def test_sell_with_commission_reduces_proceeds():
    """
    Buy 5 @ 200, comm=0.  Sell 5 @ 200, comm=5.
    realized = (200-200)*5 = 0
    cash = 10000 - 1000 + 1000 - 5 = 9995
    """
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "5", "200", "0"))
    p.on_fill(_fill(SYM, OrderSide.SELL, "5", "200", "5"))

    assert p.realized_pnl == Decimal("0")
    assert p.cash == Decimal("9995")
    assert "AAPL" not in p.open_positions


def test_partial_sell_leaves_residual_position():
    """
    Buy 10 @ 100.  Sell 4 @ 110.
    realized = (110-100)*4 = 40
    remaining qty = 6, avg_entry stays 100.
    cash = 10000 - 1000 + 440 = 9440
    """
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "10", "100", "0"))
    p.on_fill(_fill(SYM, OrderSide.SELL, "4", "110", "0"))

    assert p.realized_pnl == Decimal("40")
    assert p.cash == Decimal("9440")   # 10000 - 1000 + 440
    pos = p.open_positions["AAPL"]
    assert pos.quantity == Decimal("6")
    assert pos.avg_entry_price == Decimal("100")


# ---------------------------------------------------------------------------
# Closing a position removes it from open_positions
# ---------------------------------------------------------------------------

def test_closing_position_removes_from_open_positions():
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "10", "100", "0"))
    assert "AAPL" in p.open_positions
    p.on_fill(_fill(SYM, OrderSide.SELL, "10", "100", "0"))
    assert "AAPL" not in p.open_positions
    assert p.exposure == Decimal("0")


# ---------------------------------------------------------------------------
# avg_entry_price on position adds
# ---------------------------------------------------------------------------

def test_add_to_long_updates_avg_entry():
    """
    Buy 4 @ 100 then buy 6 @ 150.
    avg = (4*100 + 6*150) / 10 = (400 + 900) / 10 = 130
    """
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "4", "100", "0"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "6", "150", "0"))

    pos = p.open_positions["AAPL"]
    assert pos.quantity == Decimal("10")
    assert pos.avg_entry_price == Decimal("130")


# ---------------------------------------------------------------------------
# peak_equity and drawdown across a rise-then-fall cycle
# ---------------------------------------------------------------------------

def test_peak_and_drawdown_rise_then_fall():
    """
    Start: 10000. Buy 10 @ 100 (no comm). Peak = 10000.
    Mark to 150: equity = 10000 + 500 = 10500. Peak = 10500.
    Mark to 200: equity = 10000 + 1000 = 11000. Peak = 11000.
    Mark back to 50: equity = 10000 - 500 = 9500.
    drawdown = (11000 - 9500) / 11000 = 1500/11000
    """
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "10", "100", "0"))
    # equity at cost = 10000, peak unchanged
    p.on_market(_bar(SYM, "150"))
    assert p.peak_equity == Decimal("10500")
    p.on_market(_bar(SYM, "200"))
    assert p.peak_equity == Decimal("11000")
    p.on_market(_bar(SYM, "50"))
    assert p.peak_equity == Decimal("11000")   # peak doesn't fall
    assert p.equity == Decimal("9500")
    expected = Decimal("1500") / Decimal("11000")
    assert p.drawdown == expected


# ---------------------------------------------------------------------------
# exposure reflects current holdings
# ---------------------------------------------------------------------------

def test_exposure_with_multiple_positions():
    """
    Buy 10 AAPL @ 100 → market value = 1000.
    Buy 5 TSLA @ 200 → market value = 1000.
    exposure = 2000.
    """
    p = Portfolio(Decimal("50000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "10", "100", "0"))
    p.on_fill(_fill(SYM2, OrderSide.BUY, "5", "200", "0"))
    assert p.exposure == Decimal("2000")
    assert len(p.open_positions) == 2


def test_exposure_zero_after_all_closed():
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "10", "100", "0"))
    p.on_fill(_fill(SYM, OrderSide.SELL, "10", "100", "0"))
    assert p.exposure == Decimal("0")


# ---------------------------------------------------------------------------
# day_start_equity reset
# ---------------------------------------------------------------------------

def test_start_new_day_resets_day_start_equity():
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "10", "100", "0"))
    p.on_market(_bar(SYM, "150"))
    # equity is now 10500; day_start is still 10000
    assert p.day_start_equity == Decimal("10000")
    p.start_new_day()
    assert p.day_start_equity == Decimal("10500")


# ---------------------------------------------------------------------------
# last_price tracking
# ---------------------------------------------------------------------------

def test_last_price_updated_by_fill():
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "10", "150", "0"))
    assert p.last_price["AAPL"] == Decimal("150")


def test_last_price_updated_by_market_event():
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "10", "150", "0"))
    p.on_market(_bar(SYM, "175"))
    assert p.last_price["AAPL"] == Decimal("175")


def test_last_price_for_symbol_without_position():
    """Market events for symbols not held still update last_price."""
    p = Portfolio(Decimal("10000"))
    p.on_market(_bar(SYM, "200"))
    assert p.last_price.get("AAPL") == Decimal("200")


# ---------------------------------------------------------------------------
# Risk manager attribute interface (smoke-test with all 6 required)
# ---------------------------------------------------------------------------

def test_all_risk_manager_snapshot_attributes_present():
    """
    Confirm all six attributes the RiskManager reads are accessible and
    return the correct types (not just that they exist).
    """
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill(SYM, OrderSide.BUY, "5", "100", "0"))
    p.on_market(_bar(SYM, "110"))

    # equity
    assert isinstance(p.equity, Decimal)
    # peak_equity
    assert isinstance(p.peak_equity, Decimal)
    # day_start_equity
    assert isinstance(p.day_start_equity, Decimal)
    # open_positions
    assert isinstance(p.open_positions, dict)
    assert "AAPL" in p.open_positions
    # exposure
    assert isinstance(p.exposure, Decimal)
    assert p.exposure == Decimal("550")   # 5 * 110
    # last_price
    assert isinstance(p.last_price, dict)
    assert p.last_price["AAPL"] == Decimal("110")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_deterministic_same_inputs_same_outputs():
    """Same sequence of events always yields the same portfolio state."""
    def _run():
        port = Portfolio(Decimal("10000"))
        port.on_fill(_fill(SYM, OrderSide.BUY, "10", "100", "0"))
        port.on_market(_bar(SYM, "120"))
        port.on_fill(_fill(SYM, OrderSide.SELL, "5", "120", "0"))
        port.on_market(_bar(SYM, "130"))
        return port.equity, port.realized_pnl, port.drawdown

    r1 = _run()
    r2 = _run()
    assert r1 == r2
