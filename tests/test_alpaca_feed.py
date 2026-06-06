"""
Tests for apex.data.alpaca_feed.

The live SDK call is isolated behind an injectable ``bar_fetcher``, so every
piece of feed logic is exercised offline: retry/backoff, normalization, bad-bar
skipping, chronological sort + interleave, gap detection, lookback trimming, and
the connect/stream lifecycle. No alpaca-py, no network, no real keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from apex.core.events import MarketEvent
from apex.core.models import AssetClass, Symbol
from apex.data.alpaca_feed import AlpacaDataFeed

NVDA = Symbol("NVDA", AssetClass.EQUITY)
SPY = Symbol("SPY", AssetClass.ETF)
UTC = timezone.utc


def _bar(ts: str, o, h, lo, c, v):
    """A raw attribute-style bar (mimics alpaca.data.models.Bar)."""

    class _B:
        pass

    b = _B()
    b.timestamp = datetime.fromisoformat(ts).replace(tzinfo=UTC)
    b.open, b.high, b.low, b.close, b.volume = o, h, lo, c, v
    return b


def _feed(fetcher, **kw):
    feed = AlpacaDataFeed([NVDA, SPY], bar_fetcher=fetcher, sleep=lambda _s: None, **kw)
    feed.connect()
    return feed


# ----------------------------------------------------------------- lifecycle


def test_connect_without_keys_raises(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    feed = AlpacaDataFeed([NVDA])  # no injected fetcher, no keys
    with pytest.raises(ConnectionError):
        feed.connect()


def test_injected_fetcher_connects_offline():
    feed = _feed(lambda t, s, e, tf: {})
    assert feed.is_connected


def test_fetch_before_connect_raises():
    feed = AlpacaDataFeed([NVDA], bar_fetcher=lambda *a: {})
    with pytest.raises(RuntimeError):
        feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))


# ----------------------------------------------------------------- normalization


def test_fetch_normalizes_and_interleaves_by_timestamp():
    def fetcher(tickers, start, end, tf):
        return {
            "NVDA": [
                _bar("2024-01-02", 10.5, 12, 10, 11.8, 110),
                _bar("2024-01-01", 10, 11, 9, 10.5, 100),
            ],
            "SPY": [
                _bar("2024-01-01", 400, 401, 399, 400.5, 200),
                _bar("2024-01-02", 401, 402, 400, 401.5, 210),
            ],
        }

    feed = _feed(fetcher)
    bars = feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))

    order = [(b.timestamp, b.symbol.ticker) for b in bars]
    assert order == sorted(order)  # chronological, ticker tie-break
    assert all(isinstance(b.close, Decimal) for b in bars)
    day1 = [t for ts, t in order if ts == bars[0].timestamp]
    assert day1 == ["NVDA", "SPY"]  # interleaved, alphabetical


def test_unsubscribed_ticker_ignored():
    def fetcher(tickers, start, end, tf):
        return {"TSLA": [_bar("2024-01-01", 10, 11, 9, 10, 100)]}  # not subscribed

    feed = _feed(fetcher)
    bars = feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
    assert bars == []


def test_bad_bar_skipped_and_counted():
    def fetcher(tickers, start, end, tf):
        return {
            "NVDA": [
                _bar("2024-01-01", 10, 11, 9, 10, 100),
                _bar("2024-01-02", 10, 9, 11, 10, 100),  # high < low → invalid
                _bar("2024-01-03", 12, 13, 11, 12, 100),
            ]
        }

    feed = _feed(fetcher)
    bars = feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    assert len(bars) == 2
    assert feed.skipped_rows == 1


def test_bad_bar_raises_when_skip_disabled():
    def fetcher(tickers, start, end, tf):
        return {"NVDA": [_bar("2024-01-02", 10, 9, 11, 10, 100)]}  # high < low

    feed = _feed(fetcher, skip_invalid=False)
    with pytest.raises(ValueError):
        feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))


def test_dict_shaped_bars_also_normalized():
    def fetcher(tickers, start, end, tf):
        return {
            "NVDA": [
                {
                    "timestamp": "2024-01-01",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "volume": 100,
                }
            ]
        }

    feed = _feed(fetcher)
    bars = feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
    assert bars[0].close == Decimal("10.5")


# ------------------------------------------------------------------- retry


def test_retry_then_success_invokes_backoff():
    calls = {"n": 0}
    sleeps = []

    def flaky(tickers, start, end, tf):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("transient")
        return {"NVDA": [_bar("2024-01-01", 10, 11, 9, 10, 100)]}

    feed = AlpacaDataFeed(
        [NVDA], bar_fetcher=flaky, max_retries=3, backoff_base=1.0, sleep=sleeps.append
    )
    feed.connect()
    bars = feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
    assert len(bars) == 1
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]  # exponential backoff on the two failures


def test_retry_exhausted_raises_connection_error():
    def always_fails(tickers, start, end, tf):
        raise TimeoutError("down")

    feed = AlpacaDataFeed(
        [NVDA], bar_fetcher=always_fails, max_retries=2, backoff_base=0.5, sleep=lambda _s: None
    )
    feed.connect()
    with pytest.raises(ConnectionError):
        feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))


# -------------------------------------------------------------------- stream


def test_stream_replays_chronologically_and_tracks_latest():
    def fetcher(tickers, start, end, tf):
        return {
            "NVDA": [
                _bar("2024-01-01", 10, 11, 9, 10.2, 100),
                _bar("2024-01-02", 11, 12, 10, 11.5, 110),
            ]
        }

    feed = _feed(fetcher)
    feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))

    assert feed.get_latest_bar(NVDA) is None
    events = list(feed.stream())
    assert all(isinstance(e, MarketEvent) for e in events)
    assert [e.bar.close for e in events] == [Decimal("10.2"), Decimal("11.5")]
    assert feed.get_latest_bar(NVDA).close == Decimal("11.5")


def test_validations_on_fetch_window():
    feed = _feed(lambda *a: {})
    aware = datetime(2024, 1, 2, tzinfo=UTC)
    naive = datetime(2024, 1, 1)
    with pytest.raises(ValueError):
        feed.fetch_bars(naive, aware)  # naive start
    with pytest.raises(ValueError):
        feed.fetch_bars(aware, datetime(2024, 1, 1, tzinfo=UTC))  # end < start


# ---------------------------------------------------------------- gap detection


def test_gap_detection_counts_large_daily_gap():
    def fetcher(tickers, start, end, tf):
        return {
            "NVDA": [
                _bar("2024-01-01", 10, 11, 9, 10, 100),
                _bar("2024-01-15", 10, 11, 9, 10, 100),  # 14-day hole > 4-day tolerance
            ]
        }

    feed = _feed(fetcher)
    feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 16, tzinfo=UTC))
    assert feed.gaps_detected == 1


def test_normal_weekend_gap_not_flagged():
    def fetcher(tickers, start, end, tf):
        return {
            "NVDA": [
                _bar("2024-01-05", 10, 11, 9, 10, 100),  # Friday
                _bar("2024-01-08", 10, 11, 9, 10, 100),  # Monday (3-day weekend gap)
            ]
        }

    feed = _feed(fetcher)
    feed.fetch_bars(datetime(2024, 1, 5, tzinfo=UTC), datetime(2024, 1, 9, tzinfo=UTC))
    assert feed.gaps_detected == 0


# --------------------------------------------------------------- get_latest_bars


def test_get_latest_bars_trims_to_lookback_per_ticker():
    def fetcher(tickers, start, end, tf):
        return {
            "NVDA": [_bar(f"2024-01-{d:02d}", 10, 11, 9, 10, 100) for d in range(1, 11)]
        }  # 10 bars (valid OHLC: close within [low, high]; the test asserts on dates)

    feed = AlpacaDataFeed([NVDA], bar_fetcher=fetcher, sleep=lambda _s: None)
    feed.connect()
    bars = feed.get_latest_bars(lookback=3, end=datetime(2024, 1, 31, tzinfo=UTC))
    assert len(bars) == 3
    # Kept the most recent three (days 8, 9, 10).
    assert [b.timestamp.day for b in bars] == [8, 9, 10]
