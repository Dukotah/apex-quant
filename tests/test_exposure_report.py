"""
Tests for apex.risk.exposure_report.build_exposure_report.

All numbers are hand-computed with exact Decimal arithmetic so regressions
surface immediately.

Coverage:
  - Single long: gross = net = long, short = 0, pct = mv / equity.
  - Single short: gross = short, net = -short, long = 0 (signs correct).
  - Long + short book: gross = long + short, net = long - short.
  - contract_multiplier flows through market_value into exposure.
  - Per-symbol breakdown is correct, ordered by ticker, and excludes zero qty.
  - Mapping input (Portfolio.open_positions style) and iterable input agree.
  - equity <= 0 => all pct fields are None (no divide-by-zero).
  - Empty input => zero exposures, empty breakdown, leverage None handling.
  - leverage property == gross_pct.
"""

from __future__ import annotations

from decimal import Decimal

from apex.core.models import AssetClass, Position, Symbol
from apex.risk.exposure_report import build_exposure_report

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AAPL = Symbol("AAPL", AssetClass.EQUITY)
TSLA = Symbol("TSLA", AssetClass.EQUITY)
ES = Symbol("ES", AssetClass.FUTURE, contract_multiplier=Decimal("50"))


def _pos(symbol: Symbol, qty: str, price: str) -> Position:
    p = Decimal(price)
    return Position(
        symbol=symbol,
        quantity=Decimal(qty),
        avg_entry_price=p,
        current_price=p,
    )


# ---------------------------------------------------------------------------
# Single position
# ---------------------------------------------------------------------------


def test_single_long():
    # 10 shares @ $100 = $1000 long notional; equity $10,000.
    rep = build_exposure_report([_pos(AAPL, "10", "100")], Decimal("10000"))
    assert rep.gross == Decimal("1000")
    assert rep.net == Decimal("1000")
    assert rep.long == Decimal("1000")
    assert rep.short == Decimal("0")
    assert rep.gross_pct == Decimal("0.1")
    assert rep.net_pct == Decimal("0.1")
    assert rep.long_pct == Decimal("0.1")
    assert rep.short_pct == Decimal("0")
    assert rep.num_positions == 1
    assert rep.num_long == 1
    assert rep.num_short == 0


def test_single_short_signs():
    # -5 shares @ $200 = market_value -$1000; gross 1000, net -1000.
    rep = build_exposure_report([_pos(TSLA, "-5", "200")], Decimal("10000"))
    assert rep.gross == Decimal("1000")
    assert rep.net == Decimal("-1000")
    assert rep.long == Decimal("0")
    assert rep.short == Decimal("1000")
    assert rep.net_pct == Decimal("-0.1")
    assert rep.short_pct == Decimal("0.1")
    assert rep.num_short == 1
    assert rep.num_long == 0
    sym = rep.by_symbol["TSLA"]
    assert sym.is_short and not sym.is_long
    assert sym.market_value == Decimal("-1000")
    assert sym.gross == Decimal("1000")
    assert sym.short == Decimal("1000")
    assert sym.long == Decimal("0")


# ---------------------------------------------------------------------------
# Mixed long/short book
# ---------------------------------------------------------------------------


def test_long_short_book():
    # AAPL: +10 @ 100 = +1000 long
    # TSLA: -5  @ 200 = -1000 short
    positions = [_pos(AAPL, "10", "100"), _pos(TSLA, "-5", "200")]
    rep = build_exposure_report(positions, Decimal("10000"))
    assert rep.long == Decimal("1000")
    assert rep.short == Decimal("1000")
    assert rep.gross == Decimal("2000")  # 1000 + 1000
    assert rep.net == Decimal("0")  # 1000 - 1000 (market neutral)
    assert rep.gross_pct == Decimal("0.2")
    assert rep.net_pct == Decimal("0")
    assert rep.leverage == Decimal("0.2")  # gross / equity
    # Per-symbol breakdown ordered by ticker: AAPL before TSLA.
    assert list(rep.by_symbol.keys()) == ["AAPL", "TSLA"]


def test_contract_multiplier_flows_through():
    # 2 contracts @ $5000 with 50x multiplier = 2 * 5000 * 50 = $500,000.
    rep = build_exposure_report([_pos(ES, "2", "5000")], Decimal("1000000"))
    assert rep.gross == Decimal("500000")
    assert rep.long == Decimal("500000")
    assert rep.gross_pct == Decimal("0.5")
    assert rep.by_symbol["ES"].market_value == Decimal("500000")


# ---------------------------------------------------------------------------
# Input forms & filtering
# ---------------------------------------------------------------------------


def test_mapping_input_matches_iterable():
    positions = [_pos(AAPL, "10", "100"), _pos(TSLA, "-5", "200")]
    mapping = {p.symbol.ticker: p for p in positions}
    rep_iter = build_exposure_report(positions, Decimal("10000"))
    rep_map = build_exposure_report(mapping, Decimal("10000"))
    assert rep_iter.gross == rep_map.gross
    assert rep_iter.net == rep_map.net
    assert rep_iter.long == rep_map.long
    assert rep_iter.short == rep_map.short
    assert list(rep_iter.by_symbol.keys()) == list(rep_map.by_symbol.keys())


def test_zero_quantity_excluded():
    rep = build_exposure_report(
        [_pos(AAPL, "0", "100"), _pos(TSLA, "3", "50")],
        Decimal("10000"),
    )
    assert "AAPL" not in rep.by_symbol
    assert rep.num_positions == 1
    assert rep.gross == Decimal("150")  # only TSLA 3 * 50


# ---------------------------------------------------------------------------
# Edge cases — fail gracefully
# ---------------------------------------------------------------------------


def test_empty_positions():
    rep = build_exposure_report([], Decimal("10000"))
    assert rep.gross == Decimal("0")
    assert rep.net == Decimal("0")
    assert rep.long == Decimal("0")
    assert rep.short == Decimal("0")
    assert rep.by_symbol == {}
    assert rep.num_positions == 0
    assert rep.gross_pct == Decimal("0")  # 0 / 10000


def test_non_positive_equity_yields_none_pct():
    positions = [_pos(AAPL, "10", "100")]
    for eq in (Decimal("0"), Decimal("-500")):
        rep = build_exposure_report(positions, eq)
        # Absolute figures still computed.
        assert rep.gross == Decimal("1000")
        # Ratios undefined => None, never a divide-by-zero or garbage.
        assert rep.gross_pct is None
        assert rep.net_pct is None
        assert rep.long_pct is None
        assert rep.short_pct is None
        assert rep.leverage is None
        assert rep.by_symbol["AAPL"].pct_of_equity is None


def test_does_not_mutate_input():
    positions = [_pos(AAPL, "10", "100")]
    before = list(positions)
    build_exposure_report(positions, Decimal("10000"))
    assert positions == before
