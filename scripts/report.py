"""
scripts/report.py
=================
Paper-gate monitor. Turns the run_once state DB (one row per cron cycle) into a live
performance report so you can WATCH the 30-day paper gate instead of squinting at the
broker: realized return, rolling Sharpe vs the validated backtest, max drawdown, the
drift state, and gate progress.

Run:  python -m scripts.report            # default state DB, paper mode
      python -m scripts.report live       # a different mode
      python -m scripts.report --check    # print report + exit 1 if gate not met
      python -m scripts.report --check live  # same for live mode

Pure read-only; never touches the broker.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import Sequence

from apex.analytics.sleeve_attribution import SleeveAttribution, attribute_fills
from apex.core.events import FillEvent
from apex.validation import metrics
from apex.validation.drift_monitor import DriftMonitor
from scripts.run_once import DEPLOYED_VALIDATED_SHARPE, StateStore

GATE_DAYS = 30  # Rule 17: 30+ days of paper before live capital
GATE_MIN_SHARPE = 1.0  # the going-live bar


def _bar(frac: float, width: int = 24) -> str:
    filled = max(0, min(width, round(frac * width)))
    return "█" * filled + "░" * (width - filled)


def gate_passed(
    store: StateStore,
    mode: str = "paper",
    validated_sharpe: float = DEPLOYED_VALIDATED_SHARPE,
) -> bool:
    """
    Pure gate verdict: True iff the paper-trade record meets all criteria for
    live promotion.

    Criteria (all must hold):
      - At least GATE_DAYS cycles recorded for ``mode``.
      - Full-history Sharpe ratio >= GATE_MIN_SHARPE.
      - NOT currently quarantined by the drift monitor.

    This is the single source of truth; ``build_report`` delegates here so the
    two can never disagree.
    """
    rows = store.history(mode)
    n = len(rows)
    if n < GATE_DAYS:
        return False

    equities = [float(r["equity"]) for r in rows]
    rets = metrics.returns_from_equity(equities)
    full_sharpe = metrics.sharpe_ratio(rets)
    if full_sharpe < GATE_MIN_SHARPE:
        return False

    # Drift quarantine: replay the equity trail through the monitor.
    mon = DriftMonitor("multi_asset_trend", validated_sharpe=validated_sharpe, window=30)
    reading = None
    for e in equities:
        reading = mon.record_equity(e)

    if reading is not None and reading.is_quarantined:
        return False

    return True


def build_sleeve_section(
    fills: Sequence[FillEvent],
    capital_base: Decimal,
) -> str:
    """
    Render the per-sleeve (per-symbol) P&L attribution block (NEXT-4).

    A 7-sleeve book reports one aggregate equity curve, which can hide a single
    failing sleeve behind the others. This breaks the realized result down per
    symbol via ``apex.analytics.sleeve_attribution`` (FIFO round-trip matching),
    so a dead or bleeding sleeve shows up on its own line.

    ``fills`` is the book's chronological fill history (oldest-first) and
    ``capital_base`` is the equity the contributions are expressed against. When
    no fill history is available — the common case today; see the DATA GAP note
    in :func:`build_report` — this returns a single explanatory line rather than
    inventing trades, so the section is honest about what it can and cannot show.

    Returns the rendered block as a string (no trailing newline); never raises.
    """
    header = ["-" * 56, "  PER-SLEEVE ATTRIBUTION (realized, FIFO round-trips)"]
    if not fills:
        return "\n".join(
            header + ["  (no fill history available — see DATA GAP note; nothing to attribute)"]
        )

    attribution = attribute_fills(fills, capital_base=capital_base)

    # Worst-contributing sleeve first — that's the one most likely to be failing.
    rows: list[tuple[str, SleeveAttribution]] = sorted(
        attribution.items(), key=lambda kv: kv[1].realized_pnl
    )
    lines = list(header)
    lines.append(f"  {'sleeve':<8}{'realized':>13}{'trades':>8}{'win%':>8}{'contrib':>10}")
    for ticker, a in rows:
        win_pct = float(a.win_rate) * 100.0
        contrib = float(a.return_contribution) * 100.0
        lines.append(
            f"  {ticker:<8}${float(a.realized_pnl):>11,.2f}{a.trade_count:>8}"
            f"{win_pct:>7.0f}%{contrib:>+9.2f}%"
        )
    return "\n".join(lines)


def build_report(
    store: StateStore,
    mode: str = "paper",
    validated_sharpe: float = DEPLOYED_VALIDATED_SHARPE,
    fills: Sequence[FillEvent] | None = None,
) -> str:
    rows = store.history(mode)
    if not rows:
        return f"No '{mode}' runs recorded yet — the bot hasn't completed a cycle."

    equities = [float(r["equity"]) for r in rows]
    rets = metrics.returns_from_equity(equities)
    full_sharpe = metrics.sharpe_ratio(rets)
    mdd = metrics.max_drawdown(equities)
    total_ret = (equities[-1] / equities[0] - 1.0) if equities[0] else 0.0

    # Rolling drift reading (same monitor the live cron uses).
    mon = DriftMonitor("multi_asset_trend", validated_sharpe=validated_sharpe, window=30)
    reading = None
    for e in equities:
        reading = mon.record_equity(e)

    n = len(rows)
    orders = sum(int(r["orders"]) for r in rows)
    fill_count = sum(int(r["fills"]) for r in rows)
    halted = sum(1 for r in rows if int(r["halted"]))
    first, last = rows[0]["ts"][:10], rows[-1]["ts"][:10]
    gate_frac = min(1.0, n / GATE_DAYS)
    # Delegate to gate_passed() — single source of truth for the verdict.
    gate_met = gate_passed(store, mode, validated_sharpe)
    floor = validated_sharpe * 0.70

    lines = [
        "APEX QUANT — PAPER GATE REPORT",
        "=" * 56,
        f"  mode {mode}   cycles {n}   span {first} .. {last}",
        f"  equity      ${equities[-1]:>12,.2f}   (start ${equities[0]:,.0f})",
        f"  total return {total_ret:>+11.2%}   max drawdown {mdd:>7.1%}",
        f"  full Sharpe  {full_sharpe:>+11.2f}   (validated {validated_sharpe:.2f}, floor {floor:.2f})",
        f"  activity     {orders:>6} orders   {fill_count} fills   {halted} halted cycle(s)",
        "-" * 56,
        f"  30-day gate  {_bar(gate_frac)}  {n}/{GATE_DAYS} days",
    ]
    if reading is not None:
        lines.append(f"  drift        {reading.summary()}")

    # NEXT-4 per-sleeve attribution. DATA GAP: the StateStore audit trail persists
    # only a fill COUNT and an end-of-cycle positions snapshot (qty / avg_entry /
    # current_price) per run — NOT the individual FillEvents — so report.py cannot
    # reconstruct round-trip trades from the DB alone. The attribution section is
    # therefore wired to accept an explicit ``fills`` history (oldest-first) from a
    # caller that has it (e.g. a backtest run or a future fills-persisting store);
    # when none is supplied it prints an honest "no fill history" line rather than
    # fabricating trades. Persisting full fills is an integrator follow-up.
    lines.append(build_sleeve_section(fills or [], Decimal(str(equities[0]))))

    verdict = (
        "✅ GATE PASSED — eligible for the live checklist"
        if gate_met
        else f"… running — need {GATE_DAYS}+ days and full Sharpe ≥ {GATE_MIN_SHARPE:.1f}"
        + (" (quarantined!)" if reading and reading.is_quarantined else "")
    )
    lines += ["-" * 56, f"  verdict      {verdict}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """
    Entry point.

    Without --check: print the report and return 0 (unchanged behaviour).
    With --check [mode]: print the report and return 0 if the gate is passed,
    1 if it is not.  ``mode`` may appear before or after --check.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    args = list(argv) if argv is not None else sys.argv[1:]

    check_mode = "--check" in args
    remaining = [a for a in args if a != "--check"]
    mode = remaining[0] if remaining else "paper"

    store = StateStore()
    print(build_report(store, mode))

    if check_mode:
        return 0 if gate_passed(store, mode) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
