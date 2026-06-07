"""
scripts/returns_csv.py
======================
Export the equity and returns series to CSV from the run_once state DB.

Each cron cycle leaves one row in the state DB (ts, mode, equity, ...). This tool
turns that audit trail into a plain CSV with three columns — ts, equity, return —
where ``return`` is the period-over-period fraction of the equity curve (the first
row's return is blank, since there is no prior point). Feed it into a spreadsheet,
a notebook, or the apex-trader dashboard.

Run:  python -m scripts.returns_csv                       # paper mode -> stdout
      python -m scripts.returns_csv --mode live           # a different mode
      python -m scripts.returns_csv --out returns.csv     # write to a file
      python -m scripts.returns_csv --db state/apex_state.db --out r.csv

Pure read-only: it never touches the broker or the network. The CSV-building core
(``build_csv``) is a pure, deterministic function of the rows you hand it — no DB,
no clock, no I/O — so it is unit-tested directly. All state/config dependencies are
lazy-imported inside functions, so importing this module has ZERO side effects.
"""
from __future__ import annotations

import argparse
import io
from typing import List, Sequence, Tuple

CSV_HEADER: Tuple[str, str, str] = ("ts", "equity", "return")


def _fmt_return(prev: float, curr: float) -> str:
    """Period-over-period return as a string; blank if undefined (no/zero prior)."""
    if prev == 0:
        return ""
    return repr(curr / prev - 1.0)


def build_csv(rows: Sequence[Tuple[str, float]]) -> str:
    """
    Pure core: turn (timestamp, equity) rows — assumed oldest->newest — into CSV
    text with columns ts,equity,return. The first row's return is blank (no prior
    point). Insufficient data (empty rows) yields a header-only CSV. Deterministic:
    same rows in, same text out, no I/O, no wall-clock.
    """
    buf = io.StringIO()
    # csv via stdlib, but built on the rows we are handed so the core stays pure.
    import csv

    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)

    prev: float | None = None
    for ts, equity in rows:
        equity = float(equity)
        ret = "" if prev is None else _fmt_return(prev, equity)
        writer.writerow([ts, repr(equity), ret])
        prev = equity
    return buf.getvalue()


def _load_rows(db_path: str, mode: str) -> List[Tuple[str, float]]:
    """Read (ts, equity) rows for ``mode``, oldest->newest, from the state DB."""
    # Lazy import: keeps module import side-effect-free and config/state-dependency
    # free until the CLI actually runs.
    from scripts.run_once import StateStore

    store = StateStore(db_path)
    try:
        return [(r["ts"], float(r["equity"])) for r in store.history(mode)]
    finally:
        store.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="returns_csv",
        description="Export the equity and returns series to CSV from the state DB.",
    )
    parser.add_argument("--mode", default="paper",
                        help="run mode to export (default: paper)")
    parser.add_argument("--db", default=None,
                        help="path to the state DB (default: run_once's DEFAULT_STATE_PATH)")
    parser.add_argument("--out", default=None,
                        help="output CSV path (default: write to stdout)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Lazy import the default path only when needed — no import-time dependency.
    db_path = args.db
    if db_path is None:
        from scripts.run_once import DEFAULT_STATE_PATH
        db_path = str(DEFAULT_STATE_PATH)

    rows = _load_rows(db_path, args.mode)
    csv_text = build_csv(rows)

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as fh:
            fh.write(csv_text)
        print(f"wrote {len(rows)} row(s) for mode '{args.mode}' to {args.out}")
    else:
        try:
            import sys
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        print(csv_text, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
