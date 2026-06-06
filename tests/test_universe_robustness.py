"""
Tests for scripts.universe_robustness — pure helpers only.

Covers:
  draw_subset  — determinism, correct size, no duplicates, all from universe,
                 sorted output, edge cases, ValueError on bad size.
  filter_events_to — keeps matching tickers, drops others, empty inputs,
                     benchmark always passable, preserves order.

No backtests, no real data, no network. All inputs are tiny synthetic fixtures.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apex.core.events import MarketEvent
from apex.core.models import AssetClass, Bar, Symbol
from scripts.universe_robustness import draw_subset, filter_events_to

# ------------------------------------------------------------------ fixtures


_UNIVERSE_10 = ["AAPL", "MSFT", "GOOG", "AMZN", "META", "NVDA", "TSLA", "JPM", "BAC", "WFC"]


def _make_event(ticker: str, ts: datetime, price: Decimal = Decimal("100")) -> MarketEvent:
    cls = AssetClass.ETF if ticker == "SPY" else AssetClass.EQUITY
    return MarketEvent(
        bar=Bar(
            symbol=Symbol(ticker, cls),
            timestamp=ts,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal("1000"),
        )
    )


def _event_stream(tickers: list[str], n_bars: int = 5) -> list[MarketEvent]:
    """Generate n_bars events per ticker, interleaved chronologically."""
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    evs = []
    for i in range(n_bars):
        ts = base + timedelta(days=i)
        for t in tickers:
            evs.append(_make_event(t, ts))
    evs.sort(key=lambda e: (e.bar.timestamp, e.bar.symbol.ticker))
    return evs


# ================================================================== draw_subset


class TestDrawSubset:
    def test_correct_size(self):
        result = draw_subset(_UNIVERSE_10, 5, seed=0)
        assert len(result) == 5

    def test_all_from_universe(self):
        result = draw_subset(_UNIVERSE_10, 7, seed=42)
        for t in result:
            assert t in _UNIVERSE_10, f"{t!r} not in universe"

    def test_no_duplicates(self):
        result = draw_subset(_UNIVERSE_10, 8, seed=99)
        assert len(result) == len(set(result)), "subset contains duplicates"

    def test_sorted_output(self):
        result = draw_subset(_UNIVERSE_10, 6, seed=5)
        assert result == sorted(result), "subset is not sorted"

    def test_deterministic_same_seed(self):
        a = draw_subset(_UNIVERSE_10, 5, seed=7)
        b = draw_subset(_UNIVERSE_10, 5, seed=7)
        assert a == b, "same seed produced different subsets"

    def test_different_seeds_differ(self):
        """Different seeds should (almost always) produce different subsets."""
        results = {tuple(draw_subset(_UNIVERSE_10, 5, seed=s)) for s in range(20)}
        # With 10 choose 5 = 252 possible subsets and 20 draws it's virtually
        # impossible for all 20 to be identical.
        assert len(results) > 1, "all seeds produced the same subset"

    def test_full_universe_size(self):
        result = draw_subset(_UNIVERSE_10, len(_UNIVERSE_10), seed=0)
        assert sorted(result) == sorted(_UNIVERSE_10)

    def test_size_one(self):
        result = draw_subset(_UNIVERSE_10, 1, seed=3)
        assert len(result) == 1
        assert result[0] in _UNIVERSE_10

    def test_size_zero_raises(self):
        with pytest.raises(ValueError):
            draw_subset(_UNIVERSE_10, 0, seed=0)

    def test_size_too_large_raises(self):
        with pytest.raises(ValueError):
            draw_subset(_UNIVERSE_10, len(_UNIVERSE_10) + 1, seed=0)

    def test_negative_size_raises(self):
        with pytest.raises(ValueError):
            draw_subset(_UNIVERSE_10, -1, seed=0)

    def test_deduplicates_input_universe(self):
        """Duplicates in the input universe should be collapsed before sampling."""
        duped = _UNIVERSE_10[:3] + _UNIVERSE_10[:3]  # 6 items but only 3 unique
        result = draw_subset(duped, 2, seed=0)
        assert len(result) == 2
        assert len(set(result)) == 2
        for t in result:
            assert t in _UNIVERSE_10[:3]

    def test_single_element_universe(self):
        result = draw_subset(["ONLY"], 1, seed=0)
        assert result == ["ONLY"]

    def test_different_universes_differ(self):
        u1 = _UNIVERSE_10[:5]
        u2 = _UNIVERSE_10[5:]
        r1 = draw_subset(u1, 3, seed=0)
        r2 = draw_subset(u2, 3, seed=0)
        # They come from disjoint universes so they can't be equal
        assert set(r1).isdisjoint(set(r2))

    def test_seed_independence_from_universe_order(self):
        """Reversing universe order with same seed yields different (but valid) subset."""
        forward = draw_subset(_UNIVERSE_10, 4, seed=1)
        backward = draw_subset(list(reversed(_UNIVERSE_10)), 4, seed=1)
        # Both are valid subsets (items from their respective inputs)
        for t in forward:
            assert t in _UNIVERSE_10
        for t in backward:
            assert t in _UNIVERSE_10


# ================================================================== filter_events_to


class TestFilterEventsTo:
    def test_keeps_matching_tickers(self):
        evs = _event_stream(["AAPL", "MSFT", "GOOG"], n_bars=3)
        result = filter_events_to(evs, ["AAPL", "MSFT"])
        tickers = {e.bar.symbol.ticker for e in result}
        assert tickers == {"AAPL", "MSFT"}

    def test_drops_non_matching(self):
        evs = _event_stream(["AAPL", "MSFT", "GOOG"], n_bars=4)
        result = filter_events_to(evs, ["AAPL"])
        assert all(e.bar.symbol.ticker == "AAPL" for e in result)

    def test_correct_count(self):
        n_bars = 5
        evs = _event_stream(["AAA", "BBB", "CCC"], n_bars=n_bars)
        result = filter_events_to(evs, ["AAA", "CCC"])
        assert len(result) == n_bars * 2

    def test_empty_tickers_returns_empty(self):
        evs = _event_stream(["AAPL", "MSFT"], n_bars=3)
        result = filter_events_to(evs, [])
        assert result == []

    def test_empty_events_returns_empty(self):
        result = filter_events_to([], ["AAPL"])
        assert result == []

    def test_preserves_chronological_order(self):
        evs = _event_stream(["AAPL", "MSFT"], n_bars=6)
        result = filter_events_to(evs, ["AAPL", "MSFT"])
        timestamps = [e.bar.timestamp for e in result]
        assert timestamps == sorted(timestamps)

    def test_returns_new_list_does_not_mutate(self):
        evs = _event_stream(["AAPL", "MSFT"], n_bars=3)
        original_len = len(evs)
        filter_events_to(evs, ["AAPL"])
        assert len(evs) == original_len, "filter_events_to mutated the input list"

    def test_ticker_not_in_events_is_harmless(self):
        evs = _event_stream(["AAPL"], n_bars=3)
        result = filter_events_to(evs, ["AAPL", "NONEXISTENT"])
        assert all(e.bar.symbol.ticker == "AAPL" for e in result)
        assert len(result) == 3

    def test_full_pass_when_all_tickers_match(self):
        evs = _event_stream(["AAPL", "MSFT"], n_bars=4)
        result = filter_events_to(evs, ["AAPL", "MSFT"])
        assert len(result) == len(evs)

    def test_benchmark_included_when_listed(self):
        evs = _event_stream(["AAPL", "SPY"], n_bars=3)
        result = filter_events_to(evs, ["AAPL", "SPY"])
        tickers = {e.bar.symbol.ticker for e in result}
        assert "SPY" in tickers

    def test_duplicate_tickers_in_whitelist_handled(self):
        """Passing duplicate tickers in the whitelist should not double-count events."""
        evs = _event_stream(["AAPL", "MSFT"], n_bars=4)
        result = filter_events_to(evs, ["AAPL", "AAPL", "MSFT"])
        # frozenset deduplicates the whitelist internally; no event duplication
        assert len(result) == 4 * 2
