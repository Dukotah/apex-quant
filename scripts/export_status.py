"""
scripts/export_status.py
========================
The engine-side STATUS EXPORT — the API boundary between apex-quant (the brain)
and apex-trader (the Next.js control surface).

It publishes a single JSON snapshot (``state/status.json`` by default) that the
dashboard polls. The shape is fixed by the TypeScript contract in apex-trader's
``src/lib/types.ts`` (``StatusSnapshot``); field names and nesting must match it
byte-for-byte. This module is the Python source of truth for that contract.

Two layers, deliberately separated for testability:

  * ``build_status(...)`` — a PURE function. Given the engine state (portfolio,
    persisted history rows, optional gauntlet/drift readings, config) and an
    explicit ``now`` (Golden Rule 10: no wall-clock in logic), it returns a plain
    ``dict`` in the contract shape. Decimal in, float out: every monetary value is
    converted to ``float`` ONLY at this serialization edge, with non-finite values
    guarded to keep the JSON valid (no NaN/Infinity ever leaves here).

  * ``main()`` — a thin CLI. Loads persisted truth from the run_once StateStore,
    builds the snapshot, and writes it (or prints with ``--stdout``). Idempotent;
    its only side effect is writing the file.

FAIL-SOFT (Golden Rule 6, applied to the API edge): the dashboard must ALWAYS
receive valid JSON. If a section can't be built (no gauntlet results yet, an empty
state DB, a partial row), that section degrades to a sensible empty list / null /
zero rather than crashing the export.

Reuses the existing metric math (``apex.validation.metrics``), the drift monitor,
and the run_once ``StateStore`` — it does NOT re-derive Sharpe/drawdown/returns.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from apex.risk.portfolio import Portfolio
from apex.validation import metrics
from apex.validation.drift_monitor import DriftMonitor, DriftReading, DriftState
from apex.validation.gauntlet import GauntletReport
from scripts.run_once import (
    DEFAULT_STATE_PATH,
    DEPLOYED_UNIVERSE,
    DEPLOYED_VALIDATED_SHARPE,
    StateStore,
)

logger = logging.getLogger("apex.export_status")

DEFAULT_STATUS_PATH = Path("state/status.json")

# Paper-gate constants mirror scripts/report.py so the two views never disagree.
GATE_DAYS = 30
GATE_TARGET_SHARPE = 1.0
# How far the live rolling Sharpe may sit below the validated backtest and still
# count as "within the backtest band" (the 70% quarantine floor, inverted to a band).
BACKTEST_BAND_RATIO = 0.70

# The deployed strategy's identity (single live sleeve today; see run_once).
DEPLOYED_STRATEGY_ID = "multi_asset_trend"
DEPLOYED_STRATEGY_NAME = "Multi-Asset Trend (inverse-vol)"

_ZERO = Decimal("0")


# --------------------------------------------------------------------- helpers


def _num(value: Any, default: float = 0.0) -> float:
    """
    Convert any numeric (Decimal/int/float) to a JSON-safe float at the edge.

    Guards non-finite values (NaN/Inf) to ``default`` so the emitted JSON is always
    valid — json.dumps with allow_nan=False would otherwise raise, and the dashboard
    must never receive a malformed payload.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return f


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime as an ISO-8601 string, or None if absent."""
    if dt is None:
        return None
    return dt.isoformat()


def _drift_status_str(state: DriftState) -> str:
    """Map the engine's DriftState enum onto the contract's DriftStatus union."""
    if state == DriftState.QUARANTINED:
        return "quarantined"
    if state == DriftState.WARMING_UP:
        return "warn"
    return "ok"


# --------------------------------------------------------------------- builders


def _build_account(
    portfolio: Portfolio,
    *,
    buying_power: Optional[Decimal] = None,
) -> Dict[str, Any]:
    """The AccountSnapshot section. All money -> float at the edge; pct as fractions."""
    equity = portfolio.equity
    day_start = portfolio.day_start_equity
    day_pnl = equity - day_start
    day_pnl_pct = (day_pnl / day_start) if day_start and day_start != _ZERO else _ZERO
    # Cash is the conservative buying-power proxy when the broker doesn't report one
    # (no margin assumed — fail-closed toward less buying power, never more).
    bp = buying_power if buying_power is not None else portfolio.cash
    return {
        "equity": _num(equity),
        "cash": _num(portfolio.cash),
        "buyingPower": _num(bp),
        "peakEquity": _num(portfolio.peak_equity),
        "currentDrawdownPct": _num(portfolio.drawdown),
        "dailyStartEquity": _num(day_start),
        "dayPnl": _num(day_pnl),
        "dayPnlPct": _num(day_pnl_pct),
    }


def _build_equity_curve(
    equities: Sequence[float], timestamps: Sequence[str]
) -> List[Dict[str, Any]]:
    """
    The equityCurve from persisted per-cycle equity points. Drawdown at each point
    is computed against the running peak (the same definition metrics.max_drawdown
    uses), so the curve and the account agree.
    """
    out: List[Dict[str, Any]] = []
    peak = float("-inf")
    for ts, eq in zip(timestamps, equities):
        e = _num(eq)
        if e > peak:
            peak = e
        dd = ((peak - e) / peak) if peak > 0 else 0.0
        out.append({"t": ts, "equity": e, "drawdownPct": _num(max(0.0, dd))})
    return out


def _build_positions(portfolio: Portfolio, strategy_id: str) -> List[Dict[str, Any]]:
    """The PositionRow list, marked to the portfolio's last known prices."""
    equity = portfolio.equity
    rows: List[Dict[str, Any]] = []
    # Stable order by ticker so the export is deterministic (Golden Rule 10).
    for ticker in sorted(portfolio.open_positions):
        pos = portfolio.open_positions[ticker]
        mv = pos.market_value
        cost = pos.avg_entry_price * pos.quantity * pos.symbol.contract_multiplier
        upnl = pos.unrealized_pnl
        upnl_pct = (upnl / abs(cost)) if cost and cost != _ZERO else _ZERO
        weight = (mv / equity) if equity and equity != _ZERO else _ZERO
        rows.append(
            {
                "ticker": ticker,
                "assetClass": pos.symbol.asset_class.value,
                "quantity": _num(pos.quantity),
                "avgPrice": _num(pos.avg_entry_price),
                "lastPrice": _num(pos.current_price),
                "marketValue": _num(mv),
                "unrealizedPnl": _num(upnl),
                "unrealizedPnlPct": _num(upnl_pct),
                "weightPct": _num(weight),
                "strategyId": strategy_id,
            }
        )
    return rows


def _build_trades(trades: Optional[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    The TradeRow list. Trades are supplied by the caller (the engine doesn't persist
    individual fills today — only counts — so the CLI passes []). Each input dict is
    normalized to the contract shape; missing optional fields degrade gracefully.
    """
    if not trades:
        return []
    out: List[Dict[str, Any]] = []
    for t in trades:
        row = {
            "id": str(t.get("id", "")),
            "t": t.get("t") or _iso(t.get("timestamp")),
            "ticker": str(t.get("ticker", "")),
            "side": str(t.get("side", "")).upper(),
            "quantity": _num(t.get("quantity")),
            "price": _num(t.get("price")),
            "notional": _num(t.get("notional")),
            "commission": _num(t.get("commission")),
            "strategyId": str(t.get("strategyId", "")),
            "reason": str(t.get("reason", "")),
        }
        if t.get("pnl") is not None:
            row["pnl"] = _num(t.get("pnl"))
        out.append(row)
    return out


def _build_strategies(
    *,
    mode: str,
    drift: Optional[DriftReading],
    gauntlet: Optional[GauntletReport],
    allocation_pct: float,
) -> List[Dict[str, Any]]:
    """
    The StrategyRow list. Today there is a single deployed sleeve; its status is
    derived from mode + drift, and its grade/OOS Sharpe from the gauntlet report
    when available. Fail-soft: optional metrics are simply omitted if unknown.
    """
    # Map mode -> a strategy status, then let drift downgrade to quarantined.
    if mode == "live":
        status = "live"
    elif mode == "paper":
        status = "paper"
    else:
        status = "research"

    drift_status: Optional[str] = None
    if drift is not None:
        drift_status = _drift_status_str(drift.state)
        if drift.is_quarantined:
            status = "quarantined"

    row: Dict[str, Any] = {
        "id": DEPLOYED_STRATEGY_ID,
        "name": DEPLOYED_STRATEGY_NAME,
        "status": status,
        "allocationPct": _num(allocation_pct),
    }
    if gauntlet is not None:
        row["grade"] = gauntlet.grade.value
    if drift_status is not None:
        row["driftStatus"] = drift_status
    return [row]


def _build_paper_gate(
    equities: Sequence[float],
    timestamps: Sequence[str],
    *,
    validated_sharpe: float,
) -> Dict[str, Any]:
    """
    The PaperGate progress block — reuses scripts/report.py's logic (rolling Sharpe
    vs the validated backtest, days elapsed vs the 30-day rule).
    """
    n = len(equities)
    rolling = metrics.sharpe_ratio(metrics.returns_from_equity(list(equities)))
    floor = validated_sharpe * BACKTEST_BAND_RATIO
    within = rolling >= floor
    start = timestamps[0] if timestamps else _iso(datetime(1970, 1, 1, tzinfo=timezone.utc))
    return {
        "startDate": start,
        "daysElapsed": n,
        "daysRequired": GATE_DAYS,
        "rollingSharpe": _num(rolling),
        "targetSharpe": _num(GATE_TARGET_SHARPE),
        "withinBacktestBand": bool(within),
    }


def _build_gauntlet(reports: Optional[Sequence[GauntletReport]]) -> List[Dict[str, Any]]:
    """
    The gauntlet section: each GauntletReport flattened to the contract shape.
    Fail-soft: no reports -> empty list (the dashboard renders an empty panel).
    """
    if not reports:
        return []
    out: List[Dict[str, Any]] = []
    for rep in reports:
        gates = []
        for i, g in enumerate(rep.gates, start=1):
            gates.append(
                {
                    "n": i,
                    "name": g.name,
                    "status": g.status.value,
                    "detail": g.detail,
                    "hard": bool(g.is_hard_gate),
                }
            )
        out.append(
            {
                "strategyId": rep.strategy_name,
                "grade": rep.grade.value,
                "gates": gates,
            }
        )
    return out


def _build_alerts(
    *,
    now: datetime,
    halted: bool,
    halt_reason: str,
    drift: Optional[DriftReading],
    extra: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    The alerts feed. Derived deterministically from the current state (halt + drift)
    plus any caller-supplied alerts. Timestamped with the injected ``now``.
    """
    ts = _iso(now)
    alerts: List[Dict[str, Any]] = []
    if halted:
        alerts.append(
            {
                "t": ts,
                "level": "critical",
                "kind": "halt",
                "message": halt_reason or "System halted by the risk manager.",
            }
        )
    if drift is not None and drift.is_quarantined:
        alerts.append(
            {
                "t": ts,
                "level": "critical",
                "kind": "drift",
                "message": drift.reason or "Strategy quarantined: live edge decayed below floor.",
            }
        )
    elif drift is not None and drift.state == DriftState.WARMING_UP:
        alerts.append(
            {
                "t": ts,
                "level": "info",
                "kind": "drift",
                "message": drift.reason or "Drift monitor warming up.",
            }
        )
    for a in extra or []:
        alerts.append(
            {
                "t": a.get("t") or ts,
                "level": str(a.get("level", "info")),
                "kind": str(a.get("kind", "")),
                "message": str(a.get("message", "")),
            }
        )
    return alerts


# ------------------------------------------------------------------ pure entry


def build_status(
    *,
    now: datetime,
    mode: str,
    portfolio: Portfolio,
    equity_history: Optional[Sequence[float]] = None,
    timestamp_history: Optional[Sequence[str]] = None,
    halted: bool = False,
    halt_reason: str = "",
    drift: Optional[DriftReading] = None,
    gauntlet_reports: Optional[Sequence[GauntletReport]] = None,
    trades: Optional[Sequence[Dict[str, Any]]] = None,
    extra_alerts: Optional[Sequence[Dict[str, Any]]] = None,
    validated_sharpe: float = DEPLOYED_VALIDATED_SHARPE,
    allocation_pct: float = 1.0,
    buying_power: Optional[Decimal] = None,
    strategy_id: str = DEPLOYED_STRATEGY_ID,
) -> Dict[str, Any]:
    """
    Build the StatusSnapshot dict (the apex-trader contract) from engine state.

    PURE and DETERMINISTIC: every value derives from the arguments, and ``now`` is
    injected (no wall-clock). The result is plain JSON-serializable Python — Decimal
    has been converted to float at every edge and non-finite values guarded to keep
    the payload valid.

    Args:
        now:                injected current time (UTC); stamps generatedAt + alerts.
        mode:               "backtest" | "paper" | "live".
        portfolio:          the live Portfolio (account + positions, Decimal truth).
        equity_history:     per-cycle equity points (oldest->newest) for the curve.
        timestamp_history:  ISO timestamps aligned with equity_history.
        halted/halt_reason: the risk manager's halt state.
        drift:              a DriftReading (or None if unavailable).
        gauntlet_reports:   validated gauntlet reports (or None/empty).
        trades:             recent trades as dicts (or None — engine persists counts only).
        extra_alerts:       caller-supplied alerts to merge in.
        validated_sharpe:   the backtest Sharpe the paper gate compares against.
        allocation_pct:     deployed strategy's capital allocation (fraction).
        buying_power:       broker buying power (defaults to cash, fail-closed).
        strategy_id:        id stamped on position rows.
    """
    equities = list(equity_history or [])
    timestamps = list(timestamp_history or [])
    # Defensive: if the two series disagree in length, truncate to the shorter so
    # the curve never pairs an equity with the wrong timestamp.
    if len(equities) != len(timestamps):
        k = min(len(equities), len(timestamps))
        equities, timestamps = equities[:k], timestamps[:k]

    return {
        "generatedAt": _iso(now),
        "mode": mode,
        "halted": bool(halted),
        "haltReason": halt_reason or None,
        "account": _build_account(portfolio, buying_power=buying_power),
        "equityCurve": _build_equity_curve(equities, timestamps),
        "positions": _build_positions(portfolio, strategy_id),
        "trades": _build_trades(trades),
        "strategies": _build_strategies(
            mode=mode,
            drift=drift,
            gauntlet=(gauntlet_reports[0] if gauntlet_reports else None),
            allocation_pct=allocation_pct,
        ),
        "paperGate": _build_paper_gate(equities, timestamps, validated_sharpe=validated_sharpe),
        "gauntlet": _build_gauntlet(gauntlet_reports),
        "alerts": _build_alerts(
            now=now, halted=halted, halt_reason=halt_reason, drift=drift, extra=extra_alerts
        ),
    }


# ----------------------------------------------------------- structural typing


class DriftReadingLike:
    """
    Structural placeholder for typing only — the real object is a
    ``apex.validation.drift_monitor.DriftReading``. Documents the attributes
    build_status reads: ``state`` (DriftState), ``is_quarantined`` (bool),
    ``reason`` (str). Never instantiated.
    """

    state: DriftState
    is_quarantined: bool
    reason: str


# --------------------------------------------------------------------- loaders


def _portfolio_from_row(row: Any, initial_capital: Decimal) -> Portfolio:
    """
    Rebuild a Portfolio from a persisted run row (the StateStore's positions JSON).

    The row stores equity, plus per-ticker {qty, avg_entry_price, current_price}.
    We seed positions via the portfolio's public fill API (a synthetic fill at the
    average entry), then mark each to its persisted current price — reaching the same
    equity the cron recorded without inventing P&L. Fail-soft: a malformed positions
    blob yields an empty (cash-only) portfolio rather than a crash.
    """
    from apex.core.events import FillEvent, MarketEvent
    from apex.core.models import AssetClass, Bar, OrderSide, Symbol

    portfolio = Portfolio(initial_capital)
    try:
        raw = json.loads(row["positions"]) if row["positions"] else {}
    except (TypeError, ValueError, KeyError):
        raw = {}

    ts = None
    try:
        ts = datetime.fromisoformat(row["ts"])
    except (TypeError, ValueError, KeyError):
        ts = datetime.now(timezone.utc)

    for ticker, p in raw.items():
        try:
            qty = Decimal(str(p["qty"]))
            avg = Decimal(str(p["avg_entry_price"]))
            cur = Decimal(str(p.get("current_price", avg)))
        except (TypeError, ValueError, KeyError):
            continue
        if qty == _ZERO:
            continue
        # ETF is the deployed universe's class; default to it (asset class doesn't
        # affect money math here, only the displayed label).
        asset_class = AssetClass.ETF if ticker in DEPLOYED_UNIVERSE else AssetClass.EQUITY
        symbol = Symbol(ticker, asset_class)
        side = OrderSide.BUY if qty > 0 else OrderSide.SELL
        portfolio.on_fill(
            FillEvent(
                symbol=symbol,
                side=side,
                quantity=abs(qty),
                fill_price=avg,
                commission=_ZERO,
                slippage=_ZERO,
                order_id="export",
                broker_order_id="export",
                timestamp=ts,
                is_paper=True,
            )
        )
        # Mark to the persisted current price so equity matches the recorded run.
        if cur != avg:
            portfolio.on_market(
                MarketEvent(
                    bar=Bar(
                        symbol=symbol,
                        timestamp=ts,
                        open=cur,
                        high=cur,
                        low=cur,
                        close=cur,
                        volume=_ZERO,
                    )
                )
            )
    return portfolio


def build_status_from_store(
    store: StateStore,
    *,
    now: datetime,
    mode: str = "paper",
    initial_capital: Decimal = Decimal("100000"),
    validated_sharpe: float = DEPLOYED_VALIDATED_SHARPE,
    gauntlet_reports: Optional[Sequence[GauntletReport]] = None,
) -> Dict[str, Any]:
    """
    Load persisted truth from the run_once StateStore and build the snapshot.

    Reconstructs the portfolio from the latest row's positions, the equity curve
    from all rows, the halt flag from the latest row, and the drift reading by
    replaying equity history through the DriftMonitor (the same monitor run_once and
    report.py use). Fully fail-soft: an empty DB yields a valid cash-only snapshot.
    """
    rows = store.history(mode)
    if not rows:
        portfolio = Portfolio(initial_capital)
        return build_status(
            now=now,
            mode=mode,
            portfolio=portfolio,
            equity_history=[],
            timestamp_history=[],
            validated_sharpe=validated_sharpe,
            gauntlet_reports=gauntlet_reports,
        )

    equities = [float(r["equity"]) for r in rows]
    timestamps = [str(r["ts"]) for r in rows]
    latest = rows[-1]
    halted = bool(int(latest["halted"]))

    portfolio = _portfolio_from_row(latest, initial_capital)

    # Drift reading: replay equity history through the monitor (validated_sharpe>0).
    drift = None
    try:
        mon = DriftMonitor(
            DEPLOYED_STRATEGY_ID, validated_sharpe=validated_sharpe, window=GATE_DAYS
        )
        for eq in equities:
            drift = mon.record_equity(eq)
    except Exception as exc:  # noqa: BLE001 — drift is informational, never fatal
        logger.warning("drift reading unavailable for export: %s", exc)
        drift = None

    halt_reason = "System halted by the risk manager." if halted else ""

    return build_status(
        now=now,
        mode=mode,
        portfolio=portfolio,
        equity_history=equities,
        timestamp_history=timestamps,
        halted=halted,
        halt_reason=halt_reason,
        drift=drift,
        gauntlet_reports=gauntlet_reports,
        validated_sharpe=validated_sharpe,
    )


# ----------------------------------------------------------------- multi-book

DEFAULT_EXPERIMENTS_DIR = Path("state/experiments")


def _book_summary(snapshot: Dict[str, Any], initial_capital: float) -> Dict[str, Any]:
    """
    Leaderboard stats for one book, from its equity curve + account: total return
    since inception, rolling Sharpe, worst drawdown, today's move, and the number
    of recorded sessions. Fail-soft: an empty curve yields zeros, never raises.
    """
    curve = snapshot.get("equityCurve") or []
    equities = [float(p.get("equity", 0.0)) for p in curve]
    sessions = len(equities)
    first = equities[0] if equities else initial_capital
    last = equities[-1] if equities else initial_capital
    total_return = (last / first - 1.0) if first else 0.0
    max_dd = max((float(p.get("drawdownPct", 0.0)) for p in curve), default=0.0)
    sharpe = metrics.sharpe_ratio(metrics.returns_from_equity(equities)) if sessions >= 2 else 0.0
    account = snapshot.get("account") or {}
    return {
        "totalReturnPct": _num(total_return),
        "sharpe": _num(sharpe),
        "maxDrawdownPct": _num(max_dd),
        "dayPnlPct": _num(account.get("dayPnlPct", 0.0)),
        "sessions": sessions,
    }


def _book_from_snapshot(
    snapshot: Dict[str, Any],
    *,
    book_id: str,
    name: str,
    kind: str,
    initial_capital: float,
) -> Dict[str, Any]:
    """Project an already-built single-book snapshot into a compact BookSnapshot entry."""
    strategies = snapshot.get("strategies") or []
    status = strategies[0]["status"] if strategies else "paper"
    return {
        "id": book_id,
        "name": name,
        "kind": kind,  # "deployed" | "experiment"
        "status": status,
        "halted": bool(snapshot.get("halted", False)),
        "account": snapshot.get("account", {}),
        "equityCurve": snapshot.get("equityCurve", []),
        "positions": snapshot.get("positions", []),
        "paperGate": snapshot.get("paperGate", {}),
        "summary": _book_summary(snapshot, initial_capital),
    }


def build_book_entry(
    store: StateStore,
    *,
    book_id: str,
    name: str,
    kind: str,
    now: datetime,
    initial_capital: Decimal = Decimal("100000"),
    validated_sharpe: float = DEPLOYED_VALIDATED_SHARPE,
) -> Dict[str, Any]:
    """Build one BookSnapshot from a book's state DB (reuses build_status_from_store)."""
    snap = build_status_from_store(
        store,
        now=now,
        mode="paper",
        initial_capital=initial_capital,
        validated_sharpe=validated_sharpe,
    )
    return _book_from_snapshot(
        snap, book_id=book_id, name=name, kind=kind, initial_capital=float(initial_capital)
    )


def build_multi_status(
    *,
    now: datetime,
    deployed_store: StateStore,
    deployed_name: str = DEPLOYED_STRATEGY_NAME,
    experiments: Sequence[Any] = (),
    initial_capital: Decimal = Decimal("100000"),
) -> Dict[str, Any]:
    """
    Assemble the full multi-book snapshot.

    The DEPLOYED book stays the top-level snapshot — every existing single-book
    dashboard field is unchanged for back-compat — and is ALSO ``books[0]``. Each
    ``experiments`` item is a ``(StateStore, id, name)`` tuple and becomes an
    experiment book entry. The dashboard reads ``books[]`` for the compare view.
    """
    base = build_status_from_store(
        deployed_store, now=now, mode="paper", initial_capital=initial_capital
    )
    books: List[Dict[str, Any]] = [
        _book_from_snapshot(
            base,
            book_id="deployed",
            name=deployed_name,
            kind="deployed",
            initial_capital=float(initial_capital),
        )
    ]
    for store, book_id, name in experiments:
        books.append(
            build_book_entry(
                store,
                book_id=book_id,
                name=name,
                kind="experiment",
                now=now,
                initial_capital=initial_capital,
            )
        )
    out = dict(base)
    out["books"] = books
    return out


# --------------------------------------------------------------------- the CLI


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - thin I/O wiring
    """
    CLI: load persisted state, build the snapshot, write it (or print with --stdout).

    Idempotent and side-effect-light: the only effect is writing the JSON file.
    Always emits valid JSON, even on an empty/partial state DB (fail-soft).
    """
    parser = argparse.ArgumentParser(description="Export the apex-trader status snapshot.")
    parser.add_argument("--mode", default="paper", help="backtest|paper|live (default: paper)")
    parser.add_argument(
        "--state", default=str(DEFAULT_STATE_PATH), help="path to the run_once state DB"
    )
    parser.add_argument("--out", default=str(DEFAULT_STATUS_PATH), help="output JSON path")
    parser.add_argument(
        "--stdout", action="store_true", help="print to stdout instead of writing a file"
    )
    parser.add_argument(
        "--capital", default="100000", help="initial capital fallback for reconstruction"
    )
    parser.add_argument(
        "--experiments-dir",
        default=str(DEFAULT_EXPERIMENTS_DIR),
        help="dir of per-book experiment state DBs; if present, emit a multi-book books[]",
    )
    parser.add_argument(
        "--no-experiments", action="store_true", help="single-book export only (ignore experiments)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    from apex.core.clock import RealClock

    now = RealClock().now()

    capital = Decimal(str(args.capital))
    exp_dir = Path(args.experiments_dir)
    db_files = (
        sorted(exp_dir.glob("*.db")) if (exp_dir.exists() and not args.no_experiments) else []
    )

    store = StateStore(args.state)
    exp_stores: List[StateStore] = []
    try:
        if db_files:
            # Display names come from the experiment roster (fallback: the file stem).
            try:
                from scripts.run_experiments import default_experiments

                names = {b.id: b.name for b in default_experiments()}
            except Exception:  # noqa: BLE001 — names are cosmetic; never block the export
                names = {}
            experiments = []
            for db in db_files:
                book_id = db.stem
                exp_store = StateStore(db)
                exp_stores.append(exp_store)
                experiments.append((exp_store, book_id, names.get(book_id, book_id)))
            snapshot = build_multi_status(
                now=now,
                deployed_store=store,
                experiments=experiments,
                initial_capital=capital,
            )
        else:
            snapshot = build_status_from_store(
                store, now=now, mode=args.mode, initial_capital=capital
            )
    finally:
        store.close()
        for exp_store in exp_stores:
            exp_store.close()

    # allow_nan=False is the final guard: build_status already scrubbed non-finite
    # values, so this should never raise — but if it ever did, we want to know.
    payload = json.dumps(snapshot, indent=2, allow_nan=False, sort_keys=False)

    if args.stdout:
        try:
            import sys

            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        print(payload)
    else:
        out_path = Path(args.out)
        if str(out_path.parent) not in ("", "."):
            out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
        logger.info("Wrote status snapshot to %s (%d bytes)", out_path, len(payload))
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
