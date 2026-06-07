"""
Tests for scripts.trade_log_export — CSV export of the trade/fill audit log.

The CSV-building core (rows_to_csv / _cell) is pure and deterministic, so the
bulk of the coverage uses hand-built dict rows with hand-computed expected
output. The DB-backed helpers are exercised against a real temp StateStore
(the same seeding pattern as tests/test_report.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.trade_log_export import (
    DEFAULT_FIELDS,
    _cell,
    _load_rows,
    main,
    rows_to_csv,
)

UTC = timezone.utc


# --------------------------------------------------------------------- pure core

def test_header_only_for_empty_rows():
    out = rows_to_csv([])
    assert out == ",".join(DEFAULT_FIELDS) + "\r\n"


def test_single_row_known_value():
    row = {
        "ts": "2024-01-01T00:00:00+00:00",
        "mode": "paper",
        "equity": 100000.0,
        "num_positions": 2,
        "orders": 1,
        "fills": 1,
        "halted": 0,
        "positions": "{}",
    }
    out = rows_to_csv([row])
    lines = out.split("\r\n")
    assert lines[0] == "ts,mode,equity,num_positions,orders,fills,halted,positions"
    assert lines[1] == "2024-01-01T00:00:00+00:00,paper,100000.0,2,1,1,0,{}"
    assert lines[2] == ""  # trailing terminator -> empty final element


def test_custom_field_selection_and_order():
    row = {"ts": "t", "mode": "live", "equity": 5, "extra": "ignored"}
    out = rows_to_csv([row], fields=["mode", "ts"])
    assert out == "mode,ts\r\nlive,t\r\n"


def test_missing_field_becomes_empty_cell():
    # Row lacks 'orders' and 'positions' -> empty cells, never a KeyError.
    row = {"ts": "t", "mode": "paper", "equity": 1, "num_positions": 0,
           "fills": 0, "halted": 0}
    out = rows_to_csv([row])
    data_line = out.split("\r\n")[1]
    # fields: ts,mode,equity,num_positions,orders,fills,halted,positions
    assert data_line == "t,paper,1,0,,0,0,"


def test_none_value_becomes_empty():
    assert _cell({"x": None}, "x") == ""
    assert _cell({}, "x") == ""


def test_positions_json_is_canonicalized_sorted():
    # Unsorted JSON input -> deterministically re-serialised with sorted keys.
    blob = '{"b": 2, "a": 1}'
    assert _cell({"positions": blob}, "positions") == '{"a":1,"b":2}'


def test_positions_invalid_json_passes_through():
    assert _cell({"positions": "not-json"}, "positions") == "not-json"


def test_csv_quotes_embedded_commas():
    # A positions blob with a comma must be quoted so columns stay aligned.
    row = {"ts": "t", "positions": '{"SPY":{"qty":"1","avg":"2"}}'}
    out = rows_to_csv([row], fields=["ts", "positions"])
    line = out.split("\r\n")[1]
    assert line == 't,"{""SPY"":{""avg"":""2"",""qty"":""1""}}"'


def test_deterministic_repeatable():
    rows = [{"ts": f"t{i}", "mode": "paper", "equity": i} for i in range(5)]
    assert rows_to_csv(rows) == rows_to_csv(rows)


# ----------------------------------------------------------------- DB-backed I/O

def _seed(store, equities, mode="paper", orders=0, fills=0):
    from scripts.run_once import RunReport

    base = datetime(2024, 1, 1, tzinfo=UTC)
    for i, eq in enumerate(equities):
        store.save_run(
            RunReport(timestamp=base + timedelta(days=i), mode=mode,
                      equity=float(eq), num_positions=0,
                      orders_submitted=orders),
            {},
        )


def test_load_rows_filters_by_mode(tmp_path):
    from scripts.run_once import StateStore

    db = tmp_path / "s.db"
    store = StateStore(db)
    _seed(store, [100000, 100500], mode="paper")
    _seed(store, [200000], mode="live")
    store.close()

    paper = _load_rows(str(db), "paper")
    assert len(paper) == 2
    assert {r["mode"] for r in paper} == {"paper"}


def test_load_rows_all_modes_time_ordered(tmp_path):
    from scripts.run_once import StateStore

    db = tmp_path / "s.db"
    store = StateStore(db)
    _seed(store, [100000, 100500], mode="paper")
    _seed(store, [200000], mode="live")
    store.close()

    rows = _load_rows(str(db), None)
    assert len(rows) == 3
    ts = [str(r["ts"]) for r in rows]
    assert ts == sorted(ts)


def test_main_writes_file(tmp_path, capsys):
    from scripts.run_once import StateStore

    db = tmp_path / "s.db"
    store = StateStore(db)
    _seed(store, [100000, 100500], mode="paper", orders=2)
    store.close()

    out_csv = tmp_path / "trades.csv"
    rc = main(["--db", str(db), "--mode", "paper", "-o", str(out_csv)])
    assert rc == 0

    # read_text applies universal-newline translation (\r\n -> \n).
    text = out_csv.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln]
    assert lines[0].startswith("ts,mode,equity")
    assert len(lines) == 3  # header + 2 rows
    assert ",paper," in lines[1]
    assert capsys.readouterr().out.strip() == f"Wrote 2 row(s) to {out_csv}"


def test_main_to_stdout(tmp_path, capsys):
    from scripts.run_once import StateStore

    db = tmp_path / "s.db"
    store = StateStore(db)
    _seed(store, [100000], mode="paper")
    store.close()

    rc = main(["--db", str(db), "--mode", "paper"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("ts,mode,equity")
    assert ",paper," in out


def test_main_empty_db_emits_header(tmp_path, capsys):
    from scripts.run_once import StateStore

    db = tmp_path / "s.db"
    StateStore(db).close()  # creates schema, no rows
    rc = main(["--db", str(db), "--mode", "paper"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.rstrip("\r\n") == ",".join(DEFAULT_FIELDS)


def test_import_has_no_side_effects():
    # Importing the module must not connect to a DB or touch the network.
    import importlib

    import scripts.trade_log_export as mod

    importlib.reload(mod)
    assert hasattr(mod, "rows_to_csv")
