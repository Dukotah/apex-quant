"""
scripts/trade_log_export.py
===========================
Export the trade / fill log to CSV. The cron entry point (``scripts.run_once``)
persists one audit row per evaluation cycle to a SQLite state DB — when it ran,
the mode, the equity, position count, and how many orders/fills the cycle
produced, plus a JSON snapshot of the open book. This script flattens that
auditable trail into a flat CSV so you can open the trade log in a spreadsheet,
diff it, or feed it to the apex-trader dashboard.

Run:  python -m scripts.trade_log_export                       # paper rows -> stdout
      python -m scripts.trade_log_export --mode live           # a different mode
      python -m scripts.trade_log_export -o trades.csv         # write to a file
      python -m scripts.trade_log_export --db state/apex_state.db --mode paper

Read-only: it never touches the broker, never opens the network, and never
writes to the state DB. The CSV-building core (``rows_to_csv``) is pure and
deterministic — it takes already-fetched audit rows and a list of fields, so it
is fully testable without a database, a clock, or any I/O.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
from typing import Iterable, List, Mapping, Optional, Sequence

# The audit columns persisted by scripts.run_once.StateStore, in a stable,
# spreadsheet-friendly order. ``positions`` (a JSON blob) is exported last.
DEFAULT_FIELDS: tuple[str, ...] = (
    "ts",
    "mode",
    "equity",
    "num_positions",
    "orders",
    "fills",
    "halted",
    "positions",
)


# --------------------------------------------------------------------- pure core


def _cell(row: Mapping[str, object], field: str) -> str:
    """
    Render one cell deterministically. Missing keys become "" (fail soft — a
    partial row never raises); a JSON ``positions`` blob is re-serialised with
    sorted keys so identical books always produce byte-identical CSV.

    Works for plain dicts AND ``sqlite3.Row`` (whose ``in`` tests values, not
    keys), so we look the value up via ``[]`` and treat a lookup miss as empty.
    """
    try:
        value = row[field]
    except (KeyError, IndexError):
        return ""
    if value is None:
        return ""
    if field == "positions" and isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return value
        return json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return str(value)


def rows_to_csv(
    rows: Iterable[Mapping[str, object]],
    fields: Sequence[str] = DEFAULT_FIELDS,
) -> str:
    """
    Pure, deterministic CSV serialiser for the trade/fill audit log.

    ``rows`` is any iterable of mappings (a list of dicts in tests, ``sqlite3.Row``
    objects in production — both support ``in``/``[]``). ``fields`` selects and
    orders the columns. Always emits a header row; rows missing a column get an
    empty cell rather than raising. Output uses ``\\r\\n`` line terminators (the
    csv-module/RFC-4180 default) and is identical for identical inputs — no
    clock, no randomness, no I/O.
    """
    field_list: List[str] = list(fields)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(field_list)
    for row in rows:
        writer.writerow([_cell(row, f) for f in field_list])
    return buf.getvalue()


# ----------------------------------------------------------------- I/O boundary


def _load_rows(db_path: str, mode: Optional[str]) -> List[Mapping[str, object]]:
    """
    Read the audit trail from the state DB. Lazy-imports ``scripts.run_once`` so
    merely importing this module has ZERO side effects (no DB connection). When
    ``mode`` is None every recorded mode is returned, oldest -> newest.
    """
    from scripts.run_once import StateStore  # lazy: keep import side-effect-free

    store = StateStore(db_path)
    try:
        if mode is not None:
            return list(store.history(mode))
        # No mode filter: pull paper + live (and anything else) in time order.
        seen: List[Mapping[str, object]] = []
        for m in ("backtest", "paper", "live"):
            seen.extend(store.history(m))
        seen.sort(key=lambda r: str(r["ts"]))
        return seen
    finally:
        store.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trade_log_export",
        description="Export the apex-quant trade/fill audit log to CSV (read-only).",
    )
    parser.add_argument(
        "--db",
        default="state/apex_state.db",
        help="Path to the SQLite state DB (default: state/apex_state.db).",
    )
    parser.add_argument(
        "--mode",
        default="paper",
        help="Run mode to export (paper/live/backtest). Use 'all' for every mode.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Write CSV to this file instead of stdout.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    mode = None if args.mode == "all" else args.mode
    rows = _load_rows(args.db, mode)
    csv_text = rows_to_csv(rows)
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="") as fh:
            fh.write(csv_text)
        print(f"Wrote {len(rows)} row(s) to {args.output}")
    else:
        # csv already emits \r\n; write raw so terminators survive.
        import sys

        sys.stdout.write(csv_text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
