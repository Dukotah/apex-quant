"""
Tests for scripts.compare_strategies — ranks library strategies by a chosen
metric from stored backtest results. The pure core (rank_strategies /
compare_strategies) is tested with hand-checked values and edge cases; the JSON
loader is tested via a tmp file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts.compare_strategies import (
    build_parser,
    compare_strategies,
    load_results,
    rank_strategies,
    supported_metrics,
)

UTC = timezone.utc


# ----------------------------------------------------------------- pure core


def test_zero_side_effects_on_import():
    # Importing the module (done at top) must not have created files or state.
    # Re-importing is a no-op; this just asserts the public surface exists.
    assert "sharpe" in supported_metrics()
    assert "max_drawdown" in supported_metrics()


def test_ranks_higher_total_return_first():
    results = {
        "flat": [100.0, 100.0],  # 0% return
        "winner": [100.0, 130.0],  # +30%
        "loser": [100.0, 90.0],  # -10%
    }
    ranked = rank_strategies(results, "total_return")
    assert [s.name for s in ranked] == ["winner", "flat", "loser"]
    # total_return is exact: 130/100 - 1 = 0.30
    assert ranked[0].value == pytest.approx(0.30)
    assert ranked[1].value == pytest.approx(0.0)
    assert ranked[2].value == pytest.approx(-0.10)


def test_max_drawdown_is_lower_is_better():
    results = {
        "smooth": [100.0, 101.0, 102.0],  # 0 drawdown
        "bumpy": [100.0, 80.0, 110.0],  # 20% drawdown
    }
    ranked = rank_strategies(results, "max_drawdown")
    # Lower drawdown ranks first.
    assert [s.name for s in ranked] == ["smooth", "bumpy"]
    assert ranked[0].value == pytest.approx(0.0)
    assert ranked[1].value == pytest.approx(0.20)  # (100-80)/100


def test_too_short_curve_is_unscored_and_sorts_last():
    results = {
        "good": [100.0, 110.0],
        "empty": [],
        "single": [100.0],
    }
    ranked = rank_strategies(results, "total_return")
    assert ranked[0].name == "good" and ranked[0].scored
    # Unscored strategies sort last, alphabetically among themselves.
    tail = [s.name for s in ranked[1:]]
    assert tail == ["empty", "single"]
    assert all(s.value is None for s in ranked[1:])
    assert all(not s.scored for s in ranked[1:])


def test_ties_break_alphabetically():
    results = {
        "bravo": [100.0, 110.0],
        "alpha": [100.0, 110.0],  # identical curve -> identical metric
    }
    ranked = rank_strategies(results, "total_return")
    assert [s.name for s in ranked] == ["alpha", "bravo"]


def test_unknown_metric_raises():
    with pytest.raises(ValueError):
        rank_strategies({"x": [1.0, 2.0]}, "not_a_metric")


def test_points_count_recorded():
    results = {"a": [100.0, 101.0, 102.0, 103.0]}
    ranked = rank_strategies(results, "sharpe")
    assert ranked[0].points == 4


def test_sharpe_matches_metrics_module():
    # The core must delegate to apex.validation.metrics, not reinvent the math.
    from apex.validation import metrics

    curve = [100.0, 101.0, 103.0, 102.0, 105.0, 104.0]
    expected = metrics.sharpe_ratio(metrics.returns_from_equity(curve))
    ranked = rank_strategies({"s": curve}, "sharpe")
    assert ranked[0].value == pytest.approx(expected)


# ----------------------------------------------------------------- rendering


def test_compare_output_is_deterministic_with_injected_clock():
    results = {"winner": [100.0, 130.0], "loser": [100.0, 90.0]}
    ts = datetime(2024, 6, 1, 12, 30, tzinfo=UTC)
    out_a = compare_strategies(results, "total_return", generated_at=ts)
    out_b = compare_strategies(results, "total_return", generated_at=ts)
    assert out_a == out_b
    assert "STRATEGY COMPARISON" in out_a
    assert "metric total_return" in out_a
    assert "2024-06-01 12:30Z" in out_a
    # winner listed before loser
    assert out_a.index("winner") < out_a.index("loser")


def test_compare_top_limits_rows():
    results = {f"s{i}": [100.0, 100.0 + i] for i in range(5)}
    out = compare_strategies(results, "total_return", top=2)
    # Only the two best (largest gains: s4, s3) appear.
    assert "s4" in out and "s3" in out
    assert "s0" not in out and "s1" not in out


def test_compare_empty_results():
    out = compare_strategies({}, "sharpe")
    assert "no strategy results to compare" in out


def test_compare_unscored_shows_na():
    out = compare_strategies({"x": [100.0]}, "total_return")
    assert "n/a" in out


# ----------------------------------------------------------------- I/O wrapper


def test_load_results_roundtrip(tmp_path):
    p = tmp_path / "results.json"
    data = {"alpha": [100.0, 110.0], "beta": [100, 90]}
    p.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_results(str(p))
    assert loaded == {"alpha": [100.0, 110.0], "beta": [100.0, 90.0]}
    # ints coerced to float
    assert all(isinstance(x, float) for x in loaded["beta"])


def test_load_results_rejects_non_object(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError):
        load_results(str(p))


def test_load_results_rejects_bad_curve(tmp_path):
    p = tmp_path / "bad2.json"
    p.write_text(json.dumps({"x": "not a list"}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_results(str(p))


# ----------------------------------------------------------------- CLI parser


def test_parser_defaults_and_choices():
    parser = build_parser()
    args = parser.parse_args(["results.json"])
    assert args.results == "results.json"
    assert args.metric == "sharpe"
    assert args.top is None


def test_parser_rejects_unknown_metric():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["results.json", "--metric", "bogus"])
