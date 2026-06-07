"""
scripts/risk_dashboard.py
=========================
Read-only risk snapshot. Turns the current portfolio state (the positions and
equity persisted by ``run_once`` into the state DB) into a plain-text risk
dashboard you can eyeball at a glance: gross/net exposure, leverage, per-symbol
concentration (and the biggest single name), long/short tilt, and cash buffer.

It answers the one question a risk monitor exists to answer — "how much am I
actually on the hook for, and is any one bet too big?" — without touching the
broker or the network.

Run:  python -m scripts.risk_dashboard                # latest paper snapshot
      python -m scripts.risk_dashboard --mode live    # a different mode
      python -m scripts.risk_dashboard --db state/apex_state.db

Pure read-only: it reads the local audit DB only, never the broker, and sends
nothing. Importing this module has ZERO side effects — all state/config access is
lazy-imported INSIDE functions, and the analytics core is a pure, deterministic
function that takes its inputs (including the timestamp) by argument.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence

# Money/quantity math is Decimal per the golden rules.
_ZERO = Decimal("0")


# --------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class PositionSnapshot:
    """One open holding, as read from the state store (all values as Decimal)."""

    ticker: str
    quantity: Decimal  # negative = short
    avg_entry_price: Decimal
    current_price: Decimal

    @property
    def market_value(self) -> Decimal:
        """Signed notional (negative for shorts)."""
        return self.quantity * self.current_price

    @property
    def gross_value(self) -> Decimal:
        """Absolute notional deployed (always >= 0)."""
        return abs(self.market_value)

    @property
    def is_short(self) -> bool:
        return self.quantity < _ZERO


# --------------------------------------------------------------------- outputs


@dataclass(frozen=True)
class SymbolExposure:
    """Per-symbol exposure line in the snapshot."""

    ticker: str
    gross_value: Decimal
    signed_value: Decimal
    concentration: Decimal  # gross_value / equity, 0 when equity <= 0
    is_short: bool


@dataclass(frozen=True)
class RiskSnapshot:
    """The full computed risk picture for one moment. Immutable, JSON-friendly via str()."""

    timestamp: datetime
    mode: str
    equity: Decimal
    cash: Decimal
    num_positions: int
    gross_exposure: Decimal  # sum |market_value|
    net_exposure: Decimal  # sum market_value (longs - shorts)
    long_exposure: Decimal
    short_exposure: Decimal  # reported as a positive magnitude
    gross_leverage: Decimal  # gross_exposure / equity (0 if equity <= 0)
    net_leverage: Decimal  # net_exposure / equity (0 if equity <= 0)
    cash_pct: Decimal  # cash / equity (0 if equity <= 0)
    largest_concentration: Decimal  # biggest single-name gross / equity
    largest_ticker: Optional[str]
    per_symbol: List[SymbolExposure] = field(default_factory=list)


# ------------------------------------------------------------------- pure core


def compute_risk_snapshot(
    positions: Sequence[PositionSnapshot],
    *,
    equity: Decimal,
    cash: Decimal,
    timestamp: datetime,
    mode: str = "paper",
) -> RiskSnapshot:
    """
    Pure, deterministic core: derive the full risk picture from a set of positions
    and the account totals. No I/O, no clock, no globals — ``timestamp`` is injected
    so the same inputs always yield the same snapshot.

    Insufficient data is handled gracefully (fail closed): with no positions you get
    a flat snapshot; with non-positive equity, every ratio is reported as 0 rather
    than dividing by zero or emitting garbage.

    Per-symbol lines are sorted by gross exposure descending, then ticker ascending,
    so the ordering is stable regardless of the input order.
    """
    gross = sum((p.gross_value for p in positions), _ZERO)
    net = sum((p.market_value for p in positions), _ZERO)
    long_exp = sum((p.market_value for p in positions if not p.is_short), _ZERO)
    short_exp = sum((p.gross_value for p in positions if p.is_short), _ZERO)

    def _ratio(numer: Decimal) -> Decimal:
        return numer / equity if equity > _ZERO else _ZERO

    per_symbol = [
        SymbolExposure(
            ticker=p.ticker,
            gross_value=p.gross_value,
            signed_value=p.market_value,
            concentration=_ratio(p.gross_value),
            is_short=p.is_short,
        )
        for p in positions
    ]
    per_symbol.sort(key=lambda s: (-s.gross_value, s.ticker))

    largest = per_symbol[0] if per_symbol else None

    return RiskSnapshot(
        timestamp=timestamp,
        mode=mode,
        equity=equity,
        cash=cash,
        num_positions=len(positions),
        gross_exposure=gross,
        net_exposure=net,
        long_exposure=long_exp,
        short_exposure=short_exp,
        gross_leverage=_ratio(gross),
        net_leverage=_ratio(net),
        cash_pct=_ratio(cash),
        largest_concentration=largest.concentration if largest else _ZERO,
        largest_ticker=largest.ticker if largest else None,
        per_symbol=per_symbol,
    )


# ----------------------------------------------------------------- rendering


def render_snapshot(snap: RiskSnapshot) -> str:
    """Format a RiskSnapshot as the plain-text dashboard. Pure: snapshot -> string."""
    lines = [
        "APEX QUANT — RISK DASHBOARD",
        "=" * 56,
        f"  mode {snap.mode}   as of {snap.timestamp:%Y-%m-%d %H:%M}Z",
        f"  equity      ${float(snap.equity):>14,.2f}",
        f"  cash        ${float(snap.cash):>14,.2f}   ({float(snap.cash_pct):>+7.1%} of equity)",
        f"  positions   {snap.num_positions:>15}",
        "-" * 56,
        f"  gross exposure ${float(snap.gross_exposure):>13,.2f}   "
        f"leverage {float(snap.gross_leverage):>6.2f}x",
        f"  net exposure   ${float(snap.net_exposure):>13,.2f}   "
        f"leverage {float(snap.net_leverage):>+6.2f}x",
        f"  long ${float(snap.long_exposure):>13,.2f}    "
        f"short ${float(snap.short_exposure):>13,.2f}",
    ]

    if snap.largest_ticker is not None:
        lines.append(
            f"  largest name   {snap.largest_ticker:<6} "
            f"{float(snap.largest_concentration):>+7.1%} of equity"
        )
    lines.append("-" * 56)

    if not snap.per_symbol:
        lines.append("  (no open positions)")
    else:
        lines.append(f"  {'symbol':<8}{'side':<6}{'notional':>16}{'conc':>10}")
        for s in snap.per_symbol:
            side = "SHORT" if s.is_short else "LONG"
            lines.append(
                f"  {s.ticker:<8}{side:<6}"
                f"${float(s.signed_value):>14,.2f}{float(s.concentration):>+9.1%}"
            )
    return "\n".join(lines)


# ------------------------------------------------------- state-store adapter


def _to_decimal(value: object, default: Decimal = _ZERO) -> Decimal:
    """Best-effort string/number -> Decimal. Fails closed to ``default`` on bad input."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _positions_from_state(positions_json: Dict[str, dict]) -> List[PositionSnapshot]:
    """
    Convert the state store's positions blob (ticker -> {qty, avg_entry_price,
    current_price} as strings) into PositionSnapshots. Skips flat/garbage rows.
    """
    out: List[PositionSnapshot] = []
    for ticker, raw in sorted(positions_json.items()):
        qty = _to_decimal(raw.get("qty"))
        if qty == _ZERO:
            continue
        out.append(
            PositionSnapshot(
                ticker=ticker,
                quantity=qty,
                avg_entry_price=_to_decimal(raw.get("avg_entry_price")),
                current_price=_to_decimal(raw.get("current_price")),
            )
        )
    return out


def build_snapshot_from_store(store: object, mode: str) -> Optional[RiskSnapshot]:
    """
    Read the latest persisted run for ``mode`` and compute its risk snapshot.

    Returns None if there is no run recorded yet. The timestamp comes from the
    stored row (never the wall clock), keeping this deterministic w.r.t. the DB.

    ``store`` is typed as ``object`` so this module imports with zero heavy deps;
    the concrete StateStore is lazy-imported by the caller (``main``).
    """
    import json

    rows = store.history(mode)  # type: ignore[attr-defined]
    if not rows:
        return None
    row = rows[-1]

    positions_json = json.loads(row["positions"]) if row["positions"] else {}
    positions = _positions_from_state(positions_json)

    equity = _to_decimal(row["equity"])
    gross_signed = sum((p.market_value for p in positions), _ZERO)
    cash = equity - gross_signed  # cash = equity - net market value of holdings

    ts = datetime.fromisoformat(row["ts"])
    return compute_risk_snapshot(
        positions,
        equity=equity,
        cash=cash,
        timestamp=ts,
        mode=mode,
    )


# ------------------------------------------------------------------- CLI


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="risk_dashboard",
        description="Print a read-only risk snapshot (exposure, leverage, "
        "concentration) from the latest persisted portfolio state.",
    )
    parser.add_argument(
        "--mode",
        default="paper",
        help="Which run mode to report on (default: paper).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the state DB (default: the run_once DEFAULT_STATE_PATH).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        import sys

        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    args = _parse_args(argv)

    # Lazy import: keeps module import side-effect-free and avoids dragging in the
    # heavy run_once dependency graph (and its sqlite handle) until we actually run.
    from scripts.run_once import StateStore

    store = StateStore(args.db) if args.db else StateStore()
    try:
        snap = build_snapshot_from_store(store, args.mode)
    finally:
        store.close()

    if snap is None:
        print(f"No '{args.mode}' runs recorded yet — nothing to report.")
        return 0
    print(render_snapshot(snap))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
