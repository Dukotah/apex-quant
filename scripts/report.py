"""
scripts/report.py
=================
Paper-gate monitor. Turns the run_once state DB (one row per cron cycle) into a live
performance report so you can WATCH the 30-day paper gate instead of squinting at the
broker: realized return, rolling Sharpe vs the validated backtest, max drawdown, the
drift state, and gate progress.

Run:  python -m scripts.report            # default state DB, paper mode
      python -m scripts.report live       # a different mode
Pure read-only; never touches the broker.
"""

from __future__ import annotations

import sys

from apex.validation import metrics
from apex.validation.drift_monitor import DriftMonitor
from scripts.run_once import DEPLOYED_VALIDATED_SHARPE, StateStore

GATE_DAYS = 30  # Rule 17: 30+ days of paper before live capital
GATE_MIN_SHARPE = 1.0  # the going-live bar


def _bar(frac: float, width: int = 24) -> str:
    filled = max(0, min(width, round(frac * width)))
    return "█" * filled + "░" * (width - filled)


def build_report(
    store: StateStore, mode: str = "paper", validated_sharpe: float = DEPLOYED_VALIDATED_SHARPE
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
    fills = sum(int(r["fills"]) for r in rows)
    halted = sum(1 for r in rows if int(r["halted"]))
    first, last = rows[0]["ts"][:10], rows[-1]["ts"][:10]
    gate_frac = min(1.0, n / GATE_DAYS)
    gate_met = n >= GATE_DAYS and full_sharpe >= GATE_MIN_SHARPE
    floor = validated_sharpe * 0.70

    lines = [
        "APEX QUANT — PAPER GATE REPORT",
        "=" * 56,
        f"  mode {mode}   cycles {n}   span {first} .. {last}",
        f"  equity      ${equities[-1]:>12,.2f}   (start ${equities[0]:,.0f})",
        f"  total return {total_ret:>+11.2%}   max drawdown {mdd:>7.1%}",
        f"  full Sharpe  {full_sharpe:>+11.2f}   (validated {validated_sharpe:.2f}, floor {floor:.2f})",
        f"  activity     {orders:>6} orders   {fills} fills   {halted} halted cycle(s)",
        "-" * 56,
        f"  30-day gate  {_bar(gate_frac)}  {n}/{GATE_DAYS} days",
    ]
    if reading is not None:
        lines.append(f"  drift        {reading.summary()}")
    verdict = (
        "✅ GATE PASSED — eligible for the live checklist"
        if gate_met
        else f"… running — need {GATE_DAYS}+ days and full Sharpe ≥ {GATE_MIN_SHARPE:.1f}"
        + (" (quarantined!)" if reading and reading.is_quarantined else "")
    )
    lines += ["-" * 56, f"  verdict      {verdict}"]
    return "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    mode = sys.argv[1] if len(sys.argv) > 1 else "paper"
    print(build_report(StateStore(), mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
