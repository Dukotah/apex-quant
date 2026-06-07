"""
scripts/export_status.py
========================
Write a read-only JSON status snapshot for the apex-trader dashboard.

apex-trader (the Next.js control surface) cannot reach into this engine's
process, so this script serializes the current portfolio state to a small,
stable JSON file it can poll. It is purely an export: it NEVER touches the
broker, places orders, or mutates portfolio state.

Schema (state/status.json):
    {
      "mode":        str,                # "paper" | "live" | "backtest"
      "halted":      bool,               # risk manager / kill switch tripped
      "equity":      str,                # Decimal as string (no float drift)
      "cash":        str,
      "drawdown":    str,                # fraction from peak, 0..1
      "peak_equity": str,
      "positions": [
        {"ticker": str, "qty": str, "avg_entry": str,
         "current": str, "unrealized_pnl": str},
        ...
      ],
      "per_strategy": [
        {"id": str, "pnl": str},
        ...
      ],
      "generated_at": str                # ISO-8601 timestamp
    }

Money/prices/quantities are emitted as Decimal->str so the dashboard never
sees float rounding artifacts. The snapshot-building function is deterministic:
``generated_at`` is passed in (or read from state) rather than read from the
wall clock, so the same inputs always produce the same document.

Run:  python -m scripts.export_status            # default state DB -> state/status.json
      python -m scripts.export_status --mode paper --out state/status.json
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from apex.risk.portfolio import Portfolio

DEFAULT_OUT_PATH = Path("state/status.json")


def build_status(
    portfolio: Portfolio,
    *,
    mode: str,
    halted: bool,
    generated_at: str,
    per_strategy: Optional[Mapping[str, Decimal]] = None,
) -> Dict:
    """
    Build the status snapshot document from a portfolio.

    Pure and deterministic: it reads only the portfolio's public read-only
    snapshot and the values passed in. ``generated_at`` is supplied by the
    caller (never read from the wall clock here) so the function is fully
    reproducible. All money/price/quantity fields are serialized as
    ``Decimal`` -> ``str`` to avoid float drift on the dashboard side.

    Args:
        portfolio:    The position/cash/equity/drawdown tracker to snapshot.
        mode:         Execution mode label ("paper" / "live" / "backtest").
        halted:       Whether the system is halted (risk breach / kill switch).
        generated_at: ISO-8601 timestamp string stamped into the document.
        per_strategy: Optional mapping of strategy id -> realized/total P&L
                      (Decimal). The portfolio does not track P&L per strategy,
                      so the caller supplies it; omitted -> empty list.
    """
    positions: List[Dict[str, str]] = [
        {
            "ticker": ticker,
            "qty": str(pos.quantity),
            "avg_entry": str(pos.avg_entry_price),
            "current": str(pos.current_price),
            "unrealized_pnl": str(pos.unrealized_pnl),
        }
        for ticker, pos in portfolio.open_positions.items()
    ]

    per_strategy_rows: List[Dict[str, str]] = [
        {"id": sid, "pnl": str(pnl)} for sid, pnl in (per_strategy or {}).items()
    ]

    return {
        "mode": mode,
        "halted": bool(halted),
        "equity": str(portfolio.equity),
        "cash": str(portfolio.cash),
        "drawdown": str(portfolio.drawdown),
        "peak_equity": str(portfolio.peak_equity),
        "positions": positions,
        "per_strategy": per_strategy_rows,
        "generated_at": generated_at,
    }


def write_status(status: Mapping, out: str | Path = DEFAULT_OUT_PATH) -> Path:
    """
    Serialize a status document to ``out`` as pretty JSON and return the path.

    Creates the parent directory if needed. The write is the only side effect;
    building the document (``build_status``) is pure.
    """
    path = Path(out)
    if str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------- main wiring


def _portfolio_from_state(  # pragma: no cover - reads the real state DB
    mode: str,
) -> tuple[Portfolio, bool, str]:
    """
    Rebuild a portfolio snapshot from the last persisted run in the state DB.

    Returns (portfolio, halted, generated_at). ``generated_at`` is read from the
    persisted run timestamp — NOT the wall clock — so the export reflects the
    state as of the last cron cycle and stays deterministic.
    """
    from apex.core.config import AppConfig
    from apex.core.events import FillEvent
    from apex.core.models import AssetClass, OrderSide, Symbol
    from scripts.run_once import StateStore

    store = StateStore()
    row = store.last_run()
    config = AppConfig.from_env()
    portfolio = Portfolio(config.initial_capital)

    if row is None:
        return portfolio, False, ""

    # Reconstruct holdings from the persisted JSON snapshot using only the
    # portfolio's public fill API (a synthetic fill at avg entry, then marked
    # to the stored current price) — never reaching into private state.
    positions = json.loads(row["positions"])
    for ticker, p in positions.items():
        qty = Decimal(str(p["qty"]))
        if qty == 0:
            continue
        symbol = Symbol(ticker, AssetClass.ETF)
        avg = Decimal(str(p["avg_entry_price"]))
        cur = Decimal(str(p.get("current_price", p["avg_entry_price"])))
        side = OrderSide.BUY if qty > 0 else OrderSide.SELL
        portfolio.on_fill(
            FillEvent(
                symbol=symbol,
                side=side,
                quantity=abs(qty),
                fill_price=avg,
                commission=Decimal("0"),
                slippage=Decimal("0"),
                order_id="export",
                broker_order_id="export",
                timestamp=None,
                is_paper=True,
            )
        )
        portfolio.on_market(_mark_event(symbol, cur, row["ts"]))

    return portfolio, bool(row["halted"]), str(row["ts"])


def _mark_event(symbol, price: Decimal, ts: str):  # pragma: no cover - live wiring
    """Build a MarketEvent that marks ``symbol`` to ``price`` at the stored ts."""
    from datetime import datetime

    from apex.core.events import MarketEvent
    from apex.core.models import Bar

    when = datetime.fromisoformat(ts)
    bar = Bar(
        symbol=symbol,
        timestamp=when,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("0"),
    )
    return MarketEvent(bar=bar)


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - wiring
    parser = argparse.ArgumentParser(description="Export a read-only status snapshot JSON.")
    parser.add_argument("--mode", default="paper", help="execution mode label")
    parser.add_argument("--out", default=str(DEFAULT_OUT_PATH), help="output JSON path")
    args = parser.parse_args(argv)

    portfolio, halted, generated_at = _portfolio_from_state(args.mode)
    status = build_status(
        portfolio,
        mode=args.mode,
        halted=halted,
        generated_at=generated_at,
    )
    path = write_status(status, args.out)
    print(f"Wrote status snapshot -> {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
