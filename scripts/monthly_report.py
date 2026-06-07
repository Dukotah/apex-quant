"""
scripts/monthly_report.py
=========================
Monthly-returns table. Turns the run_once state DB equity history (one row per
cron cycle) into the classic "monthly returns" grid quants stare at: one row per
calendar year, one column per month, each cell the equity return over that month,
plus a year-to-date total column.

Run:  python -m scripts.monthly_report            # default state DB, paper mode
      python -m scripts.monthly_report live       # a different mode
      python -m scripts.monthly_report --db state/apex_state.db --mode paper

Pure read-only; never touches the broker, never sends anything over the network.
The table-building core (``build_monthly_table``) is a pure, deterministic
function of (timestamp, equity) pairs — every "now" is the timestamp baked into
the recorded rows, so the same DB always renders the identical table.
"""
from __future__ import annotations

import argparse
from typing import List, Optional, Sequence, Tuple

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --------------------------------------------------------------------- pure core

def _period_key(ts: str) -> Tuple[int, int]:
    """(year, month) from an ISO timestamp string like '2024-03-07T...'. Fails closed."""
    year = int(ts[0:4])
    month = int(ts[5:7])
    return year, month


def monthly_equity_endpoints(
    points: Sequence[Tuple[str, float]],
) -> List[Tuple[int, int, float]]:
    """
    Collapse a (timestamp, equity) series into (year, month, month_end_equity).

    Points must be in oldest->newest order (the order ``StateStore.history``
    returns). For each calendar month we keep the LAST equity seen in it — the
    month-end mark. Returns one entry per month that has data, in order.
    """
    out: List[Tuple[int, int, float]] = []
    last_key: Optional[Tuple[int, int]] = None
    for ts, equity in points:
        try:
            key = _period_key(ts)
        except (ValueError, IndexError):
            continue  # fail closed: skip an unparseable row rather than guess
        if key != last_key:
            out.append((key[0], key[1], float(equity)))
            last_key = key
        else:
            out[-1] = (key[0], key[1], float(equity))
    return out


def monthly_returns(
    points: Sequence[Tuple[str, float]],
    *,
    starting_equity: Optional[float] = None,
) -> List[Tuple[int, int, Optional[float]]]:
    """
    Per-month return as a fraction (0.012 = +1.2%), as (year, month, ret).

    A month's return is its month-end equity over the PRIOR month-end equity.
    The first month's return is None unless ``starting_equity`` is supplied (and
    non-zero), in which case it is measured against that opening balance — useful
    when the caller knows the account's funding level.

    Returns an empty list when there is no data. A None return means "not
    enough information for this month" — never a fabricated number.
    """
    endpoints = monthly_equity_endpoints(points)
    if not endpoints:
        return []

    out: List[Tuple[int, int, Optional[float]]] = []
    prev_equity: Optional[float] = (
        starting_equity if starting_equity not in (None, 0) else None
    )
    for year, month, equity in endpoints:
        if prev_equity is None or prev_equity == 0:
            ret: Optional[float] = None
        else:
            ret = equity / prev_equity - 1.0
        out.append((year, month, ret))
        prev_equity = equity
    return out


def _fmt_pct(ret: Optional[float], width: int = 7) -> str:
    if ret is None:
        return " " * (width - 2) + "--"
    return f"{ret:>+{width}.1%}"


def build_monthly_table(
    points: Sequence[Tuple[str, float]],
    *,
    mode: str = "paper",
    starting_equity: Optional[float] = None,
) -> str:
    """
    Render the monthly-returns grid as a fixed-width text table (pure function).

    One row per calendar year (Jan..Dec) plus a year-to-date 'YTD' column that
    compounds that year's available monthly returns. Months with no data show
    '--'. Deterministic: depends only on the supplied (timestamp, equity) pairs.
    """
    rows = monthly_returns(points, starting_equity=starting_equity)
    if not rows:
        return (f"No '{mode}' equity history recorded yet — "
                "the bot hasn't completed a cycle.")

    # Bucket returns by year -> {month: ret}.
    by_year: dict[int, dict[int, Optional[float]]] = {}
    for year, month, ret in rows:
        by_year.setdefault(year, {})[month] = ret

    header = "  year " + " ".join(f"{m:>7}" for m in MONTHS) + f" {'YTD':>8}"
    sep = "-" * len(header)
    lines = [
        "APEX QUANT — MONTHLY RETURNS",
        "=" * len(header),
        f"  mode {mode}",
        sep,
        header,
        sep,
    ]

    for year in sorted(by_year):
        cells = by_year[year]
        parts = [f"  {year:>4}"]
        ytd = 1.0
        have_ytd = False
        for m in range(1, 13):
            ret = cells.get(m)
            parts.append(_fmt_pct(ret))
            if ret is not None:
                ytd *= (1.0 + ret)
                have_ytd = True
        ytd_ret = (ytd - 1.0) if have_ytd else None
        parts.append(_fmt_pct(ytd_ret, width=8))
        lines.append(" ".join(parts))

    lines.append(sep)
    return "\n".join(lines)


# ------------------------------------------------------------------- I/O + main

def _load_points(db_path: Optional[str], mode: str) -> List[Tuple[str, float]]:
    """Read (ts, equity) pairs for `mode`, oldest->newest. Lazy imports; no logic here."""
    from scripts.run_once import StateStore  # lazy: keep module import side-effect free

    store = StateStore() if db_path is None else StateStore(db_path)
    try:
        rows = store.history(mode)
        return [(str(r["ts"]), float(r["equity"])) for r in rows]
    finally:
        store.close()


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="monthly_report",
        description="Print a monthly-returns table from the run_once state DB "
                    "equity history. Read-only; never contacts the broker.",
    )
    parser.add_argument(
        "mode", nargs="?", default="paper",
        help="execution mode to report on (default: paper)",
    )
    parser.add_argument(
        "--mode", dest="mode_opt", default=None,
        help="execution mode (overrides the positional argument if given)",
    )
    parser.add_argument(
        "--db", dest="db", default=None,
        help="path to the state DB (default: the run_once default location)",
    )
    parser.add_argument(
        "--start-equity", dest="start_equity", type=float, default=None,
        help="opening balance to measure the first month against (optional)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    args = _parse_args(argv)
    mode = args.mode_opt or args.mode
    points = _load_points(args.db, mode)
    print(build_monthly_table(points, mode=mode, starting_equity=args.start_equity))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
