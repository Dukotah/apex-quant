"""
Tests for scripts.export_status — the apex-trader status snapshot exporter.

Builds a snapshot from a known synthetic portfolio + state and asserts:
  * the exact top-level keys exist and nest correctly (the TS contract),
  * the whole thing is JSON-serializable (json.dumps round-trips, no NaN/Inf),
  * percentages are FRACTIONS, not whole percents,
  * enums fall within the allowed unions, and
  * hand-computed values (dayPnlPct, weightPct, currentDrawdownPct, unrealizedPnlPct,
    equity-curve drawdown) match exact arithmetic.

Pure/deterministic: every test injects an explicit ``now`` — no wall-clock.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import Decimal

from apex.core.events import FillEvent, MarketEvent
from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.risk.portfolio import Portfolio
from apex.validation.drift_monitor import DriftMonitor
from scripts.export_status import build_status, build_status_from_store
from scripts.run_once import RunReport, StateStore

UTC = timezone.utc
NOW = datetime(2024, 6, 3, 20, 0, tzinfo=UTC)
SPY = Symbol("SPY", AssetClass.ETF)

# Contract allowed-value sets (mirrors apex-trader src/lib/types.ts).
MODES = {"backtest", "paper", "live"}
STRAT_STATUS = {"live", "paper", "research", "quarantined", "retired"}
DRIFT_STATUS = {"ok", "warn", "quarantined"}
GATE_STATUS = {"PASS", "WARN", "FAIL"}
ALERT_LEVELS = {"info", "warn", "critical"}
SIDES = {"BUY", "SELL"}

TOP_LEVEL_KEYS = {
    "generatedAt",
    "mode",
    "halted",
    "haltReason",
    "account",
    "equityCurve",
    "positions",
    "trades",
    "strategies",
    "paperGate",
    "gauntlet",
    "alerts",
}
ACCOUNT_KEYS = {
    "equity",
    "cash",
    "buyingPower",
    "peakEquity",
    "currentDrawdownPct",
    "dailyStartEquity",
    "dayPnl",
    "dayPnlPct",
}


def _portfolio_spy_100_at_110() -> Portfolio:
    """
    100k cash, BUY 100 SPY @ 100, mark to 110.
      cash      = 100000 - 100*100 = 90000
      equity    = 90000 + 100*110  = 101000  (a new peak)
      day_start = 100000 (set at init)
    """
    p = Portfolio(Decimal("100000"))
    p.on_fill(
        FillEvent(
            symbol=SPY,
            side=OrderSide.BUY,
            quantity=Decimal("100"),
            fill_price=Decimal("100"),
            commission=Decimal("0"),
            order_id="o1",
            broker_order_id="b1",
            timestamp=NOW,
        )
    )
    p.on_market(
        MarketEvent(
            bar=Bar(
                symbol=SPY,
                timestamp=NOW,
                open=Decimal("110"),
                high=Decimal("110"),
                low=Decimal("110"),
                close=Decimal("110"),
                volume=Decimal("0"),
            )
        )
    )
    return p


def _build(**kw):
    base = dict(
        now=NOW,
        mode="paper",
        portfolio=_portfolio_spy_100_at_110(),
        equity_history=[100000.0, 101000.0],
        timestamp_history=["2024-06-02T20:00:00+00:00", "2024-06-03T20:00:00+00:00"],
    )
    base.update(kw)
    return build_status(**base)


# ------------------------------------------------------------------ structure


def test_top_level_keys_exact():
    snap = _build()
    assert set(snap.keys()) == TOP_LEVEL_KEYS
    assert set(snap["account"].keys()) == ACCOUNT_KEYS


def test_json_round_trips_no_nan():
    snap = _build()
    # allow_nan=False rejects NaN/Infinity — proves the payload is valid JSON.
    text = json.dumps(snap, allow_nan=False)
    again = json.loads(text)
    assert again["mode"] == "paper"
    # Every numeric leaf must be finite.
    for v in again["account"].values():
        assert isinstance(v, (int, float)) and math.isfinite(v)


def test_enums_within_allowed_sets():
    snap = _build()
    assert snap["mode"] in MODES
    for s in snap["strategies"]:
        assert s["status"] in STRAT_STATUS
        if "driftStatus" in s:
            assert s["driftStatus"] in DRIFT_STATUS
    for g in snap["gauntlet"]:
        for gate in g["gates"]:
            assert gate["status"] in GATE_STATUS
    for a in snap["alerts"]:
        assert a["level"] in ALERT_LEVELS
    for t in snap["trades"]:
        assert t["side"] in SIDES


def test_generated_at_is_iso():
    snap = _build()
    # Round-trips through fromisoformat without error.
    assert datetime.fromisoformat(snap["generatedAt"]) == NOW


# --------------------------------------------------------------- computed math


def test_account_hand_computed_values():
    snap = _build()
    acct = snap["account"]
    assert acct["equity"] == 101000.0
    assert acct["cash"] == 90000.0
    assert acct["peakEquity"] == 101000.0
    assert acct["dailyStartEquity"] == 100000.0
    assert acct["dayPnl"] == 1000.0
    # FRACTION not percent: 1000 / 100000 = 0.01, NOT 1.0.
    assert abs(acct["dayPnlPct"] - 0.01) < 1e-12
    # At a fresh peak -> zero drawdown.
    assert acct["currentDrawdownPct"] == 0.0
    # buyingPower defaults to cash (fail-closed, no margin).
    assert acct["buyingPower"] == 90000.0


def test_position_hand_computed_values():
    snap = _build()
    assert len(snap["positions"]) == 1
    pos = snap["positions"][0]
    assert pos["ticker"] == "SPY"
    assert pos["assetClass"] == "etf"
    assert pos["quantity"] == 100.0
    assert pos["avgPrice"] == 100.0
    assert pos["lastPrice"] == 110.0
    assert pos["marketValue"] == 11000.0
    assert pos["unrealizedPnl"] == 1000.0
    # FRACTIONS: 1000/10000 = 0.10 ; 11000/101000 ≈ 0.1089.
    assert abs(pos["unrealizedPnlPct"] - 0.10) < 1e-12
    assert abs(pos["weightPct"] - (11000.0 / 101000.0)) < 1e-12
    assert pos["strategyId"] == "multi_asset_trend"


def test_equity_curve_drawdown_fraction():
    # A peak then a 10% dip: 100 -> 110 -> 99 gives drawdown 0, 0, (110-99)/110=0.1.
    snap = _build(
        equity_history=[100.0, 110.0, 99.0],
        timestamp_history=[
            "2024-06-01T00:00:00+00:00",
            "2024-06-02T00:00:00+00:00",
            "2024-06-03T00:00:00+00:00",
        ],
    )
    curve = snap["equityCurve"]
    assert len(curve) == 3
    assert curve[0]["drawdownPct"] == 0.0
    assert curve[1]["drawdownPct"] == 0.0
    assert abs(curve[2]["drawdownPct"] - (11.0 / 110.0)) < 1e-12
    assert curve[2]["equity"] == 99.0


def test_drawdown_when_below_peak():
    # Equity 90000 with peak 100000 -> drawdown 0.10 (a fraction).
    p = Portfolio(Decimal("100000"))
    p.on_fill(
        FillEvent(
            symbol=SPY,
            side=OrderSide.BUY,
            quantity=Decimal("100"),
            fill_price=Decimal("100"),
            commission=Decimal("0"),
            order_id="o",
            broker_order_id="b",
            timestamp=NOW,
        )
    )
    # Mark down to 0 value would be extreme; instead drop SPY to 0 net -> equity 90000.
    p.on_market(
        MarketEvent(
            bar=Bar(
                symbol=SPY,
                timestamp=NOW,
                open=Decimal("0.01"),
                high=Decimal("0.01"),
                low=Decimal("0.01"),
                close=Decimal("0.01"),
                volume=Decimal("0"),
            )
        )
    )
    # peak was 100000 at init; equity now 90000 + 100*0.01 = 90001.
    snap = build_status(
        now=NOW,
        mode="paper",
        portfolio=p,
        equity_history=[100000.0, 90001.0],
        timestamp_history=["2024-06-02T00:00:00+00:00", "2024-06-03T00:00:00+00:00"],
    )
    dd = snap["account"]["currentDrawdownPct"]
    assert abs(dd - (100000.0 - 90001.0) / 100000.0) < 1e-9


# ------------------------------------------------------------------- sections


def test_halt_surfaces_in_flag_reason_and_alert():
    snap = _build(halted=True, halt_reason="max drawdown breached")
    assert snap["halted"] is True
    assert snap["haltReason"] == "max drawdown breached"
    crit = [a for a in snap["alerts"] if a["level"] == "critical" and a["kind"] == "halt"]
    assert crit and crit[0]["message"] == "max drawdown breached"


def test_halt_reason_null_when_not_halted():
    snap = _build()
    assert snap["halted"] is False
    assert snap["haltReason"] is None


def test_drift_quarantine_sets_strategy_status_and_alert():
    # Force a quarantine reading: steady losses below the floor after warmup.
    mon = DriftMonitor("multi_asset_trend", validated_sharpe=1.0, window=3, min_observations=3)
    reading = None
    for eq in [100.0, 90.0, 81.0, 72.9, 65.6]:
        reading = mon.record_equity(eq)
    assert reading.is_quarantined
    snap = _build(drift=reading)
    assert snap["strategies"][0]["status"] == "quarantined"
    assert snap["strategies"][0]["driftStatus"] == "quarantined"
    assert any(a["kind"] == "drift" and a["level"] == "critical" for a in snap["alerts"])


def test_paper_gate_fields_and_fraction_semantics():
    snap = _build()
    pg = snap["paperGate"]
    assert set(pg.keys()) == {
        "startDate",
        "daysElapsed",
        "daysRequired",
        "rollingSharpe",
        "targetSharpe",
        "withinBacktestBand",
    }
    assert pg["daysElapsed"] == 2
    assert pg["daysRequired"] == 30
    assert isinstance(pg["withinBacktestBand"], bool)
    datetime.fromisoformat(pg["startDate"])  # valid ISO


def test_empty_inputs_still_valid_json():
    # No history, no positions, no gauntlet -> still a valid, complete snapshot.
    p = Portfolio(Decimal("50000"))
    snap = build_status(now=NOW, mode="backtest", portfolio=p)
    json.dumps(snap, allow_nan=False)  # must not raise
    assert snap["equityCurve"] == []
    assert snap["positions"] == []
    assert snap["trades"] == []
    assert snap["gauntlet"] == []
    assert snap["strategies"][0]["status"] == "research"  # backtest mode
    assert snap["account"]["equity"] == 50000.0


def test_trades_normalized_and_optional_pnl():
    trades = [
        {
            "id": "t1",
            "timestamp": NOW,
            "ticker": "SPY",
            "side": "buy",
            "quantity": 10,
            "price": 100,
            "notional": 1000,
            "commission": 0,
            "strategyId": "multi_asset_trend",
            "reason": "entry",
        },
        {
            "id": "t2",
            "timestamp": NOW,
            "ticker": "SPY",
            "side": "sell",
            "quantity": 10,
            "price": 110,
            "notional": 1100,
            "commission": 0,
            "strategyId": "multi_asset_trend",
            "reason": "exit",
            "pnl": 100,
        },
    ]
    snap = _build(trades=trades)
    rows = snap["trades"]
    assert len(rows) == 2
    assert rows[0]["side"] == "BUY" and "pnl" not in rows[0]
    assert rows[1]["side"] == "SELL" and rows[1]["pnl"] == 100.0
    json.dumps(snap, allow_nan=False)


# ----------------------------------------------------------- store integration


def test_build_from_store_round_trips(tmp_path):
    store = StateStore(tmp_path / "s.db")
    base = datetime(2024, 1, 1, tzinfo=UTC)
    for i, eq in enumerate([100000, 100500, 101000]):
        store.save_run(
            RunReport(
                timestamp=base.replace(day=i + 1),
                mode="paper",
                equity=float(eq),
                num_positions=1,
                orders_submitted=1,
            ),
            {"SPY": {"qty": "8", "avg_entry_price": "100", "current_price": "110"}},
        )
    snap = build_status_from_store(store, now=NOW, mode="paper")
    json.dumps(snap, allow_nan=False)
    assert set(snap.keys()) == TOP_LEVEL_KEYS
    assert len(snap["equityCurve"]) == 3
    # Reconstructed position from the latest row.
    assert len(snap["positions"]) == 1
    assert snap["positions"][0]["ticker"] == "SPY"
    assert snap["positions"][0]["quantity"] == 8.0
    assert snap["positions"][0]["lastPrice"] == 110.0
    store.close()


def test_build_from_empty_store_is_valid(tmp_path):
    store = StateStore(tmp_path / "empty.db")
    snap = build_status_from_store(store, now=NOW, mode="paper")
    json.dumps(snap, allow_nan=False)
    assert set(snap.keys()) == TOP_LEVEL_KEYS
    assert snap["equityCurve"] == []
    assert snap["positions"] == []
    store.close()
