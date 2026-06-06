"""
tests/test_temporal_robustness.py
===================================
Unit tests for scripts.temporal_robustness — pure helper functions only.
No backtests, no network, no real CSV data. Fast and fully offline.

Tested:
  - slice_into_periods (edge cases, distribution, determinism)
  - slice_expanding    (anchored start, final slice = full, edge cases)
  - _date_range_label  (formatting)
  - _period_years      (duration calculation)
  - _verdict           (consistent vs regime-dependent string logic)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from apex.core.events import MarketEvent
from apex.core.models import AssetClass, Bar, Symbol
from scripts.temporal_robustness import (
    _date_range_label,
    _period_years,
    _verdict,
    slice_expanding,
    slice_into_periods,
)

# ------------------------------------------------------------------ helpers


def _make_events(n: int, ticker: str = "AAPL", step_days: int = 1) -> list[MarketEvent]:
    """Create `n` synthetic MarketEvents spaced `step_days` apart, deterministic."""
    base = datetime(2010, 1, 4, tzinfo=timezone.utc)
    sym = Symbol(ticker, AssetClass.EQUITY)
    price = Decimal("100")
    events: list[MarketEvent] = []
    for i in range(n):
        ts = base + timedelta(days=i * step_days)
        bar = Bar(
            symbol=sym,
            timestamp=ts,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal("1000"),
        )
        events.append(MarketEvent(bar=bar))
    return events


# ================================================================== slice_into_periods


class TestSliceIntoPeriods:
    def test_empty_events_returns_empty(self):
        assert slice_into_periods([], 5) == []

    def test_zero_periods_returns_empty(self):
        evs = _make_events(10)
        assert slice_into_periods(evs, 0) == []

    def test_negative_periods_returns_empty(self):
        evs = _make_events(10)
        assert slice_into_periods(evs, -3) == []

    def test_one_period_returns_all_events(self):
        evs = _make_events(10)
        result = slice_into_periods(evs, 1)
        assert len(result) == 1
        assert len(result[0]) == 10

    def test_exact_division(self):
        # 12 events, 3 periods → 4 each
        evs = _make_events(12)
        result = slice_into_periods(evs, 3)
        assert len(result) == 3
        assert all(len(s) == 4 for s in result)

    def test_uneven_division_distributes_remainder(self):
        # 10 events, 3 periods → [4, 3, 3]  (remainder=1 goes to first slice)
        evs = _make_events(10)
        result = slice_into_periods(evs, 3)
        assert len(result) == 3
        assert sum(len(s) for s in result) == 10
        # First slice gets the extra
        assert len(result[0]) == 4
        assert len(result[1]) == 3
        assert len(result[2]) == 3

    def test_no_events_are_dropped(self):
        for n in (1, 7, 13, 100):
            for p in (1, 3, 5, 13):
                evs = _make_events(n)
                slices = slice_into_periods(evs, p)
                assert sum(len(s) for s in slices) == n, f"n={n}, p={p}"

    def test_slices_are_consecutive_and_non_overlapping(self):
        evs = _make_events(20)
        slices = slice_into_periods(evs, 4)
        reconstructed = [ev for s in slices for ev in s]
        # Order and identity must match
        for orig, got in zip(evs, reconstructed):
            assert orig.bar.timestamp == got.bar.timestamp

    def test_more_periods_than_events_caps_at_n_events(self):
        evs = _make_events(5)
        result = slice_into_periods(evs, 100)
        # At most 5 slices (one per event)
        assert len(result) == 5
        assert all(len(s) == 1 for s in result)

    def test_five_periods_on_twenty_one_events(self):
        # 21 / 5 = 4 r1 → first slice has 5, remaining four have 4
        evs = _make_events(21)
        result = slice_into_periods(evs, 5)
        assert len(result) == 5
        assert sum(len(s) for s in result) == 21
        assert len(result[0]) == 5
        for s in result[1:]:
            assert len(s) == 4

    def test_deterministic(self):
        evs = _make_events(30)
        a = slice_into_periods(evs, 4)
        b = slice_into_periods(evs, 4)
        assert [len(s) for s in a] == [len(s) for s in b]
        for sa, sb in zip(a, b):
            for ea, eb in zip(sa, sb):
                assert ea.bar.timestamp == eb.bar.timestamp


# ================================================================== slice_expanding


class TestSliceExpanding:
    def test_empty_events_returns_empty(self):
        assert slice_expanding([], 3) == []

    def test_zero_periods_returns_empty(self):
        evs = _make_events(10)
        assert slice_expanding(evs, 0) == []

    def test_one_period_returns_full_stream(self):
        evs = _make_events(10)
        result = slice_expanding(evs, 1)
        assert len(result) == 1
        assert len(result[0]) == 10

    def test_final_window_is_always_full_stream(self):
        for n in (10, 20, 25):
            for p in (2, 3, 5):
                evs = _make_events(n)
                result = slice_expanding(evs, p)
                assert len(result[-1]) == n, f"n={n}, p={p}"

    def test_all_windows_anchored_at_start(self):
        evs = _make_events(20)
        result = slice_expanding(evs, 4)
        for s in result:
            # Every window starts at index 0 (same first event timestamp)
            assert s[0].bar.timestamp == evs[0].bar.timestamp

    def test_windows_are_strictly_growing(self):
        evs = _make_events(20)
        result = slice_expanding(evs, 4)
        lengths = [len(s) for s in result]
        for a, b in zip(lengths, lengths[1:]):
            assert b > a, f"Not strictly growing: {lengths}"

    def test_total_coverage_monotone_increasing(self):
        evs = _make_events(12)
        result = slice_expanding(evs, 3)
        lengths = [len(s) for s in result]
        assert lengths == sorted(lengths)
        assert lengths[-1] == 12

    def test_more_periods_than_events_caps(self):
        evs = _make_events(4)
        result = slice_expanding(evs, 100)
        # Must not crash; final window is full
        assert len(result[-1]) == 4

    def test_deterministic(self):
        evs = _make_events(30)
        a = slice_expanding(evs, 5)
        b = slice_expanding(evs, 5)
        assert [len(s) for s in a] == [len(s) for s in b]


# ================================================================== _date_range_label


class TestDateRangeLabel:
    def test_empty_returns_dash(self):
        assert _date_range_label([]) == "-"

    def test_single_event(self):
        evs = _make_events(1)
        label = _date_range_label(evs)
        assert label == "2010-01-04..2010-01-04"

    def test_two_events(self):
        evs = _make_events(2, step_days=30)
        label = _date_range_label(evs)
        assert label.startswith("2010-01-04")
        assert ".." in label

    def test_format(self):
        evs = _make_events(10, step_days=365)
        label = _date_range_label(evs)
        parts = label.split("..")
        assert len(parts) == 2
        for part in parts:
            # YYYY-MM-DD
            assert len(part) == 10
            assert part[4] == "-"
            assert part[7] == "-"

    def test_first_before_last(self):
        evs = _make_events(50, step_days=7)
        label = _date_range_label(evs)
        first, last = label.split("..")
        assert first < last


# ================================================================== _period_years


class TestPeriodYears:
    def test_empty_returns_zero(self):
        assert _period_years([]) == 0.0

    def test_single_event_returns_zero(self):
        evs = _make_events(1)
        assert _period_years(evs) == 0.0

    def test_roughly_one_year(self):
        # 365 days apart
        evs = _make_events(2, step_days=365)
        years = _period_years(evs)
        assert 0.95 < years < 1.05

    def test_roughly_five_years(self):
        # step_days=1 → 365*5 ≈ 1825 days
        evs = _make_events(1826, step_days=1)
        years = _period_years(evs)
        assert 4.9 < years < 5.1

    def test_same_day_two_events(self):
        # Both at the same timestamp → 0 days → 0 years
        sym = Symbol("X", AssetClass.EQUITY)
        ts = datetime(2020, 6, 1, tzinfo=timezone.utc)
        bar = Bar(
            symbol=sym,
            timestamp=ts,
            open=Decimal("10"),
            high=Decimal("10"),
            low=Decimal("10"),
            close=Decimal("10"),
            volume=Decimal("1"),
        )
        evs = [MarketEvent(bar=bar), MarketEvent(bar=bar)]
        assert _period_years(evs) == 0.0


# ================================================================== _verdict


class TestVerdict:
    def _row(self, sharpe_2x: float, label: str = "2010..2015", idx: int = 0) -> dict:
        return {
            "window_type": "consecutive",
            "period": idx + 1,
            "label": label,
            "years": 5.0,
            "n_bars": 100,
            "n_trades": 50,
            "sharpe_1x": sharpe_2x + 0.3,
            "sharpe_2x": sharpe_2x,
        }

    def test_empty_rows_returns_inconclusive(self):
        v = _verdict([], [])
        assert "no sub-period" in v.lower() or "inconclusive" in v.lower()

    def test_all_positive_is_consistent(self):
        rows = [self._row(0.5, f"label{i}", i) for i in range(5)]
        v = _verdict(rows, [])
        assert "consistent" in v.lower()
        assert "regime-dependent" not in v.lower()

    def test_zero_sharpe_is_regime_dependent(self):
        rows = [
            self._row(0.8, "2010..2015", 0),
            self._row(0.0, "2015..2020", 1),  # exactly 0.0 → failing
            self._row(0.6, "2020..2026", 2),
        ]
        v = _verdict(rows, [])
        assert "regime-dependent" in v.lower()

    def test_negative_sharpe_is_regime_dependent(self):
        rows = [
            self._row(1.2, "2010..2015", 0),
            self._row(-0.3, "2015..2020", 1),
        ]
        v = _verdict(rows, [])
        assert "regime-dependent" in v.lower()

    def test_verdict_mentions_count(self):
        rows = [self._row(0.7, f"L{i}", i) for i in range(4)]
        v = _verdict(rows, [])
        assert "4" in v  # mentions the total count

    def test_verdict_mentions_min_sharpe_when_consistent(self):
        rows = [self._row(0.5, "A", 0), self._row(0.9, "B", 1)]
        v = _verdict(rows, [])
        # The min Sharpe@2x (0.5) should appear in the string
        assert "0.50" in v

    def test_regime_dependent_mentions_failing_label(self):
        rows = [
            self._row(1.0, "2005..2010", 0),
            self._row(-0.5, "2010..2015", 1),
            self._row(0.8, "2015..2020", 2),
        ]
        v = _verdict(rows, [])
        assert "2010..2015" in v
