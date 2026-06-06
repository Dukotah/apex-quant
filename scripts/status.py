"""
scripts/status.py
=================
Operator status CLI — one command for a clean one-screen read of the live system.

Prints:
  - mode (APEX_MODE), broker (APEX_BROKER), halt/kill state
  - open positions (ticker, qty, avg entry, current price, unrealised P&L)
  - cash, equity, peak equity, current drawdown
  - paper-gate progress (day N of 30 since the first recorded state)

The DB is opened read-only; if it is missing, a friendly message is shown
and the script exits 0.  Money is always Decimal — never float arithmetic.

Run:
    python -m scripts.status
    APEX_MODE=paper python -m scripts.status
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

from scripts.run_once import DEFAULT_STATE_PATH

# -----------------------------------------------------------------------
# constants
# -----------------------------------------------------------------------

GATE_DAYS = 30  # Rule 17 — 30+ day paper period before live capital
_ZERO = Decimal("0")


# -----------------------------------------------------------------------
# pure formatting helpers (these are the testable units)
# -----------------------------------------------------------------------


def _fmt_pct(value: Decimal) -> str:
    """Format a Decimal fraction (0.05 → '+5.00%', -0.03 → '-3.00%')."""
    pct = value * 100
    sign = "+" if pct >= _ZERO else ""
    return f"{sign}{pct:.2f}%"


def _fmt_money(value: Decimal) -> str:
    """Format a Decimal dollar amount with commas and two decimals."""
    return f"${value:>14,.2f}"


def render_status(state: dict) -> str:
    """
    Pure function: build the status report string from a plain dict.

    Expected keys (all optional — missing keys degrade gracefully):
      mode          str   e.g. "paper"
      broker        str   e.g. "alpaca"
      apex_halt_env str   raw APEX_HALT env value
      halt_persisted bool True if the last DB row has halted=1
      positions     dict  {ticker: {"qty": str, "avg_entry_price": str,
                                    "current_price": str}}
      cash          str   Decimal-serialised cash
      equity        str   Decimal-serialised current equity
      peak_equity   str   Decimal-serialised peak equity (max ever seen)
      first_ts      str   ISO timestamp of the first recorded run
      last_ts       str   ISO timestamp of the most recent run
      total_runs    int   total DB rows for this mode
    """
    mode = state.get("mode", "unknown")
    broker = state.get("broker", "unknown")

    # Kill / halt state
    apex_halt_raw = state.get("apex_halt_env", "")
    halt_env_active = apex_halt_raw.strip().lower() in ("1", "true", "yes", "on")
    halt_persisted = bool(state.get("halt_persisted", False))
    halt_any = halt_env_active or halt_persisted

    kill_label: str
    if halt_env_active and halt_persisted:
        kill_label = "ACTIVE (env + persisted)"
    elif halt_env_active:
        kill_label = "ACTIVE (APEX_HALT env)"
    elif halt_persisted:
        kill_label = "ACTIVE (persisted from last run)"
    else:
        kill_label = "off"

    # Money
    cash = Decimal(str(state.get("cash", "0")))
    equity = Decimal(str(state.get("equity", "0")))
    peak_equity = Decimal(str(state.get("peak_equity", "0")))

    drawdown: Decimal
    if peak_equity > _ZERO:
        drawdown = max(_ZERO, (peak_equity - equity) / peak_equity)
    else:
        drawdown = _ZERO

    # Positions
    positions: dict = state.get("positions", {})

    # Paper-gate progress
    total_runs: int = int(state.get("total_runs", 0))
    first_ts: str = str(state.get("first_ts", ""))
    last_ts: str = str(state.get("last_ts", ""))
    gate_frac = min(1.0, total_runs / GATE_DAYS) if GATE_DAYS > 0 else 0.0
    gate_bar_width = 24
    filled = max(0, min(gate_bar_width, round(gate_frac * gate_bar_width)))
    gate_bar = "█" * filled + "░" * (gate_bar_width - filled)

    lines: list[str] = [
        "APEX QUANT — SYSTEM STATUS",
        "=" * 56,
        f"  mode         {mode}",
        f"  broker       {broker}",
        f"  kill/halt    {kill_label}",
        "-" * 56,
        f"  cash         {_fmt_money(cash)}",
        f"  equity       {_fmt_money(equity)}",
        f"  peak equity  {_fmt_money(peak_equity)}",
        f"  drawdown     {_fmt_pct(drawdown)}",
        "-" * 56,
    ]

    if positions:
        lines.append(
            f"  {'TICKER':<8}  {'QTY':>10}  {'AVG ENTRY':>12}  {'CUR PRICE':>12}  {'UNREAL P&L':>13}"
        )
        for ticker, pos in sorted(positions.items()):
            qty = Decimal(str(pos.get("qty", "0")))
            avg = Decimal(str(pos.get("avg_entry_price", "0")))
            cur = Decimal(str(pos.get("current_price", "0")))
            unreal = (cur - avg) * qty
            unreal_str = f"{float(unreal):+,.2f}"
            lines.append(
                f"  {ticker:<8}  {qty:>10.4f}  {avg:>12,.4f}  {cur:>12,.4f}  {unreal_str:>13}"
            )
    else:
        lines.append("  no open positions")

    lines.append("-" * 56)

    if total_runs > 0:
        span = f"{first_ts[:10]} .. {last_ts[:10]}" if first_ts and last_ts else "n/a"
        lines.append(f"  paper gate   {gate_bar}  {total_runs}/{GATE_DAYS} days")
        lines.append(f"  run span     {span}")
        if total_runs >= GATE_DAYS:
            lines.append("  gate status  30-day window COMPLETE")
        else:
            remaining = GATE_DAYS - total_runs
            lines.append(f"  gate status  {remaining} more day(s) needed")
    else:
        lines.append("  paper gate   no runs recorded yet")

    if halt_any:
        lines += [
            "=" * 56,
            "  *** SYSTEM IS HALTED — no new orders will be placed ***",
        ]

    return "\n".join(lines)


# -----------------------------------------------------------------------
# DB reader — builds the state dict from SQLite
# -----------------------------------------------------------------------


def _read_state(db_path: Path, mode: str) -> dict:
    """
    Open the state DB read-only and assemble the status dict.
    Returns an empty dict if the DB has no rows for *mode*.
    Does NOT raise — errors are surfaced as empty/partial dicts.
    """
    # Use immutable=1 URI so SQLite never tries to write a WAL/journal.
    uri = f"file:{db_path}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return {}

    try:
        # Most recent row for this mode — gives equity, positions, halted.
        cur = conn.execute("SELECT * FROM runs WHERE mode = ? ORDER BY ts DESC LIMIT 1", (mode,))
        last = cur.fetchone()
        if last is None:
            return {}

        # Oldest row for gate-progress calculation.
        cur2 = conn.execute("SELECT ts FROM runs WHERE mode = ? ORDER BY ts ASC LIMIT 1", (mode,))
        first = cur2.fetchone()
        first_ts = first["ts"] if first else last["ts"]

        # Total run count for this mode.
        total_runs = conn.execute("SELECT COUNT(*) FROM runs WHERE mode = ?", (mode,)).fetchone()[0]

        # Peak equity: max equity ever seen for this mode.
        peak_row = conn.execute("SELECT MAX(equity) FROM runs WHERE mode = ?", (mode,)).fetchone()
        peak_equity_float: float = (
            peak_row[0] if peak_row and peak_row[0] is not None else float(last["equity"])
        )

        # Positions JSON from the last row.
        positions_raw: dict = {}
        try:
            positions_raw = json.loads(last["positions"])
        except (json.JSONDecodeError, TypeError):
            pass

        return {
            "mode": mode,
            "broker": os.getenv("APEX_BROKER", "unknown"),
            "apex_halt_env": os.getenv("APEX_HALT", ""),
            "halt_persisted": bool(int(last["halted"])),
            "positions": positions_raw,
            # DB stores equity as REAL (float); convert precisely via str.
            "cash": str(Decimal(str(last["equity"])) - _market_value_from_positions(positions_raw)),
            "equity": str(Decimal(str(last["equity"]))),
            "peak_equity": str(Decimal(str(peak_equity_float))),
            "first_ts": first_ts,
            "last_ts": last["ts"],
            "total_runs": total_runs,
        }
    finally:
        conn.close()


def _market_value_from_positions(positions: dict) -> Decimal:
    """
    Estimate total market value of open positions from the persisted snapshot.
    Each position dict has qty and current_price; their product is market value.
    Falls back to zero on any parse error (safe — we never over-count).
    """
    total = _ZERO
    for pos in positions.values():
        try:
            qty = Decimal(str(pos.get("qty", "0")))
            cur = Decimal(str(pos.get("current_price", "0")))
            total += qty * cur
        except Exception:  # noqa: BLE001
            pass
    return total


# -----------------------------------------------------------------------
# main
# -----------------------------------------------------------------------


def main() -> int:
    """Entry point. Reads mode from APEX_MODE env, opens DB read-only, prints status."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    mode = os.getenv("APEX_MODE", "paper")
    db_path = Path(os.getenv("APEX_STATE_DB", str(DEFAULT_STATE_PATH)))

    if not db_path.exists():
        print("No state yet — the bot hasn't run a cycle. Start with: python -m scripts.run_once")
        return 0

    state = _read_state(db_path, mode)
    if not state:
        state = {
            "mode": mode,
            "broker": os.getenv("APEX_BROKER", "unknown"),
            "apex_halt_env": os.getenv("APEX_HALT", ""),
            "halt_persisted": False,
            "positions": {},
            "cash": "0",
            "equity": "0",
            "peak_equity": "0",
            "total_runs": 0,
        }

    print(render_status(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
