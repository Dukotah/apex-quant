"""
Tests for apex.data.alpaca_crypto_feed.

The live SDK call is isolated behind an injectable ``bar_fetcher``, so every
piece of feed logic is exercised offline: connect/disconnect lifecycle,
normalization (Decimal OHLCV, UTC timestamps), bad-bar skipping, chronological
interleaving, gap detection (tight 2× crypto tolerance vs 4× equity), lookback
trimming, retry/backoff, stream ordering, and 24/7 weekend inclusion.
No alpaca-py, no network, no real keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from apex.core.events import MarketEvent
from apex.core.models import AssetClass, Symbol
from apex.data.alpaca_crypto_feed import AlpacaCryptoDataFeed

BTC = Symbol("BTC/USD", AssetClass.CRYPTO, fractionable=True)
ETH = Symbol("ETH/USD", AssetClass.CRYPTO, fractionable=True)
UTC = timezone.utc


def _bar(ts: str, o, h, lo, c, v):
    """Raw attribute-style bar (mimics alpaca.data.models.Bar)."""

    class _B:
        pass

    b = _B()
    b.timestamp = datetime.fromisoformat(ts).replace(tzinfo=UTC)
    b.open, b.high, b.low, b.close, b.volume = o, h, lo, c, v
    return b


def _feed(fetcher, **kw):
    """Helper: create a connected feed with an injected fetcher."""
    feed = AlpacaCryptoDataFeed([BTC, ETH], bar_fetcher=fetcher, sleep=lambda _s: None, **kw)
    feed.connect()
    return feed


# ----------------------------------------------------------------- lifecycle


def test_connect_without_keys_raises(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    feed = AlpacaCryptoDataFeed([BTC])  # no injected fetcher, no keys
    with pytest.raises(ConnectionError):
        feed.connect()


def test_injected_fetcher_connects_offline():
    feed = _feed(lambda t, s, e, tf: {})
    assert feed.is_connected


def test_disconnect_clears_state():
    feed = _feed(lambda *a: {})
    feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
    feed.disconnect()
    assert not feed.is_connected
    assert feed._bars == []
    assert feed._latest == {}


def test_fetch_before_connect_raises():
    feed = AlpacaCryptoDataFeed([BTC], bar_fetcher=lambda *a: {})
    with pytest.raises(RuntimeError):
        feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))


def test_stream_before_connect_raises():
    feed = AlpacaCryptoDataFeed([BTC], bar_fetcher=lambda *a: {})
    with pytest.raises(RuntimeError):
        list(feed.stream())


def test_requires_at_least_one_symbol():
    with pytest.raises(ValueError):
        AlpacaCryptoDataFeed([])


# ----------------------------------------------------------------- normalization


def test_fetch_normalizes_decimal_ohlcv():
    """Prices / volume come back as Decimal regardless of input type."""

    def fetcher(tickers, start, end, tf):
        return {"BTC/USD": [_bar("2024-01-02", 42000.5, 43000, 41500, 42800.75, 1500.123)]}

    feed = _feed(fetcher)
    bars = feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))

    assert len(bars) == 1
    b = bars[0]
    assert isinstance(b.open, Decimal)
    assert isinstance(b.high, Decimal)
    assert isinstance(b.low, Decimal)
    assert isinstance(b.close, Decimal)
    assert isinstance(b.volume, Decimal)
    assert b.close == Decimal("42800.75")
    assert b.symbol.ticker == "BTC/USD"


def test_fetch_normalizes_dict_shaped_bars():
    """Dict-keyed bars (mapping format) are also accepted."""

    def fetcher(tickers, start, end, tf):
        return {
            "BTC/USD": [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "open": 40000,
                    "high": 41000,
                    "low": 39500,
                    "close": 40500,
                    "volume": 1200,
                }
            ]
        }

    feed = _feed(fetcher)
    bars = feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
    assert len(bars) == 1
    assert bars[0].close == Decimal("40500")
    assert bars[0].open == Decimal("40000")


def test_timestamp_is_utc_aware():
    """Normalised bars always carry a UTC-aware timestamp."""

    def fetcher(tickers, start, end, tf):
        return {"ETH/USD": [_bar("2024-03-15", 3000, 3100, 2950, 3050, 500)]}

    feed = AlpacaCryptoDataFeed([ETH], bar_fetcher=fetcher, sleep=lambda _s: None)
    feed.connect()
    bars = feed.fetch_bars(datetime(2024, 3, 15, tzinfo=UTC), datetime(2024, 3, 16, tzinfo=UTC))
    assert bars[0].timestamp.tzinfo is not None
    assert bars[0].timestamp.utcoffset().total_seconds() == 0


def test_fetch_interleaves_chronologically():
    """Multi-symbol bars are merged oldest → newest; ties broken by ticker."""

    def fetcher(tickers, start, end, tf):
        return {
            "BTC/USD": [
                _bar("2024-01-02", 42000, 43000, 41500, 42500, 1000),
                _bar("2024-01-01", 40000, 41000, 39500, 40500, 900),
            ],
            "ETH/USD": [
                _bar("2024-01-01", 2000, 2100, 1950, 2050, 300),
                _bar("2024-01-02", 2100, 2200, 2050, 2150, 320),
            ],
        }

    feed = _feed(fetcher)
    bars = feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))

    order = [(b.timestamp, b.symbol.ticker) for b in bars]
    assert order == sorted(order)  # strictly chronological

    # Day-1 pair: BTC sorts before ETH alphabetically
    day1 = [t for ts, t in order if ts == bars[0].timestamp]
    assert day1 == ["BTC/USD", "ETH/USD"]


def test_unsubscribed_ticker_ignored():
    """Bars for tickers not in the subscription list are silently dropped."""

    def fetcher(tickers, start, end, tf):
        return {"DOGE/USD": [_bar("2024-01-01", 0.1, 0.11, 0.09, 0.10, 50000)]}

    feed = _feed(fetcher)
    bars = feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
    assert bars == []


def test_bad_bar_skipped_and_counted():
    """A malformed bar (high < low) is skipped and increments skipped_rows."""

    def fetcher(tickers, start, end, tf):
        return {
            "BTC/USD": [
                _bar("2024-01-01", 40000, 41000, 39500, 40500, 900),
                _bar("2024-01-02", 42000, 41000, 43000, 42000, 800),  # high < low → invalid
                _bar("2024-01-03", 43000, 44000, 42500, 43500, 950),
            ]
        }

    feed = _feed(fetcher)
    bars = feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    assert len(bars) == 2
    assert feed.skipped_rows == 1


def test_bad_bar_raises_when_skip_disabled():
    """With skip_invalid=False a bad bar raises ValueError immediately."""

    def fetcher(tickers, start, end, tf):
        return {"BTC/USD": [_bar("2024-01-02", 42000, 41000, 43000, 42000, 800)]}

    feed = _feed(fetcher, skip_invalid=False)
    with pytest.raises(ValueError):
        feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))


# -------------------------------------------- 24/7 weekend / continuous coverage


def test_weekend_bars_included():
    """
    Crypto trades on weekends.  A Saturday bar (2024-01-06) and a Sunday bar
    (2024-01-07) must appear in the stream just like any weekday bar.
    """

    def fetcher(tickers, start, end, tf):
        return {
            "BTC/USD": [
                _bar("2024-01-05", 44000, 45000, 43500, 44500, 1100),  # Friday
                _bar("2024-01-06", 44500, 46000, 44000, 45500, 1200),  # Saturday
                _bar("2024-01-07", 45500, 47000, 45000, 46500, 1300),  # Sunday
                _bar("2024-01-08", 46500, 47500, 46000, 47000, 1050),  # Monday
            ]
        }

    feed = AlpacaCryptoDataFeed([BTC], bar_fetcher=fetcher, sleep=lambda _s: None)
    feed.connect()
    bars = feed.fetch_bars(datetime(2024, 1, 5, tzinfo=UTC), datetime(2024, 1, 9, tzinfo=UTC))

    assert len(bars) == 4
    days = [b.timestamp.strftime("%A") for b in bars]
    assert days == ["Friday", "Saturday", "Sunday", "Monday"]


def test_no_market_hours_gating_weekend_not_a_gap():
    """
    A Saturday → Sunday consecutive-day sequence must NOT trigger a gap warning.
    The 2× daily spacing tolerance is 2 days, and Saturday → Sunday is 1 day apart.
    """

    def fetcher(tickers, start, end, tf):
        return {
            "BTC/USD": [
                _bar("2024-01-06", 44500, 46000, 44000, 45500, 1200),  # Saturday
                _bar("2024-01-07", 45500, 47000, 45000, 46500, 1300),  # Sunday
            ]
        }

    feed = AlpacaCryptoDataFeed([BTC], bar_fetcher=fetcher, sleep=lambda _s: None)
    feed.connect()
    feed.fetch_bars(datetime(2024, 1, 6, tzinfo=UTC), datetime(2024, 1, 8, tzinfo=UTC))
    assert feed.gaps_detected == 0


# ---------------------------------------------------------------- gap detection


def test_gap_detection_counts_large_daily_gap():
    """A multi-day gap that exceeds the 2× tolerance is flagged."""

    def fetcher(tickers, start, end, tf):
        return {
            "BTC/USD": [
                _bar("2024-01-01", 40000, 41000, 39500, 40500, 900),
                _bar("2024-01-10", 42000, 43000, 41500, 42500, 950),  # 9-day gap > 2-day tol
            ]
        }

    feed = _feed(fetcher)
    feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 11, tzinfo=UTC))
    assert feed.gaps_detected == 1


def test_normal_consecutive_day_not_a_gap():
    """Adjacent daily bars (1-day apart) are within the 2× tolerance — no gap."""

    def fetcher(tickers, start, end, tf):
        return {
            "BTC/USD": [
                _bar("2024-01-01", 40000, 41000, 39500, 40500, 900),
                _bar("2024-01-02", 40500, 41500, 40000, 41000, 920),
            ]
        }

    feed = _feed(fetcher)
    feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))
    assert feed.gaps_detected == 0


# -------------------------------------------------------------------- stream


def test_stream_yields_market_events():
    """stream() yields MarketEvent instances wrapping Bar objects."""

    def fetcher(tickers, start, end, tf):
        return {
            "BTC/USD": [
                _bar("2024-01-01", 40000, 41000, 39500, 40500, 900),
                _bar("2024-01-02", 40500, 41500, 40000, 41000, 920),
            ]
        }

    feed = AlpacaCryptoDataFeed([BTC], bar_fetcher=fetcher, sleep=lambda _s: None)
    feed.connect()
    feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))

    events = list(feed.stream())
    assert all(isinstance(e, MarketEvent) for e in events)
    assert [e.bar.close for e in events] == [Decimal("40500"), Decimal("41000")]


def test_stream_updates_latest_bar():
    """get_latest_bar returns None before streaming, then the most recent bar after."""

    def fetcher(tickers, start, end, tf):
        return {
            "BTC/USD": [
                _bar("2024-01-01", 40000, 41000, 39500, 40500, 900),
                _bar("2024-01-02", 40500, 41500, 40000, 41000, 920),
            ]
        }

    feed = AlpacaCryptoDataFeed([BTC], bar_fetcher=fetcher, sleep=lambda _s: None)
    feed.connect()
    feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))

    assert feed.get_latest_bar(BTC) is None
    list(feed.stream())
    assert feed.get_latest_bar(BTC).close == Decimal("41000")


def test_stream_replays_oldest_to_newest():
    """Bars come out of stream() in ascending timestamp order."""

    def fetcher(tickers, start, end, tf):
        # Deliberately return in reverse chronological order to confirm sorting.
        return {
            "ETH/USD": [
                _bar("2024-02-03", 2300, 2400, 2250, 2350, 400),
                _bar("2024-02-01", 2100, 2200, 2050, 2150, 380),
                _bar("2024-02-02", 2150, 2300, 2100, 2250, 390),
            ]
        }

    feed = AlpacaCryptoDataFeed([ETH], bar_fetcher=fetcher, sleep=lambda _s: None)
    feed.connect()
    feed.fetch_bars(datetime(2024, 2, 1, tzinfo=UTC), datetime(2024, 2, 4, tzinfo=UTC))

    events = list(feed.stream())
    timestamps = [e.bar.timestamp for e in events]
    assert timestamps == sorted(timestamps)


# --------------------------------------------------------------- retry / backoff


def test_retry_then_success_invokes_backoff():
    calls = {"n": 0}
    sleeps = []

    def flaky(tickers, start, end, tf):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("transient")
        return {"BTC/USD": [_bar("2024-01-01", 40000, 41000, 39500, 40500, 900)]}

    feed = AlpacaCryptoDataFeed(
        [BTC], bar_fetcher=flaky, max_retries=3, backoff_base=1.0, sleep=sleeps.append
    )
    feed.connect()
    bars = feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
    assert len(bars) == 1
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]  # exponential backoff on the two failures


def test_retry_exhausted_raises_connection_error():
    def always_fails(tickers, start, end, tf):
        raise TimeoutError("down")

    feed = AlpacaCryptoDataFeed(
        [BTC], bar_fetcher=always_fails, max_retries=2, backoff_base=0.5, sleep=lambda _s: None
    )
    feed.connect()
    with pytest.raises(ConnectionError):
        feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))


# --------------------------------------------------------- fetch window validation


def test_naive_start_raises():
    feed = _feed(lambda *a: {})
    with pytest.raises(ValueError):
        feed.fetch_bars(datetime(2024, 1, 1), datetime(2024, 1, 2, tzinfo=UTC))


def test_naive_end_raises():
    feed = _feed(lambda *a: {})
    with pytest.raises(ValueError):
        feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2))


def test_end_before_start_raises():
    feed = _feed(lambda *a: {})
    with pytest.raises(ValueError):
        feed.fetch_bars(
            datetime(2024, 1, 5, tzinfo=UTC),
            datetime(2024, 1, 1, tzinfo=UTC),
        )


# --------------------------------------------------------------- get_latest_bars


def test_get_latest_bars_trims_to_lookback_per_ticker():
    """get_latest_bars keeps only the most recent ``lookback`` bars per ticker."""

    def fetcher(tickers, start, end, tf):
        return {
            "BTC/USD": [
                _bar(f"2024-01-{d:02d}", 40000, 41000, 39500, 40500, 900) for d in range(1, 11)
            ]
        }  # 10 bars

    feed = AlpacaCryptoDataFeed([BTC], bar_fetcher=fetcher, sleep=lambda _s: None)
    feed.connect()
    bars = feed.get_latest_bars(lookback=3, end=datetime(2024, 1, 31, tzinfo=UTC))
    assert len(bars) == 3
    assert [b.timestamp.day for b in bars] == [8, 9, 10]


def test_get_latest_bars_zero_lookback_raises():
    feed = AlpacaCryptoDataFeed([BTC], bar_fetcher=lambda *a: {}, sleep=lambda _s: None)
    feed.connect()
    with pytest.raises(ValueError):
        feed.get_latest_bars(lookback=0, end=datetime(2024, 1, 31, tzinfo=UTC))


def test_get_latest_bars_multi_ticker_independent_trim():
    """Each ticker is trimmed independently; mixed-depth histories work correctly."""

    def fetcher(tickers, start, end, tf):
        return {
            "BTC/USD": [
                _bar(f"2024-01-{d:02d}", 40000, 41000, 39500, 40500, 900) for d in range(1, 6)
            ],  # 5 bars
            "ETH/USD": [
                _bar(f"2024-01-{d:02d}", 2000, 2100, 1950, 2050, 300) for d in range(1, 11)
            ],  # 10 bars
        }

    feed = AlpacaCryptoDataFeed([BTC, ETH], bar_fetcher=fetcher, sleep=lambda _s: None)
    feed.connect()
    bars = feed.get_latest_bars(lookback=3, end=datetime(2024, 1, 31, tzinfo=UTC))

    btc_bars = [b for b in bars if b.symbol.ticker == "BTC/USD"]
    eth_bars = [b for b in bars if b.symbol.ticker == "ETH/USD"]

    # BTC only had 5 total; we asked for 3, so 3 returned
    assert len(btc_bars) == 3
    assert [b.timestamp.day for b in btc_bars] == [3, 4, 5]

    # ETH had 10; last 3
    assert len(eth_bars) == 3
    assert [b.timestamp.day for b in eth_bars] == [8, 9, 10]


def test_skipped_rows_reset_between_fetches():
    """skipped_rows is reset to 0 at the start of each fetch_bars call."""

    call = {"n": 0}

    def fetcher(tickers, start, end, tf):
        call["n"] += 1
        if call["n"] == 1:
            return {"BTC/USD": [_bar("2024-01-02", 42000, 41000, 43000, 42000, 800)]}  # bad
        return {"BTC/USD": [_bar("2024-01-03", 40000, 41000, 39500, 40500, 900)]}  # good

    feed = AlpacaCryptoDataFeed([BTC], bar_fetcher=fetcher, sleep=lambda _s: None)
    feed.connect()

    feed.fetch_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))
    assert feed.skipped_rows == 1

    feed.fetch_bars(datetime(2024, 1, 3, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    assert feed.skipped_rows == 0  # reset, not accumulated
