"""
apex.data.alpaca_feed
=====================
AlpacaDataFeed: real market data from Alpaca for paper/live trading.

Apex runs on a **cron model** (GitHub Actions): ``scripts/run_once.py`` wakes up,
fetches the most recent bars, lets strategies react, routes any signal through
risk → execution, persists state, and exits. There is no always-on websocket to
babysit. So this feed's job is to **fetch a finite, chronological window of bars
on demand** and replay it as ``MarketEvent``s — the exact same shape
``HistoricalDataFeed`` produces, so the one ``TradingEngine`` drives both a
backtest and a live evaluation cycle with zero changes.

Design for offline testability (Golden Rule 12 — every module ships with tests):
the only code that touches the network is a single ``bar_fetcher`` callable built
in ``connect()``. Everything else — retry/backoff, UTC/Decimal normalization,
bad-bar skipping, gap detection, chronological sort, the stream — is pure and
fully unit-tested by injecting a fake fetcher. The live SDK path is a thin,
documented wrapper that needs real keys + network and so is verified in paper,
not in CI.

Determinism: ``fetch_bars`` takes explicit ``start``/``end`` (no hidden
``now()`` in logic — the cron caller supplies the time from its injected clock),
and the same fetched window always yields the same event sequence.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Callable, Iterable, Iterator, List, Mapping, Optional, Sequence

from apex.core.events import MarketEvent
from apex.core.models import Bar, Symbol
from apex.data import normalizer as norm
from apex.data.base_feed import BaseDataFeed

logger = logging.getLogger(__name__)

# A fetcher maps requested tickers → an iterable of raw bar objects/dicts for a
# [start, end] window at a timeframe. This is the one seam the real SDK plugs into.
BarFetcher = Callable[[List[str], datetime, datetime, str], Mapping[str, Iterable[object]]]

# Map Apex timeframe strings → the rough calendar spacing of one bar, used only
# for gap detection (warn when consecutive bars skip more than a tolerance).
_TIMEFRAME_SPACING = {
    "1Day": timedelta(days=1),
    "1Hour": timedelta(hours=1),
    "1Min": timedelta(minutes=1),
}


class AlpacaDataFeed(BaseDataFeed):
    """
    On-demand OHLCV from Alpaca, replayed as a chronological MarketEvent stream.

    Typical use (a cron evaluation cycle)::

        feed = AlpacaDataFeed([nvda, spy], timeframe="1Day")
        feed.connect()                              # builds the SDK client
        feed.get_latest_bars(lookback=200, end=clock.now())   # fetch recent window
        for event in feed.stream():                 # oldest → newest
            engine handles event
        feed.disconnect()

    Parameters
    ----------
    symbols:        instruments to subscribe to.
    timeframe:      Apex timeframe string ("1Day", "1Hour", "1Min", ...).
    api_key / api_secret:
                    Alpaca credentials. Default: read from ALPACA_API_KEY /
                    ALPACA_SECRET_KEY env vars (never hardcoded, never committed).
    feed_source:    Alpaca data feed ("iex" free, "sip" paid). Default "iex".
    max_retries / backoff_base:
                    transient-error retry policy for the network fetch.
    skip_invalid:   skip + count malformed bars (True) or raise (False).
    bar_fetcher:    DEPENDENCY INJECTION for tests — a callable replacing the
                    live SDK call. When provided, connect() uses it and never
                    imports alpaca-py.
    sleep:          injectable sleep (tests pass a no-op to keep retries instant).
    """

    def __init__(
        self,
        symbols: Sequence[Symbol],
        timeframe: str = "1Day",
        *,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        feed_source: str = "iex",
        max_retries: int = 3,
        backoff_base: float = 1.0,
        skip_invalid: bool = True,
        bar_fetcher: Optional[BarFetcher] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(list(symbols), timeframe)
        if not self.symbols:
            raise ValueError("AlpacaDataFeed requires at least one Symbol")

        self.api_key = api_key if api_key is not None else os.getenv("ALPACA_API_KEY")
        self.api_secret = api_secret if api_secret is not None else os.getenv("ALPACA_SECRET_KEY")
        self.feed_source = feed_source
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.skip_invalid = skip_invalid
        self._sleep = sleep

        self._by_ticker: dict[str, Symbol] = {s.ticker: s for s in self.symbols}
        self._fetcher: Optional[BarFetcher] = bar_fetcher
        self._injected_fetcher = bar_fetcher is not None

        self._bars: List[Bar] = []  # current fetched window, sorted
        self._latest: dict[str, Bar] = {}  # ticker → most recent yielded bar
        self.skipped_rows: int = 0
        self.gaps_detected: int = 0

    # ----------------------------------------------------------------- lifecycle

    def connect(self) -> None:
        """
        Build the data client. With an injected fetcher this is a pure no-op
        (used in tests); otherwise it requires credentials and the alpaca-py SDK.
        """
        if self._injected_fetcher:
            self._connected = True
            logger.info("AlpacaDataFeed connected (injected fetcher — offline/test mode).")
            return

        if not self.api_key or not self.api_secret:
            raise ConnectionError(
                "Alpaca credentials missing. Set ALPACA_API_KEY / ALPACA_SECRET_KEY "
                "(free paper keys at alpaca.markets) or inject a bar_fetcher for tests."
            )
        self._fetcher = self._build_sdk_fetcher()
        self._connected = True
        logger.info("AlpacaDataFeed connected (live SDK, feed=%s).", self.feed_source)

    def disconnect(self) -> None:
        """Release buffered bars. Idempotent."""
        self._bars = []
        self._latest = {}
        self._connected = False

    # --------------------------------------------------------------- fetch / stream

    def fetch_bars(
        self,
        start: datetime,
        end: datetime,
        timeframe: Optional[str] = None,
    ) -> List[Bar]:
        """
        Fetch all subscribed symbols over [start, end], normalize + sort them
        chronologically, and load them as this feed's current window.

        Retries transient fetch errors with exponential backoff. Malformed bars
        are skipped + counted (or raise, if skip_invalid=False). Returns the
        sorted bars; ``stream()`` then replays them.
        """
        if not self._connected or self._fetcher is None:
            raise RuntimeError("fetch_bars() called before connect()")
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("fetch_bars start/end must be timezone-aware (UTC)")
        if end < start:
            raise ValueError("fetch_bars end is before start")

        # Reset per-call quality counters so a second fetch in the same session reports
        # this call's counts, not a running total since construction.
        self.skipped_rows = 0
        self.gaps_detected = 0

        tf = timeframe or self.timeframe
        tickers = [s.ticker for s in self.symbols]
        raw_by_ticker = self._fetch_with_retry(tickers, start, end, tf)

        self._bars = self._normalize_and_sort(raw_by_ticker, tf)
        self._latest = {}
        self._detect_gaps(self._bars, tf)
        logger.info(
            "AlpacaDataFeed fetched %d bars for %s over [%s, %s] (%d skipped, %d gaps).",
            len(self._bars),
            tickers,
            start.date(),
            end.date(),
            self.skipped_rows,
            self.gaps_detected,
        )
        return self._bars

    def get_latest_bars(self, lookback: int, end: datetime) -> List[Bar]:
        """
        Fetch roughly the most recent ``lookback`` bars per symbol ending at
        ``end`` (the caller supplies the time from its clock — keeps logic
        deterministic). Over-fetches a calendar window to absorb weekends/
        holidays, then keeps the last ``lookback`` bars per ticker.
        """
        if lookback <= 0:
            raise ValueError("lookback must be positive")
        spacing = _TIMEFRAME_SPACING.get(self.timeframe, timedelta(days=1))
        # Over-fetch generously: ~2x for non-trading days on daily, a buffer otherwise.
        span = spacing * lookback * (2 if spacing >= timedelta(days=1) else 1) + spacing
        self.fetch_bars(end - span, end, self.timeframe)
        self._bars = self._keep_last_per_ticker(self._bars, lookback)
        return self._bars

    def stream(self) -> Iterator[MarketEvent]:
        """Replay the current fetched window oldest → newest, then stop."""
        if not self._connected:
            raise RuntimeError("stream() called before connect()")
        for bar in self._bars:
            self._latest[bar.symbol.ticker] = bar
            yield MarketEvent(bar=bar)

    def get_latest_bar(self, symbol: Symbol) -> Optional[Bar]:
        """Most recent bar yielded for ``symbol`` so far, or None."""
        return self._latest.get(symbol.ticker)

    # ----------------------------------------------------------------- internals

    def _fetch_with_retry(
        self,
        tickers: List[str],
        start: datetime,
        end: datetime,
        tf: str,
    ) -> Mapping[str, Iterable[object]]:
        """Call the fetcher, retrying transient failures with exponential backoff."""
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._fetcher(tickers, start, end, tf)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001 — any network error is retryable
                last_exc = exc
                if attempt == self.max_retries:
                    break
                delay = self.backoff_base * (2**attempt)
                logger.warning(
                    "Alpaca fetch failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                    delay,
                )
                self._sleep(delay)
        raise ConnectionError(
            f"Alpaca fetch failed after {self.max_retries + 1} attempts: {last_exc}"
        ) from last_exc

    def _normalize_and_sort(
        self,
        raw_by_ticker: Mapping[str, Iterable[object]],
        tf: str,
    ) -> List[Bar]:
        """Normalize every raw bar to a validated Bar; sort chronologically."""
        parsed: List[tuple[datetime, str, int, Bar]] = []
        idx = 0
        for ticker, raw_bars in raw_by_ticker.items():
            symbol = self._by_ticker.get(ticker)
            if symbol is None:
                continue  # not subscribed — ignore
            for raw in raw_bars:
                bar = self._to_bar(raw, symbol, tf, idx)
                if bar is not None:
                    parsed.append((bar.timestamp, ticker, idx, bar))
                idx += 1
        parsed.sort(key=lambda t: (t[0], t[1], t[2]))
        return [t[3] for t in parsed]

    def _to_bar(self, raw: object, symbol: Symbol, tf: str, idx: int) -> Optional[Bar]:
        """Convert one raw bar (dict or attribute object) into a Bar, or skip it."""
        try:
            if isinstance(raw, Mapping):
                return norm.bar_from_mapping(raw, symbol, tf)
            return norm.bar_from_obj(raw, symbol, tf)
        except (ValueError, TypeError) as exc:
            if not self.skip_invalid:
                raise ValueError(f"Invalid Alpaca bar #{idx} for {symbol.ticker}: {exc}") from exc
            self.skipped_rows += 1
            logger.warning("Skipping invalid Alpaca bar #%d for %s: %s", idx, symbol.ticker, exc)
            return None

    def _detect_gaps(self, bars: List[Bar], tf: str) -> None:
        """Count (and log) suspicious gaps between consecutive same-ticker bars."""
        spacing = _TIMEFRAME_SPACING.get(tf)
        if spacing is None:
            return
        # Tolerance: daily bars may legitimately skip weekends + a holiday (~4 days).
        tolerance = spacing * 4 if spacing >= timedelta(days=1) else spacing * 2
        last_ts: dict[str, datetime] = {}
        for bar in bars:
            t = bar.symbol.ticker
            prev = last_ts.get(t)
            if prev is not None and (bar.timestamp - prev) > tolerance:
                self.gaps_detected += 1
                logger.warning(
                    "Data gap for %s: %s → %s (> %s)",
                    t,
                    prev,
                    bar.timestamp,
                    tolerance,
                )
            last_ts[t] = bar.timestamp

    @staticmethod
    def _keep_last_per_ticker(bars: List[Bar], lookback: int) -> List[Bar]:
        """Trim a chronological list to the last ``lookback`` bars per ticker."""
        counts: dict[str, int] = {}
        for bar in bars:
            counts[bar.symbol.ticker] = counts.get(bar.symbol.ticker, 0) + 1
        keep_after: dict[str, int] = {t: max(0, n - lookback) for t, n in counts.items()}
        seen: dict[str, int] = {}
        out: List[Bar] = []
        for bar in bars:
            t = bar.symbol.ticker
            seen[t] = seen.get(t, 0) + 1
            if seen[t] > keep_after[t]:
                out.append(bar)
        return out

    def _build_sdk_fetcher(self) -> BarFetcher:
        """
        Build the real alpaca-py fetcher. Imported lazily so the module loads
        (and the test suite runs) without the SDK installed. Verified against
        live paper keys, not in CI — its surface is intentionally tiny.
        """
        try:  # pragma: no cover - requires alpaca-py + network
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        except ImportError as exc:  # pragma: no cover
            raise ConnectionError(
                "alpaca-py is required for live data. `pip install alpaca-py` "
                "or inject a bar_fetcher for offline use."
            ) from exc

        client = StockHistoricalDataClient(self.api_key, self.api_secret)
        unit_map = {  # pragma: no cover
            "1Day": (1, TimeFrameUnit.Day),
            "1Hour": (1, TimeFrameUnit.Hour),
            "1Min": (1, TimeFrameUnit.Minute),
        }

        def fetch(tickers, start, end, tf):  # pragma: no cover - live path
            amount, unit = unit_map.get(tf, (1, TimeFrameUnit.Day))
            request = StockBarsRequest(
                symbol_or_symbols=tickers,
                timeframe=TimeFrame(amount, unit),
                start=start,
                end=end,
                feed=self.feed_source,
            )
            barset = client.get_stock_bars(request)
            # BarSet.data is {ticker: [Bar, ...]} of attribute objects.
            return dict(barset.data)

        return fetch
