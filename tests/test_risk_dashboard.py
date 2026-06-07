"""
tests/test_risk_dashboard.py
============================
Tests for the pure risk-dashboard core (scripts/risk_dashboard.py). Imports by
full path so no package __init__ edit is needed. All values are hand-computed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from scripts.risk_dashboard import (
    PositionSnapshot,
    _positions_from_state,
    _to_decimal,
    compute_risk_snapshot,
    render_snapshot,
)

TS = datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc)
D = Decimal


def _pos(ticker: str, qty: str, entry: str, price: str) -> PositionSnapshot:
    return PositionSnapshot(ticker, D(qty), D(entry), D(price))


# --------------------------------------------------------------- known values

def test_long_only_known_values():
    # SPY: 100 @ 50 now 60 -> notional 6000 ; GLD: 50 @ 100 now 80 -> 4000.
    # equity 12000, cash 2000. gross=net=10000. lev gross=net=10000/12000.
    positions = [_pos("SPY", "100", "50", "60"), _pos("GLD", "50", "100", "80")]
    snap = compute_risk_snapshot(positions, equity=D("12000"), cash=D("2000"), timestamp=TS)

    assert snap.gross_exposure == D("10000")
    assert snap.net_exposure == D("10000")
    assert snap.long_exposure == D("10000")
    assert snap.short_exposure == D("0")
    assert snap.gross_leverage == D("10000") / D("12000")
    assert snap.net_leverage == D("10000") / D("12000")
    assert snap.cash_pct == D("2000") / D("12000")
    assert snap.num_positions == 2
    # SPY is the larger name (6000 > 4000) and sorts first.
    assert snap.largest_ticker == "SPY"
    assert snap.largest_concentration == D("6000") / D("12000")
    assert [s.ticker for s in snap.per_symbol] == ["SPY", "GLD"]


def test_long_short_net_vs_gross():
    # Long SPY 6000, short TLT 80 shares @ price 50 -> qty -80 -> mv -4000.
    long_p = _pos("SPY", "100", "50", "60")          # +6000
    short_p = _pos("TLT", "-80", "50", "50")          # -4000
    snap = compute_risk_snapshot([long_p, short_p], equity=D("10000"),
                                 cash=D("8000"), timestamp=TS)

    assert snap.gross_exposure == D("10000")   # 6000 + 4000
    assert snap.net_exposure == D("2000")      # 6000 - 4000
    assert snap.long_exposure == D("6000")
    assert snap.short_exposure == D("4000")    # reported positive
    assert snap.gross_leverage == D("1.0")
    assert snap.net_leverage == D("0.2")
    # largest gross is SPY (6000); short flag carried for TLT line.
    assert snap.largest_ticker == "SPY"
    tlt_line = next(s for s in snap.per_symbol if s.ticker == "TLT")
    assert tlt_line.is_short is True
    assert tlt_line.signed_value == D("-4000")
    assert tlt_line.gross_value == D("4000")


def test_empty_positions_flat_snapshot():
    snap = compute_risk_snapshot([], equity=D("5000"), cash=D("5000"), timestamp=TS)
    assert snap.num_positions == 0
    assert snap.gross_exposure == D("0")
    assert snap.net_exposure == D("0")
    assert snap.gross_leverage == D("0")
    assert snap.largest_ticker is None
    assert snap.largest_concentration == D("0")
    assert snap.per_symbol == []


def test_non_positive_equity_fails_closed_to_zero_ratios():
    # equity 0 must not divide-by-zero; every ratio reads 0.
    positions = [_pos("SPY", "100", "50", "60")]
    snap = compute_risk_snapshot(positions, equity=D("0"), cash=D("0"), timestamp=TS)
    assert snap.gross_exposure == D("6000")    # raw notional still computed
    assert snap.gross_leverage == D("0")       # ratio fails closed
    assert snap.net_leverage == D("0")
    assert snap.cash_pct == D("0")
    assert snap.largest_concentration == D("0")


def test_determinism_and_sort_stability():
    # Same data, different input order -> identical snapshot.
    a = [_pos("AAA", "10", "5", "5"), _pos("BBB", "10", "10", "10"),
         _pos("CCC", "10", "10", "10")]
    b = list(reversed(a))
    sa = compute_risk_snapshot(a, equity=D("1000"), cash=D("0"), timestamp=TS)
    sb = compute_risk_snapshot(b, equity=D("1000"), cash=D("0"), timestamp=TS)
    assert [s.ticker for s in sa.per_symbol] == [s.ticker for s in sb.per_symbol]
    # BBB & CCC tie on gross (100); AAA is smallest (50). Tie broken by ticker asc.
    assert [s.ticker for s in sa.per_symbol] == ["BBB", "CCC", "AAA"]
    assert sa == sb


def test_injected_timestamp_is_preserved():
    snap = compute_risk_snapshot([], equity=D("100"), cash=D("100"), timestamp=TS, mode="live")
    assert snap.timestamp == TS
    assert snap.mode == "live"


# --------------------------------------------------------------- helpers

def test_to_decimal_fails_closed():
    assert _to_decimal("12.5") == D("12.5")
    assert _to_decimal(None) == D("0")
    assert _to_decimal("garbage") == D("0")
    assert _to_decimal("x", default=D("7")) == D("7")


def test_positions_from_state_skips_flat_and_garbage():
    blob = {
        "SPY": {"qty": "100", "avg_entry_price": "50", "current_price": "60"},
        "FLAT": {"qty": "0", "avg_entry_price": "10", "current_price": "10"},
        "BAD": {"qty": "oops", "avg_entry_price": "1", "current_price": "1"},
    }
    out = _positions_from_state(blob)
    tickers = {p.ticker for p in out}
    assert "SPY" in tickers
    assert "FLAT" not in tickers   # zero qty skipped
    assert "BAD" not in tickers    # qty parses to 0 -> skipped
    spy = next(p for p in out if p.ticker == "SPY")
    assert spy.market_value == D("6000")


# --------------------------------------------------------------- rendering

def test_render_contains_key_lines():
    positions = [_pos("SPY", "100", "50", "60"), _pos("TLT", "-80", "50", "50")]
    snap = compute_risk_snapshot(positions, equity=D("10000"), cash=D("8000"), timestamp=TS)
    text = render_snapshot(snap)
    assert "RISK DASHBOARD" in text
    assert "gross exposure" in text
    assert "net exposure" in text
    assert "SPY" in text and "TLT" in text
    assert "SHORT" in text and "LONG" in text
    assert "largest name" in text


def test_render_empty_positions():
    snap = compute_risk_snapshot([], equity=D("5000"), cash=D("5000"), timestamp=TS)
    text = render_snapshot(snap)
    assert "(no open positions)" in text


def test_module_import_has_no_side_effects():
    # Re-importing must not execute main() or touch any DB/network.
    import importlib

    import scripts.risk_dashboard as mod
    importlib.reload(mod)
    assert hasattr(mod, "compute_risk_snapshot")
    assert hasattr(mod, "main")
