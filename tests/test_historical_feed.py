"""
Tests for apex.data.historical_feed.

Covers the Phase-2 contract: chronological replay, multi-symbol interleaving,
UTC/Decimal normalization, bad-row skipping, latest-bar tracking, determinism,
and the connect/stream/disconnect lifecycle. Uses small inline CSV fixtures
written to tmp_path — stdlib only, no pandas needed for the CSV path.
"""
from __future__ import annotations

from datetime import timezone
from decimal import Decimal
from pathlib import Path

import pytest

from apex.core.events import MarketEvent
from apex.core.models import AssetClass, Symbol
from apex.data.historical_feed import HistoricalDataFeed

NVDA = Symbol("NVDA", AssetClass.EQUITY)
SPY = Symbol("SPY", AssetClass.ETF)


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------- fixtures

SINGLE_SYMBOL_CSV = """
timestamp,open,high,low,close,volume
2024-01-03,12.0,12.5,11.8,12.4,1000
2024-01-01,10.0,10.5,9.8,10.2,1500
2024-01-02,11.0,11.6,10.9,11.5,1200
"""  # intentionally out of order to prove the feed sorts it

MULTI_SYMBOL_CSV = """
timestamp,symbol,open,high,low,close,volume
2024-01-01,NVDA,10,11,9,10.5,100
2024-01-01,SPY,400,401,399,400.5,200
2024-01-02,SPY,401,402,400,401.5,210
2024-01-02,NVDA,10.5,12,10,11.8,110
"""


# ------------------------------------------------------------------------ tests

def test_single_symbol_replays_in_chronological_order(tmp_path):
    path = _write(tmp_path / "nvda.csv", SINGLE_SYMBOL_CSV)
    feed = HistoricalDataFeed([NVDA], path)
    feed.connect()
    events = list(feed.stream())

    assert all(isinstance(e, MarketEvent) for e in events)
    closes = [e.bar.close for e in events]
    times = [e.bar.timestamp for e in events]
    # Sorted ascending even though the source file was shuffled.
    assert times == sorted(times)
    assert closes == [Decimal("10.2"), Decimal("11.5"), Decimal("12.4")]


def test_stream_stops_after_last_bar(tmp_path):
    path = _write(tmp_path / "nvda.csv", SINGLE_SYMBOL_CSV)
    feed = HistoricalDataFeed([NVDA], path)
    feed.connect()
    gen = feed.stream()
    assert sum(1 for _ in gen) == 3
    with pytest.raises(StopIteration):
        next(gen)   # exhausted generator → backtest is over


def test_multi_symbol_interleaved_by_timestamp(tmp_path):
    path = _write(tmp_path / "multi.csv", MULTI_SYMBOL_CSV)
    feed = HistoricalDataFeed([NVDA, SPY], path)
    feed.connect()
    events = list(feed.stream())

    order = [(e.bar.timestamp, e.bar.symbol.ticker) for e in events]
    # Non-decreasing timestamps; within the same timestamp, ticker is the tie-break.
    assert order == sorted(order)
    # Two symbols on the same day appear adjacent, alphabetical by ticker.
    day1 = [t for ts, t in order if ts == events[0].bar.timestamp]
    assert day1 == ["NVDA", "SPY"]


def test_naive_timestamps_become_utc_and_prices_are_decimal(tmp_path):
    path = _write(tmp_path / "nvda.csv", SINGLE_SYMBOL_CSV)
    feed = HistoricalDataFeed([NVDA], path)
    feed.connect()
    bar = next(feed.stream()).bar
    assert bar.timestamp.tzinfo is not None
    assert bar.timestamp.utcoffset() == timezone.utc.utcoffset(None)
    assert isinstance(bar.open, Decimal)
    assert bar.open == Decimal("10.0")


def test_zulu_and_aware_timestamps_normalized_to_utc(tmp_path):
    csv = """
timestamp,open,high,low,close,volume
2024-01-01T00:00:00Z,10,11,9,10,100
2024-01-01T05:00:00+05:00,11,12,10,11,100
"""
    path = _write(tmp_path / "tz.csv", csv)
    feed = HistoricalDataFeed([NVDA], path)
    feed.connect()
    times = [e.bar.timestamp for e in feed.stream()]
    # Both resolve to the same UTC instant (00:00Z and 05:00+05:00).
    assert times[0] == times[1]
    assert all(t.tzinfo == timezone.utc for t in times)


def test_invalid_bar_is_skipped_by_default(tmp_path):
    csv = """
timestamp,open,high,low,close,volume
2024-01-01,10,11,9,10,100
2024-01-02,10,9,11,10,100
2024-01-03,12,13,11,12,100
"""  # row 2 has high < low → invalid
    path = _write(tmp_path / "bad.csv", csv)
    feed = HistoricalDataFeed([NVDA], path)
    feed.connect()
    events = list(feed.stream())
    assert len(events) == 2
    assert feed.skipped_rows == 1


def test_invalid_bar_raises_when_skip_disabled(tmp_path):
    csv = """
timestamp,open,high,low,close,volume
2024-01-01,10,9,11,10,100
"""  # high < low
    path = _write(tmp_path / "bad.csv", csv)
    feed = HistoricalDataFeed([NVDA], path, skip_invalid=False)
    with pytest.raises(ValueError):
        feed.connect()


def test_unsubscribed_symbols_are_skipped(tmp_path):
    path = _write(tmp_path / "multi.csv", MULTI_SYMBOL_CSV)
    feed = HistoricalDataFeed([NVDA], path)   # only subscribed to NVDA
    feed.connect()
    tickers = {e.bar.symbol.ticker for e in feed.stream()}
    assert tickers == {"NVDA"}


def test_get_latest_bar_tracks_stream_progress(tmp_path):
    path = _write(tmp_path / "nvda.csv", SINGLE_SYMBOL_CSV)
    feed = HistoricalDataFeed([NVDA], path)
    feed.connect()
    assert feed.get_latest_bar(NVDA) is None   # nothing streamed yet
    gen = feed.stream()
    first = next(gen)
    assert feed.get_latest_bar(NVDA) == first.bar
    last = None
    for ev in gen:
        last = ev
    assert feed.get_latest_bar(NVDA) == last.bar


def test_stream_before_connect_raises(tmp_path):
    path = _write(tmp_path / "nvda.csv", SINGLE_SYMBOL_CSV)
    feed = HistoricalDataFeed([NVDA], path)
    with pytest.raises(RuntimeError):
        list(feed.stream())


def test_missing_file_raises_connection_error(tmp_path):
    feed = HistoricalDataFeed([NVDA], tmp_path / "does_not_exist.csv")
    with pytest.raises(ConnectionError):
        feed.connect()


def test_no_symbol_column_with_multiple_symbols_skips_all(tmp_path):
    # Ambiguous: file has no symbol column but two symbols are configured.
    path = _write(tmp_path / "nvda.csv", SINGLE_SYMBOL_CSV)
    feed = HistoricalDataFeed([NVDA, SPY], path)
    feed.connect()
    # Every row is unresolvable → skipped, not crashed.
    assert list(feed.stream()) == []
    assert feed.skipped_rows == 3


def test_context_manager_lifecycle(tmp_path):
    path = _write(tmp_path / "nvda.csv", SINGLE_SYMBOL_CSV)
    with HistoricalDataFeed([NVDA], path) as feed:
        assert feed.is_connected
        assert len(list(feed.stream())) == 3
    assert not feed.is_connected   # __exit__ called disconnect()


def test_deterministic_across_runs(tmp_path):
    path = _write(tmp_path / "multi.csv", MULTI_SYMBOL_CSV)
    f1 = HistoricalDataFeed([NVDA, SPY], path)
    f2 = HistoricalDataFeed([NVDA, SPY], path)
    f1.connect()
    f2.connect()
    seq1 = [(e.bar.timestamp, e.bar.symbol.ticker, e.bar.close) for e in f1.stream()]
    seq2 = [(e.bar.timestamp, e.bar.symbol.ticker, e.bar.close) for e in f2.stream()]
    assert seq1 == seq2


def test_case_insensitive_and_aliased_headers(tmp_path):
    csv = """
Date,O,H,L,C,Vol
2024-01-01,10,11,9,10.5,100
"""
    path = _write(tmp_path / "alias.csv", csv)
    feed = HistoricalDataFeed([NVDA], path)
    feed.connect()
    bar = next(feed.stream()).bar
    assert bar.close == Decimal("10.5")
    assert bar.volume == Decimal("100")
