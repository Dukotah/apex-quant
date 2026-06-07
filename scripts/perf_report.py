"""
scripts/perf_report.py
======================
A standalone text PERFORMANCE report: returns, Sharpe and drawdown distilled from
an equity curve. Point it at the ``run_once`` state DB (the paper/live cron trail)
or feed it a backtest equity curve directly.

    python -m scripts.perf_report                 # default state DB, paper mode
    python -m scripts.perf_report --mode live      # a different mode
    python -m scripts.perf_report --db state/apex_state.db --periods 252

Read-only by design: it never touches the broker, never sends over the network,
and the module imports with ZERO side effects (every state/config dependency is
lazily imported INSIDE the functions that need it).

The number-crunching lives in :func:`build_perf_report`, a PURE deterministic
function of a plain equity curve plus injected metadata (no wall clock, no I/O).
The CLI is a thin shell that only READS the equity curve out of SQLite, then
hands it to that pure core. Statistical code uses float to match the
``apex.validation.metrics`` layer it builds on (Golden Rule: follow the layer's
convention).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class PerfStats:
    """The headline performance numbers for one equity curve (all floats)."""

    points: int
    start_equity: float
    end_equity: float
    total_return: float
    annualized_return: float
    sharpe: float
    sortino: float
    max_drawdown: float
    calmar: float


# ------------------------------------------------------------------- pure core


def compute_perf_stats(
    equity_curve: Sequence[float],
    *,
    periods_per_year: int = 252,
    risk_free_rate: float = 0.0,
) -> Optional[PerfStats]:
    """
    Reduce an equity curve to its performance statistics. PURE and deterministic.

    Returns None for an insufficient window (fewer than two points) rather than
    fabricating numbers — fail closed (Golden Rule 6/10).
    """
    eq = [float(v) for v in equity_curve]
    if len(eq) < 2:
        return None

    # metrics is the established statistical layer (float, stdlib-only). Imported
    # at call time so the module stays side-effect-free on import.
    from apex.validation import metrics

    rets = metrics.returns_from_equity(eq)
    return PerfStats(
        points=len(eq),
        start_equity=eq[0],
        end_equity=eq[-1],
        total_return=metrics.total_return(eq),
        annualized_return=metrics.annualized_return(eq, periods_per_year),
        sharpe=metrics.sharpe_ratio(rets, risk_free_rate, periods_per_year),
        sortino=metrics.sortino_ratio(rets, risk_free_rate, periods_per_year),
        max_drawdown=metrics.max_drawdown(eq),
        calmar=metrics.calmar_ratio(eq, periods_per_year),
    )


def build_perf_report(
    equity_curve: Sequence[float],
    *,
    label: str = "performance",
    periods_per_year: int = 252,
    risk_free_rate: float = 0.0,
) -> str:
    """
    Render a plain-text performance report from an equity curve. PURE: no I/O, no
    wall-clock — given the same curve and metadata it always returns the same text.
    """
    stats = compute_perf_stats(
        equity_curve, periods_per_year=periods_per_year, risk_free_rate=risk_free_rate
    )
    if stats is None:
        return (
            f"APEX QUANT — PERFORMANCE REPORT ({label})\n"
            + "=" * 56
            + "\n  not enough data — need at least 2 equity points."
        )

    lines = [
        f"APEX QUANT — PERFORMANCE REPORT ({label})",
        "=" * 56,
        f"  points {stats.points:>6}   "
        f"equity ${stats.start_equity:,.2f} -> ${stats.end_equity:,.2f}",
        "-" * 56,
        f"  total return     {stats.total_return:>+11.2%}",
        f"  annualized       {stats.annualized_return:>+11.2%}",
        f"  Sharpe           {stats.sharpe:>+11.2f}",
        f"  Sortino          {stats.sortino:>+11.2f}",
        f"  max drawdown     {stats.max_drawdown:>11.2%}",
        f"  Calmar           {stats.calmar:>+11.2f}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------- CLI plumbing


def _load_equities(db_path: str, mode: str) -> list[float]:
    """
    Read the equity curve (oldest -> newest) for ``mode`` out of the run_once
    state DB. The state store is imported lazily so importing this module has no
    side effects and pulls in no SQLite/config machinery unless the CLI runs.
    """
    from scripts.run_once import StateStore  # lazy: keeps import side-effect free

    store = StateStore(db_path)
    try:
        rows = store.history(mode)
        return [float(r["equity"]) for r in rows]
    finally:
        store.close()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perf_report",
        description="Print a text performance report (returns, Sharpe, drawdown) "
        "from the run_once state DB. Read-only; never contacts the broker.",
    )
    parser.add_argument(
        "--db",
        default="state/apex_state.db",
        help="Path to the run_once SQLite state DB (default: state/apex_state.db).",
    )
    parser.add_argument(
        "--mode",
        default="paper",
        help="Which run mode's history to report on (default: paper).",
    )
    parser.add_argument(
        "--periods",
        type=int,
        default=252,
        help="Periods per year for annualization (default: 252 trading days).",
    )
    parser.add_argument(
        "--risk-free",
        type=float,
        default=0.0,
        help="Annual risk-free rate used by Sharpe/Sortino (default: 0.0).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        import sys

        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    args = _build_arg_parser().parse_args(argv)
    equities = _load_equities(args.db, args.mode)
    if not equities:
        print(f"No '{args.mode}' runs recorded in {args.db} yet — nothing to report.")
        return 0
    print(
        build_perf_report(
            equities,
            label=args.mode,
            periods_per_year=args.periods,
            risk_free_rate=args.risk_free,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
