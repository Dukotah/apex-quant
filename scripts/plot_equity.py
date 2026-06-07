"""
scripts/plot_equity.py
======================
Render the paper/live equity curve (and its drawdown) from the run_once state DB
to a PNG. The same data scripts/report.py reads as text — here drawn as a chart so
you can eyeball the 30-day gate instead of squinting at numbers.

The state DB holds one row per cron cycle (ts, equity, ...); this loads them oldest
-> newest for a `mode`, computes the running drawdown with the same definition the
Gauntlet uses (apex.validation.metrics), and stacks an equity panel over a drawdown
panel into a single PNG.

Usage:
    python -m scripts.plot_equity                       # paper mode -> logs/equity_paper.png
    python -m scripts.plot_equity live                  # live mode
    python -m scripts.plot_equity --out reports/eq.png  # custom output path

matplotlib is imported LAZILY (with the Agg backend) inside render_equity_png so the
module imports with zero side effects and the script degrades gracefully (clear
message, exit 0) when matplotlib isn't installed. It is NOT a hard dependency.

Pure read-only; never touches the broker. Deterministic given the same state DB.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from apex.validation import metrics
from scripts.run_once import StateStore

DEFAULT_LOG_DIR = Path("logs")


def drawdown_series(equity_curve: Sequence[float]) -> list[float]:
    """
    Running peak-to-trough drawdown at each point, as a positive fraction
    (0.25 = -25% from the prior peak). Mirrors metrics.max_drawdown's definition
    so the chart and the report agree. Empty in -> empty out.
    """
    out: list[float] = []
    if not equity_curve:
        return out
    peak = equity_curve[0]
    for value in equity_curve:
        if value > peak:
            peak = value
        out.append((peak - value) / peak if peak > 0 else 0.0)
    return out


def _resolve_out_path(out: str | None, mode: str) -> Path:
    """Default output lands under logs/ keyed by mode; an explicit --out wins."""
    if out:
        return Path(out)
    return DEFAULT_LOG_DIR / f"equity_{mode}.png"


def render_equity_png(store: StateStore, mode: str, out_path: Path) -> bool:
    """
    Draw the equity + drawdown chart for `mode` to `out_path`. Returns True on a
    successful render, False if there's nothing to plot or matplotlib is missing.

    matplotlib is imported here (lazily, Agg backend) so importing this module is
    free of side effects and missing matplotlib never breaks an import.
    """
    rows = store.history(mode)
    if not rows:
        print(f"No '{mode}' runs recorded yet — nothing to plot.")
        return False

    try:
        import matplotlib
        matplotlib.use("Agg")  # headless: no display needed, deterministic raster
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001 — any import/backend failure degrades gracefully
        print(
            "matplotlib is not installed — skipping the equity plot.\n"
            "Install it (pip install matplotlib) to enable PNG charts; "
            "scripts/report.py gives the same data as text without it."
        )
        return False

    equities = [float(r["equity"]) for r in rows]
    drawdowns = [d * 100.0 for d in drawdown_series(equities)]  # percent, for readability
    x = list(range(len(equities)))

    mdd = metrics.max_drawdown(equities)
    total_ret = metrics.total_return(equities)
    first, last = rows[0]["ts"][:10], rows[-1]["ts"][:10]

    fig, (ax_eq, ax_dd) = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )

    ax_eq.plot(x, equities, color="#2e7d32", linewidth=1.6, label="equity")
    ax_eq.fill_between(x, equities, min(equities), color="#2e7d32", alpha=0.08)
    ax_eq.set_ylabel("equity ($)")
    ax_eq.set_title(
        f"APEX QUANT — {mode} equity   "
        f"{first}..{last}   total {total_ret:+.2%}   max DD {mdd:.1%}"
    )
    ax_eq.grid(True, alpha=0.3)
    ax_eq.legend(loc="upper left")

    ax_dd.fill_between(x, drawdowns, 0.0, color="#c62828", alpha=0.35)
    ax_dd.plot(x, drawdowns, color="#c62828", linewidth=1.0)
    ax_dd.set_ylabel("drawdown (%)")
    ax_dd.set_xlabel("cycle")
    ax_dd.invert_yaxis()  # deeper drawdowns hang downward
    ax_dd.grid(True, alpha=0.3)

    fig.tight_layout()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=120)
    plt.close(fig)

    print(f"Wrote {len(equities)} cycles -> {out_path}")
    return True


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="plot_equity",
        description="Render the run_once equity curve + drawdown to a PNG.",
    )
    parser.add_argument(
        "mode", nargs="?", default="paper",
        help="trading mode to plot (default: paper)",
    )
    parser.add_argument(
        "--out", default=None,
        help="output PNG path (default: logs/equity_<mode>.png)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    out_path = _resolve_out_path(args.out, args.mode)
    store = StateStore()
    try:
        render_equity_png(store, args.mode, out_path)
    finally:
        store.close()
    return 0  # never fail the caller: missing data / matplotlib is a soft skip


if __name__ == "__main__":
    raise SystemExit(main())
