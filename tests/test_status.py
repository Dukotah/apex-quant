"""
tests/test_status.py
====================
Unit tests for scripts.status — the operator status CLI.

Tests target ONLY pure functions (render_status, _fmt_pct, _fmt_money,
_market_value_from_positions) and the DB-reading helper (_read_state) via a
real SQLite temp file.  No network, no backtest, no real DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.status import (
    GATE_DAYS,
    _fmt_money,
    _fmt_pct,
    _market_value_from_positions,
    _read_state,
    render_status,
)

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

UTC = timezone.utc


def _base_state(**overrides) -> dict:
    """Minimal valid state dict for render_status tests."""
    base = {
        "mode": "paper",
        "broker": "alpaca",
        "apex_halt_env": "",
        "halt_persisted": False,
        "positions": {},
        "cash": "90000",
        "equity": "100000",
        "peak_equity": "110000",
        "first_ts": "2024-01-01T00:00:00",
        "last_ts": "2024-01-15T00:00:00",
        "total_runs": 15,
    }
    base.update(overrides)
    return base


def _seed_db(db_path: Path, rows: list[dict]) -> None:
    """Seed a SQLite DB with the same schema as StateStore."""
    import json
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            ts            TEXT NOT NULL,
            mode          TEXT NOT NULL,
            equity        REAL NOT NULL,
            num_positions INTEGER NOT NULL,
            orders        INTEGER NOT NULL,
            fills         INTEGER NOT NULL,
            halted        INTEGER NOT NULL,
            positions     TEXT NOT NULL,
            PRIMARY KEY (ts, mode)
        )
        """
    )
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?)",
            (
                row["ts"],
                row["mode"],
                float(row["equity"]),
                row.get("num_positions", 0),
                row.get("orders", 0),
                row.get("fills", 0),
                1 if row.get("halted", False) else 0,
                json.dumps(row.get("positions", {})),
            ),
        )
    conn.commit()
    conn.close()


# -----------------------------------------------------------------------
# _fmt_pct
# -----------------------------------------------------------------------


def test_fmt_pct_positive():
    assert _fmt_pct(Decimal("0.05")) == "+5.00%"


def test_fmt_pct_negative():
    assert _fmt_pct(Decimal("-0.03")) == "-3.00%"


def test_fmt_pct_zero():
    assert _fmt_pct(Decimal("0")) == "+0.00%"


def test_fmt_pct_small_drawdown():
    # 1 % drawdown
    assert _fmt_pct(Decimal("0.01")) == "+1.00%"


# -----------------------------------------------------------------------
# _fmt_money
# -----------------------------------------------------------------------


def test_fmt_money_large():
    result = _fmt_money(Decimal("123456.78"))
    assert "123,456.78" in result
    assert result.startswith("$")


def test_fmt_money_zero():
    result = _fmt_money(Decimal("0"))
    assert "$" in result
    assert "0.00" in result


def test_fmt_money_negative():
    result = _fmt_money(Decimal("-500.00"))
    assert "$" in result
    assert "-500.00" in result


# -----------------------------------------------------------------------
# _market_value_from_positions
# -----------------------------------------------------------------------


def test_market_value_empty():
    assert _market_value_from_positions({}) == Decimal("0")


def test_market_value_single_position():
    positions = {"SPY": {"qty": "10", "avg_entry_price": "400", "current_price": "420"}}
    val = _market_value_from_positions(positions)
    assert val == Decimal("4200")  # 10 * 420


def test_market_value_multiple_positions():
    positions = {
        "SPY": {"qty": "5", "current_price": "400"},
        "TLT": {"qty": "3", "current_price": "100"},
    }
    val = _market_value_from_positions(positions)
    assert val == Decimal("2300")  # 5*400 + 3*100


def test_market_value_bad_entry_survives():
    """A corrupt entry must not crash — it contributes 0."""
    positions = {
        "SPY": {"qty": "10", "current_price": "420"},
        "BAD": {"qty": "not-a-number", "current_price": "100"},
    }
    val = _market_value_from_positions(positions)
    assert val == Decimal("4200")


# -----------------------------------------------------------------------
# render_status — header / mode / broker
# -----------------------------------------------------------------------


def test_render_status_header():
    out = render_status(_base_state())
    assert "APEX QUANT" in out
    assert "SYSTEM STATUS" in out


def test_render_status_shows_mode_and_broker():
    out = render_status(_base_state(mode="live", broker="alpaca"))
    assert "live" in out
    assert "alpaca" in out


# -----------------------------------------------------------------------
# render_status — kill/halt display
# -----------------------------------------------------------------------


def test_render_status_no_halt():
    out = render_status(_base_state(apex_halt_env="", halt_persisted=False))
    assert "kill/halt" in out
    assert "off" in out
    assert "HALTED" not in out


def test_render_status_env_halt():
    out = render_status(_base_state(apex_halt_env="1", halt_persisted=False))
    assert "APEX_HALT env" in out
    assert "HALTED" in out


def test_render_status_persisted_halt():
    out = render_status(_base_state(apex_halt_env="", halt_persisted=True))
    assert "persisted" in out
    assert "HALTED" in out


def test_render_status_both_halts():
    out = render_status(_base_state(apex_halt_env="true", halt_persisted=True))
    assert "env + persisted" in out


@pytest.mark.parametrize("halt_val", ["1", "true", "yes", "on", "TRUE", "YES", "ON"])
def test_render_status_halt_truthy_values(halt_val):
    out = render_status(_base_state(apex_halt_env=halt_val, halt_persisted=False))
    assert "HALTED" in out


# -----------------------------------------------------------------------
# render_status — money metrics
# -----------------------------------------------------------------------


def test_render_status_equity_and_cash():
    state = _base_state(cash="85000", equity="100000", peak_equity="105000")
    out = render_status(state)
    assert "85,000.00" in out  # cash
    assert "100,000.00" in out  # equity


def test_render_status_drawdown_computed():
    # peak=110000, equity=99000 → DD = (110000-99000)/110000 ≈ 10%
    state = _base_state(equity="99000", peak_equity="110000")
    out = render_status(state)
    assert "drawdown" in out.lower()
    # 10% drawdown should appear (positive, formatted without '+')
    assert "10.00%" in out


def test_render_status_zero_drawdown_at_peak():
    state = _base_state(equity="110000", peak_equity="110000")
    out = render_status(state)
    assert "+0.00%" in out


def test_render_status_zero_peak_no_crash():
    """If peak_equity is zero, drawdown must be 0 and not blow up."""
    state = _base_state(equity="0", peak_equity="0")
    out = render_status(state)
    assert "drawdown" in out.lower()


# -----------------------------------------------------------------------
# render_status — positions table
# -----------------------------------------------------------------------


def test_render_status_no_positions():
    out = render_status(_base_state(positions={}))
    assert "no open positions" in out


def test_render_status_single_position():
    pos = {"SPY": {"qty": "10", "avg_entry_price": "400.00", "current_price": "420.00"}}
    out = render_status(_base_state(positions=pos))
    assert "SPY" in out
    assert "10.0000" in out  # qty formatted to 4dp
    # unrealised P&L = (420-400)*10 = +200
    assert "+200.00" in out


def test_render_status_negative_unrealised():
    pos = {"TLT": {"qty": "5", "avg_entry_price": "100.00", "current_price": "90.00"}}
    out = render_status(_base_state(positions=pos))
    # unrealised = (90-100)*5 = -50
    assert "-50.00" in out


def test_render_status_multiple_positions_sorted():
    pos = {
        "TLT": {"qty": "3", "avg_entry_price": "100", "current_price": "102"},
        "SPY": {"qty": "2", "avg_entry_price": "400", "current_price": "410"},
    }
    out = render_status(_base_state(positions=pos))
    # Alphabetical sort: SPY before TLT
    assert out.index("SPY") < out.index("TLT")


# -----------------------------------------------------------------------
# render_status — paper-gate progress
# -----------------------------------------------------------------------


def test_render_status_gate_progress_partial():
    state = _base_state(
        total_runs=15, first_ts="2024-01-01T00:00:00", last_ts="2024-01-15T00:00:00"
    )
    out = render_status(state)
    assert f"15/{GATE_DAYS} days" in out
    assert "more day(s) needed" in out


def test_render_status_gate_progress_complete():
    state = _base_state(
        total_runs=30, first_ts="2024-01-01T00:00:00", last_ts="2024-01-30T00:00:00"
    )
    out = render_status(state)
    assert f"30/{GATE_DAYS} days" in out
    assert "COMPLETE" in out


def test_render_status_gate_zero_runs():
    state = _base_state(total_runs=0, first_ts="", last_ts="")
    out = render_status(state)
    assert "no runs recorded" in out


def test_render_status_gate_over_30():
    """More than 30 runs still caps at COMPLETE, no negative remainder."""
    state = _base_state(
        total_runs=35, first_ts="2024-01-01T00:00:00", last_ts="2024-02-05T00:00:00"
    )
    out = render_status(state)
    assert "COMPLETE" in out
    assert "more day(s)" not in out


# -----------------------------------------------------------------------
# render_status — date span
# -----------------------------------------------------------------------


def test_render_status_run_span_dates():
    state = _base_state(
        first_ts="2024-03-01T10:00:00", last_ts="2024-03-20T10:00:00", total_runs=20
    )
    out = render_status(state)
    assert "2024-03-01" in out
    assert "2024-03-20" in out


# -----------------------------------------------------------------------
# _read_state — real SQLite temp files
# -----------------------------------------------------------------------


def test_read_state_missing_db(tmp_path):
    """Non-existent DB returns empty dict."""
    result = _read_state(tmp_path / "nonexistent.db", "paper")
    assert result == {}


def test_read_state_empty_db(tmp_path):
    """DB with no rows for the mode returns empty dict."""
    db = tmp_path / "apex_state.db"
    _seed_db(db, [])
    result = _read_state(db, "paper")
    assert result == {}


def test_read_state_basic(tmp_path, monkeypatch):
    """Single row is read correctly; mode, equity, positions, run counts match."""
    monkeypatch.setenv("APEX_BROKER", "alpaca")
    monkeypatch.delenv("APEX_HALT", raising=False)
    db = tmp_path / "apex_state.db"
    pos = {"SPY": {"qty": "5", "avg_entry_price": "400", "current_price": "420"}}
    _seed_db(
        db,
        [
            {
                "ts": "2024-01-10T00:00:00",
                "mode": "paper",
                "equity": 102000.0,
                "positions": pos,
                "halted": False,
            }
        ],
    )
    result = _read_state(db, "paper")
    assert result["mode"] == "paper"
    assert result["broker"] == "alpaca"
    assert result["total_runs"] == 1
    assert Decimal(result["equity"]) == Decimal("102000")
    assert result["halt_persisted"] is False
    assert "SPY" in result["positions"]


def test_read_state_peak_equity_is_max(tmp_path, monkeypatch):
    """peak_equity is the maximum equity seen across all runs, not just the last."""
    monkeypatch.delenv("APEX_HALT", raising=False)
    monkeypatch.delenv("APEX_BROKER", raising=False)
    db = tmp_path / "apex_state.db"
    _seed_db(
        db,
        [
            {"ts": "2024-01-01T00:00:00", "mode": "paper", "equity": 100000.0},
            {"ts": "2024-01-02T00:00:00", "mode": "paper", "equity": 110000.0},  # peak
            {"ts": "2024-01-03T00:00:00", "mode": "paper", "equity": 105000.0},  # last
        ],
    )
    result = _read_state(db, "paper")
    # Peak should be 110000, not 105000 (the last equity).
    assert Decimal(result["peak_equity"]) == Decimal("110000")


def test_read_state_halted_row(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_HALT", raising=False)
    monkeypatch.delenv("APEX_BROKER", raising=False)
    db = tmp_path / "apex_state.db"
    _seed_db(
        db, [{"ts": "2024-01-10T00:00:00", "mode": "paper", "equity": 95000.0, "halted": True}]
    )
    result = _read_state(db, "paper")
    assert result["halt_persisted"] is True


def test_read_state_first_and_last_ts(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_HALT", raising=False)
    monkeypatch.delenv("APEX_BROKER", raising=False)
    db = tmp_path / "apex_state.db"
    _seed_db(
        db,
        [
            {"ts": "2024-01-01T00:00:00", "mode": "paper", "equity": 100000.0},
            {"ts": "2024-01-05T00:00:00", "mode": "paper", "equity": 101000.0},
        ],
    )
    result = _read_state(db, "paper")
    assert result["first_ts"].startswith("2024-01-01")
    assert result["last_ts"].startswith("2024-01-05")


def test_read_state_ignores_other_mode(tmp_path, monkeypatch):
    """Rows for 'live' mode must not appear when querying 'paper'."""
    monkeypatch.delenv("APEX_HALT", raising=False)
    monkeypatch.delenv("APEX_BROKER", raising=False)
    db = tmp_path / "apex_state.db"
    _seed_db(
        db,
        [
            {"ts": "2024-01-01T00:00:00", "mode": "live", "equity": 200000.0},
        ],
    )
    result = _read_state(db, "paper")
    assert result == {}


def test_read_state_run_count(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_HALT", raising=False)
    monkeypatch.delenv("APEX_BROKER", raising=False)
    db = tmp_path / "apex_state.db"
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = [
        {"ts": (base + timedelta(days=i)).isoformat(), "mode": "paper", "equity": 100000.0 + i}
        for i in range(10)
    ]
    _seed_db(db, rows)
    result = _read_state(db, "paper")
    assert result["total_runs"] == 10
