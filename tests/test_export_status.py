"""
Tests for scripts.export_status — the read-only JSON status snapshot the
apex-trader dashboard polls.

These exercise the pure ``build_status`` / ``write_status`` functions against a
synthetic Portfolio (built via its public fill/market API) — no broker, no state
DB, no wall clock. They assert the document SHAPE and TYPES: every money/price
field is a Decimal serialized to ``str`` (no float drift), positions and
per_strategy are lists of the right keys, and the snapshot is deterministic
(generated_at is an injected parameter).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from apex.core.events import FillEvent, MarketEvent
from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.risk.portfolio import Portfolio
from scripts.export_status import build_status, write_status

UTC = timezone.utc
SPY = Symbol("SPY", AssetClass.ETF)
GLD = Symbol("GLD", AssetClass.ETF)
GENERATED_AT = "2024-06-03T20:00:00+00:00"

MONEY_FIELDS = ("equity", "cash", "drawdown", "peak_equity")
POSITION_FIELDS = ("ticker", "qty", "avg_entry", "current", "unrealized_pnl")


def _mark(symbol: Symbol, price: str) -> MarketEvent:
    """A MarketEvent that marks ``symbol`` to ``price`` (timezone-aware bar)."""
    p = Decimal(price)
    bar = Bar(
        symbol=symbol,
        timestamp=datetime(2024, 6, 3, tzinfo=UTC),
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal("0"),
    )
    return MarketEvent(bar=bar)


def _portfolio() -> Portfolio:
    """A synthetic portfolio: $100k start, long 10 SPY @ 400 marked to 410."""
    pf = Portfolio(Decimal("100000"))
    pf.on_fill(
        FillEvent(
            symbol=SPY,
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            fill_price=Decimal("400"),
            order_id="t1",
            broker_order_id="b1",
        )
    )
    pf.on_market(_mark(SPY, "410"))
    return pf


def test_build_status_shape_and_keys():
    status = build_status(_portfolio(), mode="paper", halted=False, generated_at=GENERATED_AT)

    assert set(status) == {
        "mode",
        "halted",
        "equity",
        "cash",
        "drawdown",
        "peak_equity",
        "positions",
        "per_strategy",
        "generated_at",
    }
    assert status["mode"] == "paper"
    assert status["halted"] is False
    assert status["generated_at"] == GENERATED_AT
    assert isinstance(status["positions"], list)
    assert isinstance(status["per_strategy"], list)


def test_money_fields_are_decimal_strings():
    status = build_status(_portfolio(), mode="paper", halted=False, generated_at=GENERATED_AT)
    for field in MONEY_FIELDS:
        assert isinstance(status[field], str), field
        # Must round-trip to a Decimal without raising (no float artifacts).
        Decimal(status[field])


def test_money_values_match_portfolio_exactly():
    pf = _portfolio()
    status = build_status(pf, mode="paper", halted=False, generated_at=GENERATED_AT)

    # 100000 - (10*400) cash = 96000; equity = 96000 + 10*410 = 100100.
    assert status["cash"] == "96000"
    assert status["equity"] == "100100"
    # No float drift: string equals str(Decimal) of the live property.
    assert status["equity"] == str(pf.equity)
    assert status["peak_equity"] == str(pf.peak_equity)
    assert status["drawdown"] == str(pf.drawdown)


def test_position_rows_shape_and_types():
    status = build_status(_portfolio(), mode="paper", halted=False, generated_at=GENERATED_AT)
    assert len(status["positions"]) == 1
    row = status["positions"][0]
    assert set(row) == set(POSITION_FIELDS)
    assert row["ticker"] == "SPY"
    # All non-ticker fields are Decimal-strings.
    for field in ("qty", "avg_entry", "current", "unrealized_pnl"):
        assert isinstance(row[field], str), field
        Decimal(row[field])
    assert row["qty"] == "10"
    assert row["avg_entry"] == "400"
    assert row["current"] == "410"
    assert row["unrealized_pnl"] == "100"  # (410-400)*10


def test_per_strategy_serialized_as_decimal_strings():
    status = build_status(
        _portfolio(),
        mode="paper",
        halted=False,
        generated_at=GENERATED_AT,
        per_strategy={"multi_asset_trend": Decimal("123.45"), "rsi2": Decimal("-7.5")},
    )
    rows = status["per_strategy"]
    assert len(rows) == 2
    for row in rows:
        assert set(row) == {"id", "pnl"}
        assert isinstance(row["id"], str)
        assert isinstance(row["pnl"], str)
        Decimal(row["pnl"])
    by_id = {r["id"]: r["pnl"] for r in rows}
    assert by_id == {"multi_asset_trend": "123.45", "rsi2": "-7.5"}


def test_per_strategy_defaults_to_empty_list():
    status = build_status(_portfolio(), mode="paper", halted=False, generated_at=GENERATED_AT)
    assert status["per_strategy"] == []


def test_empty_portfolio_has_no_positions():
    pf = Portfolio(Decimal("50000"))
    status = build_status(pf, mode="live", halted=True, generated_at=GENERATED_AT)
    assert status["positions"] == []
    assert status["mode"] == "live"
    assert status["halted"] is True
    assert status["equity"] == "50000"
    assert status["drawdown"] == "0"


def test_deterministic_same_inputs_same_document():
    a = build_status(_portfolio(), mode="paper", halted=False, generated_at=GENERATED_AT)
    b = build_status(_portfolio(), mode="paper", halted=False, generated_at=GENERATED_AT)
    assert a == b


def test_status_is_json_serializable_and_roundtrips(tmp_path):
    status = build_status(_portfolio(), mode="paper", halted=False, generated_at=GENERATED_AT)
    # Must serialize with the stdlib json encoder (i.e. no raw Decimal objects).
    text = json.dumps(status)
    assert json.loads(text) == status


def test_write_status_creates_file_and_valid_json(tmp_path):
    status = build_status(_portfolio(), mode="paper", halted=False, generated_at=GENERATED_AT)
    out = tmp_path / "nested" / "status.json"
    path = write_status(status, out)

    assert path == out
    assert out.exists()
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded == status


def test_short_position_unrealized_pnl_serialized():
    pf = Portfolio(Decimal("100000"))
    pf.on_fill(
        FillEvent(
            symbol=GLD,
            side=OrderSide.SELL,
            quantity=Decimal("5"),
            fill_price=Decimal("200"),
            order_id="s1",
            broker_order_id="b2",
        )
    )
    pf.on_market(_mark(GLD, "190"))  # short profits when price falls
    status = build_status(pf, mode="paper", halted=False, generated_at=GENERATED_AT)

    row = status["positions"][0]
    assert row["ticker"] == "GLD"
    assert row["qty"] == "-5"
    # (190-200) * -5 = 50 profit on the short.
    assert row["unrealized_pnl"] == "50"
    assert Decimal(row["unrealized_pnl"]) == pf.unrealized_pnl
