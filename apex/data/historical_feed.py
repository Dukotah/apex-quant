"""
apex.data.historical_feed
=========================
HistoricalDataFeed: replays stored OHLCV bars from a file for backtesting.

This is the engine that powers ALL backtesting. It reads a CSV (stdlib path)
or Parquet (lazy pandas) file of OHLCV data, normalizes every row into a
validated, frozen ``Bar`` (UTC timestamp, Decimal prices), and yields
``MarketEvent``s in **strict chronological order**, then stops — that
StopIteration is what ends a backtest.

Multiple symbols in one file are interleaved by timestamp into a single merged
stream, exactly as a live feed would deliver them. Ordering is deterministic:
the same file always produces the same event sequence (stable sort on
``(timestamp, ticker, source-row-index)``), which is what makes backtest results
reproducible and backtest/live parity meaningful.

I/O note: a data feed is one of the few places I/O is allowed — it lives at the
edge of the system, translating a source format into normalized events. The
"no I/O" rule applies to *strategy* logic, which only ever sees MarketEvents.

Design choice — load-then-sort: the whole file is read and sorted in
``connect()``. Daily-bar backtests are tiny (a few thousand rows even for 20
years across several symbols), so the simplicity and the guarantee of correct
chronological order — even when the source file is unsorted — are worth more
than streaming a file we could never realistically be too large to hold.
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

from apex.core.events import MarketEvent
from apex.core.models import Bar, Symbol
from apex.data.base_feed import BaseDataFeed

logger = logging.getLogger(__name__)

# Accepted header aliases (matched case-insensitively after stripping).
_TIMESTAMP_KEYS = ("timestamp", "date", "datetime", "time")
_SYMBOL_KEYS = ("symbol", "ticker")
_OHLCV_KEYS = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c", "adj_close", "adjclose"),
    "volume": ("volume", "vol", "v"),
}


class HistoricalDataFeed(BaseDataFeed):
    """
    Replays OHLCV bars from a CSV/Parquet file as a chronological MarketEvent stream.

    Lifecycle (inherited contract)::

        feed = HistoricalDataFeed(symbols=[nvda, spy], path="bars.csv")
        feed.connect()                 # reads + validates + sorts the file
        for event in feed.stream():    # yields MarketEvents oldest→newest
            event_bus.put(event)
        feed.disconnect()              # idempotent cleanup

    Or as a context manager::

        with HistoricalDataFeed([nvda], "bars.csv") as feed:
            for event in feed.stream():
                ...

    File schema (CSV headers, case-insensitive):
      - one timestamp column: ``timestamp`` / ``date`` / ``datetime`` / ``time``
      - OHLCV columns: ``open, high, low, close, volume`` (common aliases accepted)
      - optional ``symbol`` / ``ticker`` column for multi-symbol files

    If there is no symbol column, exactly one Symbol must be configured and it is
    applied to every row. Rows whose symbol is not in ``symbols`` are skipped
    (the feed is simply not subscribed to them).
    """

    def __init__(
        self,
        symbols: Sequence[Symbol],
        path: str | Path,
        timeframe: str = "1Day",
        *,
        skip_invalid: bool = True,
    ) -> None:
        super().__init__(list(symbols), timeframe)
        if not self.symbols:
            raise ValueError("HistoricalDataFeed requires at least one Symbol")
        self.path = Path(path)
        self.skip_invalid = skip_invalid

        # ticker -> Symbol, so each parsed row resolves to the configured instrument.
        self._by_ticker: dict[str, Symbol] = {s.ticker: s for s in self.symbols}

        self._bars: List[Bar] = []                 # sorted, validated, ready to stream
        self._latest: dict[str, Bar] = {}          # ticker -> most recent yielded bar
        self.skipped_rows: int = 0                 # count of rows dropped as invalid

    # ----------------------------------------------------------------- lifecycle

    def connect(self) -> None:
        """Read, validate, and chronologically sort the whole file into memory."""
        if not self.path.exists():
            raise ConnectionError(f"Historical data file not found: {self.path}")

        suffix = self.path.suffix.lower()
        try:
            if suffix in (".parquet", ".pq"):
                rows = self._read_parquet()
            elif suffix in (".csv", ".txt", ""):
                rows = self._read_csv()
            else:
                raise ConnectionError(f"Unsupported file type '{suffix}' for {self.path}")
        except ConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001 — turn any read failure into the contract's error
            raise ConnectionError(f"Failed to read {self.path}: {exc}") from exc

        self._bars = self._build_sorted_bars(rows)
        self._latest = {}
        self._connected = True
        logger.info(
            "HistoricalDataFeed connected: %d bars from %s (%d rows skipped)",
            len(self._bars), self.path, self.skipped_rows,
        )

    def disconnect(self) -> None:
        """Release buffered bars. Idempotent — safe to call more than once."""
        self._bars = []
        self._latest = {}
        self._connected = False

    def stream(self) -> Iterator[MarketEvent]:
        """
        Yield one MarketEvent per bar, oldest→newest, then stop.

        The terminating StopIteration is the signal that the backtest is over.
        """
        if not self._connected:
            raise RuntimeError("stream() called before connect() — call connect() first")
        for bar in self._bars:
            self._latest[bar.symbol.ticker] = bar
            yield MarketEvent(bar=bar)

    def get_latest_bar(self, symbol: Symbol) -> Optional[Bar]:
        """Most recent bar yielded for ``symbol`` so far, or None before streaming."""
        return self._latest.get(symbol.ticker)

    # ----------------------------------------------------------------- internals

    def _read_csv(self) -> List[dict]:
        with self.path.open("r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise ConnectionError(f"Empty or headerless CSV: {self.path}")
            # Normalize headers once (lowercased, stripped) for alias matching.
            field_map = {(name or "").strip().lower(): name for name in reader.fieldnames}
            return [self._normalize_keys(row, field_map) for row in reader]

    def _read_parquet(self) -> List[dict]:
        try:
            import pandas as pd  # lazy: keep the CSV path dependency-free
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise ConnectionError(
                "Reading Parquet requires pandas. Install it (`pip install pandas`) "
                "or convert the file to CSV."
            ) from exc
        df = pd.read_parquet(self.path)
        field_map = {str(c).strip().lower(): c for c in df.columns}
        # Stringify every value so Decimal/timestamp parsing is identical to the CSV path.
        records = df.astype(object).where(df.notna(), None).to_dict("records")
        return [self._normalize_keys(rec, field_map) for rec in records]

    @staticmethod
    def _normalize_keys(row: dict, field_map: dict[str, object]) -> dict:
        """Re-key a raw row by canonical lowercase header name."""
        return {canon: row[orig] for canon, orig in field_map.items()}

    def _build_sorted_bars(self, rows: List[dict]) -> List[Bar]:
        """Parse rows into validated Bars and return them in chronological order.

        Stable-sorted on (timestamp, ticker, source index) so equal-timestamp
        bars across symbols interleave deterministically and the same file always
        yields the same sequence.
        """
        parsed: List[tuple[datetime, str, int, Bar]] = []
        for idx, row in enumerate(rows):
            bar = self._row_to_bar(row, idx)
            if bar is not None:
                parsed.append((bar.timestamp, bar.symbol.ticker, idx, bar))
        parsed.sort(key=lambda t: (t[0], t[1], t[2]))
        return [t[3] for t in parsed]

    def _row_to_bar(self, row: dict, idx: int) -> Optional[Bar]:
        """Convert one normalized row into a Bar, or None if it should be skipped."""
        try:
            symbol = self._resolve_symbol(row, idx)
            if symbol is None:
                return None  # row belongs to a symbol we're not subscribed to
            bar = Bar(
                symbol=symbol,
                timestamp=self._parse_timestamp(row, idx),
                open=self._parse_decimal(row, "open", idx),
                high=self._parse_decimal(row, "high", idx),
                low=self._parse_decimal(row, "low", idx),
                close=self._parse_decimal(row, "close", idx),
                volume=self._parse_decimal(row, "volume", idx),
                timeframe=self.timeframe,
            )
            return bar
        except (ValueError, KeyError, InvalidOperation, TypeError) as exc:
            return self._handle_bad_row(idx, exc)

    def _handle_bad_row(self, idx: int, exc: Exception) -> None:
        if not self.skip_invalid:
            raise ValueError(f"Invalid bar at row {idx}: {exc}") from exc
        self.skipped_rows += 1
        logger.warning("Skipping invalid bar at row %d: %s", idx, exc)
        return None

    def _resolve_symbol(self, row: dict, idx: int) -> Optional[Symbol]:
        ticker = None
        for key in _SYMBOL_KEYS:
            if key in row and row[key] not in (None, ""):
                ticker = str(row[key]).strip()
                break
        if ticker is None:
            # No symbol column: only valid when exactly one symbol is configured.
            if len(self.symbols) != 1:
                raise ValueError(
                    "File has no symbol column but multiple symbols are configured; "
                    "cannot disambiguate which symbol the rows belong to"
                )
            return self.symbols[0]
        return self._by_ticker.get(ticker)  # None → unsubscribed symbol, skipped upstream

    @staticmethod
    def _parse_timestamp(row: dict, idx: int) -> datetime:
        raw = None
        for key in _TIMESTAMP_KEYS:
            if key in row and row[key] not in (None, ""):
                raw = row[key]
                break
        if raw is None:
            raise ValueError(f"row {idx} missing a timestamp column {_TIMESTAMP_KEYS}")

        if isinstance(raw, datetime):
            dt = raw
        else:
            text = str(raw).strip()
            if text.endswith("Z") or text.endswith("z"):
                text = text[:-1] + "+00:00"   # 3.11-safe handling of Zulu suffix
            dt = datetime.fromisoformat(text)
        # Naive → assume UTC; aware → convert to UTC. Bars are always UTC.
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _parse_decimal(row: dict, field: str, idx: int) -> Decimal:
        value = None
        for key in _OHLCV_KEYS[field]:
            if key in row and row[key] not in (None, ""):
                value = row[key]
                break
        if value is None:
            raise ValueError(f"row {idx} missing '{field}' (tried {_OHLCV_KEYS[field]})")
        # str() first so floats from Parquet don't smuggle binary-float artifacts in.
        return Decimal(str(value).strip())
