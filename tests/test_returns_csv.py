"""
Tests for scripts.returns_csv — the equity/returns CSV exporter.

The pure core (``build_csv``) is tested directly against hand-computed values;
the DB-backed loader is exercised through a tmp state DB plus the file/stdout
paths of ``main``.
"""

from __future__ import annotations

import csv
import io

from scripts.returns_csv import CSV_HEADER, build_csv, main


def _parse(text):
    return list(csv.reader(io.StringIO(text)))


# --------------------------------------------------------------- pure core


def test_empty_rows_is_header_only():
    out = build_csv([])
    rows = _parse(out)
    assert rows == [list(CSV_HEADER)]


def test_single_row_has_blank_return():
    out = build_csv([("2024-01-01", 100000.0)])
    rows = _parse(out)
    assert rows[0] == list(CSV_HEADER)
    assert rows[1][0] == "2024-01-01"
    assert float(rows[1][1]) == 100000.0
    assert rows[1][2] == ""  # no prior point -> undefined return


def test_returns_are_hand_computed():
    # 100 -> 110 = +10%; 110 -> 99 = -10%.
    out = build_csv([("d0", 100.0), ("d1", 110.0), ("d2", 99.0)])
    rows = _parse(out)
    assert rows[1][2] == ""  # first row blank
    assert abs(float(rows[2][2]) - 0.10) < 1e-12  # +10%
    assert abs(float(rows[3][2]) - (-0.10)) < 1e-12  # -10%


def test_zero_prior_equity_yields_blank_return():
    out = build_csv([("d0", 0.0), ("d1", 100.0)])
    rows = _parse(out)
    assert rows[1][2] == ""  # first row always blank
    assert rows[2][2] == ""  # division by zero prior -> blank, never garbage


def test_deterministic_same_input_same_output():
    data = [("a", 100.0), ("b", 101.0), ("c", 102.5)]
    assert build_csv(data) == build_csv(data)


def test_equity_values_preserved():
    out = build_csv([("d0", 100000.0), ("d1", 100500.0)])
    rows = _parse(out)
    assert float(rows[1][1]) == 100000.0
    assert float(rows[2][1]) == 100500.0
    assert abs(float(rows[2][2]) - (100500.0 / 100000.0 - 1.0)) < 1e-12


# ----------------------------------------------------- module import is clean


def test_module_import_has_no_side_effects():
    import importlib

    import scripts.returns_csv as mod

    importlib.reload(mod)  # re-import must not raise / connect to anything


# -------------------------------------------------------- DB-backed main()


def _seed(db_path):
    from datetime import datetime, timedelta, timezone

    from scripts.run_once import RunReport, StateStore

    store = StateStore(db_path)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i, eq in enumerate([100000.0, 100500.0, 101000.0]):
        store.save_run(
            RunReport(timestamp=base + timedelta(days=i), mode="paper", equity=eq, num_positions=0),
            {},
        )
    store.close()


def test_main_writes_file(tmp_path, capsys):
    db = tmp_path / "s.db"
    _seed(db)
    out = tmp_path / "returns.csv"
    rc = main(["--mode", "paper", "--db", str(db), "--out", str(out)])
    assert rc == 0
    assert "wrote 3 row(s)" in capsys.readouterr().out

    rows = _parse(out.read_text(encoding="utf-8"))
    assert rows[0] == list(CSV_HEADER)
    assert len(rows) == 4  # header + 3 data rows
    assert rows[1][2] == ""  # first return blank
    assert abs(float(rows[2][2]) - (100500.0 / 100000.0 - 1.0)) < 1e-12


def test_main_stdout(tmp_path, capsys):
    db = tmp_path / "s.db"
    _seed(db)
    rc = main(["--mode", "paper", "--db", str(db)])
    assert rc == 0
    captured = capsys.readouterr().out
    rows = _parse(captured)
    assert rows[0] == list(CSV_HEADER)
    assert len(rows) == 4


def test_main_unknown_mode_is_header_only(tmp_path, capsys):
    db = tmp_path / "s.db"
    _seed(db)
    rc = main(["--mode", "nonexistent", "--db", str(db)])
    assert rc == 0
    rows = _parse(capsys.readouterr().out)
    assert rows == [list(CSV_HEADER)]
