"""
Tests for scripts.backtest_grid — the deterministic parameter-grid expander.

Pure, fast, no I/O. Hand-computed cartesian products plus edge cases.
"""

from __future__ import annotations

import pytest

from scripts.backtest_grid import (
    count_combinations,
    expand_grid,
    iter_combinations,
    label_combination,
    parse_param_specs,
    render_grid,
)

# ------------------------------------------------------------------ expand_grid


def test_expand_single_param():
    assert expand_grid({"a": [1, 2, 3]}) == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_expand_is_row_major_last_axis_fastest():
    # Hand-computed: 'a' outer, 'b' inner (fastest).
    grid = {"a": [1, 2], "b": ["x", "y"]}
    assert expand_grid(grid) == [
        {"a": 1, "b": "x"},
        {"a": 1, "b": "y"},
        {"a": 2, "b": "x"},
        {"a": 2, "b": "y"},
    ]


def test_expand_three_axes_count_and_order():
    grid = {"a": [1, 2], "b": [10, 20], "c": [100, 200]}
    combos = expand_grid(grid)
    assert len(combos) == 8  # 2 * 2 * 2
    assert combos[0] == {"a": 1, "b": 10, "c": 100}
    assert combos[1] == {"a": 1, "b": 10, "c": 200}  # last axis varies first
    assert combos[-1] == {"a": 2, "b": 20, "c": 200}


def test_expand_empty_grid_is_one_trivial_combination():
    assert expand_grid({}) == [{}]


def test_expand_with_empty_axis_yields_nothing():
    assert expand_grid({"a": [1, 2], "b": []}) == []


def test_expand_is_deterministic():
    grid = {"lookback": [100, 200, 300], "fast": [10, 20]}
    assert expand_grid(grid) == expand_grid(grid)


def test_expand_returns_independent_dicts():
    combos = expand_grid({"a": [1, 2]})
    combos[0]["a"] = 999
    assert combos[1] == {"a": 2}  # mutation didn't leak


# ------------------------------------------------------------- count vs expand


def test_count_matches_expand_length():
    grid = {"a": [1, 2, 3], "b": [4, 5], "c": [6]}
    assert count_combinations(grid) == 6
    assert count_combinations(grid) == len(expand_grid(grid))


def test_count_empty_grid_is_one():
    assert count_combinations({}) == 1


def test_count_empty_axis_is_zero():
    assert count_combinations({"a": [1], "b": []}) == 0


# --------------------------------------------------------- iter == expand order


def test_iter_matches_expand_exactly():
    grid = {"a": [1, 2], "b": ["x", "y", "z"]}
    assert list(iter_combinations(grid)) == expand_grid(grid)


def test_iter_empty_grid_yields_one():
    assert list(iter_combinations({})) == [{}]


def test_iter_empty_axis_yields_none():
    assert list(iter_combinations({"a": [], "b": [1]})) == []


# --------------------------------------------------------------- parse_param_specs


def test_parse_coerces_int_float_string():
    grid = parse_param_specs(["lookback=100,200", "rate=0.5,1.5", "name=spy,efa"])
    assert grid == {
        "lookback": [100, 200],
        "rate": [0.5, 1.5],
        "name": ["spy", "efa"],
    }
    assert all(isinstance(v, int) for v in grid["lookback"])
    assert all(isinstance(v, float) for v in grid["rate"])


def test_parse_preserves_order_and_dedupes():
    grid = parse_param_specs(["a=3,1,3,2,1"])
    assert grid["a"] == [3, 1, 2]  # first appearance kept, dupes dropped


def test_parse_int_and_float_are_distinct_values():
    # "1" -> int 1, "1.0" -> float 1.0 ; both kept (different types).
    grid = parse_param_specs(["a=1,1.0"])
    assert grid["a"] == [1, 1.0]
    assert [type(v).__name__ for v in grid["a"]] == ["int", "float"]


def test_parse_last_write_wins_keeps_position():
    grid = parse_param_specs(["a=1,2", "b=9", "a=5,6"])
    assert list(grid.keys()) == ["a", "b"]  # 'a' keeps its leading position
    assert grid["a"] == [5, 6]  # but later spec replaced values


def test_parse_skips_blank_tokens():
    assert parse_param_specs(["a=1,,2, ,3"])["a"] == [1, 2, 3]


@pytest.mark.parametrize("bad", ["noequals", "=1,2", "a=", "a=,, "])
def test_parse_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_param_specs([bad])


def test_parse_then_expand_end_to_end():
    grid = parse_param_specs(["lookback=100,200", "fast=10,20"])
    combos = expand_grid(grid)
    assert len(combos) == 4
    assert combos[0] == {"lookback": 100, "fast": 10}
    assert combos[-1] == {"lookback": 200, "fast": 20}


# ------------------------------------------------------------- label_combination


def test_label_basic():
    assert label_combination({"a": 1, "b": 2}) == "a=1 b=2"


def test_label_with_index_and_injected_timestamp():
    label = label_combination({"a": 1}, index=3, timestamp="2024-01-01T00:00:00Z")
    assert label == "#3 a=1  [2024-01-01T00:00:00Z]"


def test_label_empty_combo():
    assert label_combination({}) == "(no params)"


def test_label_is_deterministic_without_clock():
    combo = {"lookback": 200, "fast": 20}
    # No timestamp injected -> identical every call (no datetime.now()).
    assert label_combination(combo, index=0) == label_combination(combo, index=0)


# ------------------------------------------------------------------- render_grid


def test_render_reports_count_and_lines():
    out = render_grid({"a": [1, 2], "b": [3, 4]})
    assert "BACKTEST PARAMETER GRID" in out
    assert "combinations: 4" in out
    assert "#0 a=1 b=3" in out
    assert "#3 a=2 b=4" in out


def test_render_limit_shows_subset_but_full_count():
    out = render_grid({"a": [1, 2, 3]}, limit=1)
    assert "combinations: 3" in out
    assert "(showing 1)" in out
    assert "#0 a=1" in out
    assert "#1" not in out


def test_render_empty_grid_message():
    out = render_grid({"a": [1], "b": []})
    assert "combinations: 0" in out
    assert "nothing to sweep" in out


def test_render_injects_timestamp_not_now():
    out = render_grid({"a": [1]}, timestamp="2030-12-31T23:59:59Z")
    assert "[2030-12-31T23:59:59Z]" in out


# ------------------------------------------------------- import has no side effects


def test_module_import_is_clean():
    import importlib

    import scripts.backtest_grid as mod

    # Re-importing must not raise / mutate global state observably.
    importlib.reload(mod)
    assert hasattr(mod, "expand_grid")
