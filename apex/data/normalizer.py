"""
apex.data.normalizer
=====================
Raw source data → normalized, validated ``Bar`` / ``Tick`` models.

Every data feed (CSV replay, Alpaca REST/websocket, any future vendor) speaks a
different dialect: timestamps as ISO strings, ``datetime`` objects, or epoch
seconds/millis; prices as floats, strings, or ``Decimal``; OHLCV columns under a
dozen different header spellings; SDK objects with attributes instead of dict
keys. The rest of Apex must never see any of that — it sees only frozen ``Bar``
and ``Tick`` models with UTC-aware timestamps and ``Decimal`` prices.

This module is that single translation boundary. It is pure (no I/O, no network,
no clock) and deterministic, so it is fully unit-testable offline — which is why
the Alpaca feed delegates all of its row→model conversion here rather than
re-implementing it against a live connection.

Two correctness guarantees every normalized value upholds:
  - **All timestamps are timezone-aware UTC.** Naive → assumed UTC; aware →
    converted to UTC; epoch ints/floats → UTC. A naive timestamp never reaches
    a model (``Bar``/``Tick`` would reject it anyway — we fail with a clearer
    message first).
  - **All money is ``Decimal``, parsed via ``str()`` first** so a binary-float
    artifact (e.g. ``0.1 + 0.2``) can never smuggle itself into P&L math.

Bad input fails loud here (``ValueError``) so the *caller* decides whether to
skip-and-count or abort — matching how ``HistoricalDataFeed`` already treats a
bad row. The normalizer never silently invents a value.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional, Sequence

from apex.core.models import Bar, Symbol, Tick

# Canonical header aliases (matched case-insensitively, stripped). Kept in sync
# with HistoricalDataFeed's accepted spellings so a CSV that works there works here.
TIMESTAMP_KEYS: tuple[str, ...] = ("timestamp", "date", "datetime", "time", "t")
SYMBOL_KEYS: tuple[str, ...] = ("symbol", "ticker", "s")
OHLCV_KEYS: dict[str, tuple[str, ...]] = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c", "adj_close", "adjclose"),
    "volume": ("volume", "vol", "v"),
}

# Epoch timestamps below this magnitude are seconds; at or above, milliseconds.
# 1e11 seconds ≈ year 5138, and 1e11 ms ≈ year 1973 — a clean, unambiguous split
# for any realistic market timestamp.
_EPOCH_MS_THRESHOLD = 1e11


# --------------------------------------------------------------------- scalars

def to_utc(value: object) -> datetime:
    """
    Coerce a timestamp of unknown shape into a timezone-aware UTC ``datetime``.

    Accepts:
      - ``datetime`` (naive → assumed UTC; aware → converted to UTC)
      - ISO-8601 string, including a trailing ``Z`` (Zulu) suffix
      - epoch ``int``/``float`` (seconds if < 1e11, else milliseconds)

    Raises ``ValueError`` on anything unparseable or empty.
    """
    if value is None or value == "":
        raise ValueError("timestamp is missing/empty")

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, bool):
        # bool is an int subclass; a boolean is never a valid timestamp.
        raise ValueError(f"invalid timestamp (bool): {value!r}")
    elif isinstance(value, (int, float)):
        seconds = float(value)
        if abs(seconds) >= _EPOCH_MS_THRESHOLD:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("timestamp is missing/empty")
        if text[-1] in ("Z", "z"):
            text = text[:-1] + "+00:00"   # 3.11-safe Zulu handling
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"unparseable timestamp {value!r}: {exc}") from exc

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)   # naive → assume UTC
    return dt.astimezone(timezone.utc)            # aware → convert to UTC


def to_decimal(value: object, *, field: str = "value") -> Decimal:
    """
    Coerce a numeric of unknown shape into ``Decimal``, via ``str()`` first so
    float artifacts never enter money math. Raises ``ValueError`` on None/garbage.
    """
    if value is None or value == "":
        raise ValueError(f"{field} is missing/empty")
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} is not a number: {value!r}") from exc


# ------------------------------------------------------------------- builders

def make_bar(
    symbol: Symbol,
    timestamp: object,
    open: object,
    high: object,
    low: object,
    close: object,
    volume: object,
    timeframe: str = "1Day",
) -> Bar:
    """
    Build a validated ``Bar`` from loosely-typed fields. Each field is normalized
    (UTC timestamp, ``Decimal`` prices) and then the frozen ``Bar`` self-validates
    (rejects high<low, negative prices/volume, naive timestamps).
    """
    return Bar(
        symbol=symbol,
        timestamp=to_utc(timestamp),
        open=to_decimal(open, field="open"),
        high=to_decimal(high, field="high"),
        low=to_decimal(low, field="low"),
        close=to_decimal(close, field="close"),
        volume=to_decimal(volume, field="volume"),
        timeframe=timeframe,
    )


def make_tick(
    symbol: Symbol,
    timestamp: object,
    price: object,
    size: object,
    bid: object = None,
    ask: object = None,
) -> Tick:
    """Build a validated ``Tick`` from loosely-typed fields."""
    return Tick(
        symbol=symbol,
        timestamp=to_utc(timestamp),
        price=to_decimal(price, field="price"),
        size=to_decimal(size, field="size"),
        bid=None if bid in (None, "") else to_decimal(bid, field="bid"),
        ask=None if ask in (None, "") else to_decimal(ask, field="ask"),
    )


def _pick(source: Mapping, keys: Sequence[str]) -> Optional[object]:
    """Return the first present, non-empty value among ``keys`` (case-insensitive)."""
    # Pre-lower the mapping once so callers can pass raw vendor headers.
    lowered = {str(k).strip().lower(): v for k, v in source.items()}
    for key in keys:
        if key in lowered and lowered[key] not in (None, ""):
            return lowered[key]
    return None


def bar_from_mapping(
    raw: Mapping[str, object],
    symbol: Symbol,
    timeframe: str = "1Day",
) -> Bar:
    """
    Build a ``Bar`` from a dict-like row using the canonical header aliases.

    The symbol is supplied by the caller (it already knows which instrument it
    subscribed to); any symbol column in ``raw`` is ignored here. Raises
    ``ValueError`` naming the first missing/invalid field.
    """
    ts = _pick(raw, TIMESTAMP_KEYS)
    if ts is None:
        raise ValueError(f"row missing a timestamp column (tried {TIMESTAMP_KEYS})")
    fields: dict[str, object] = {}
    for name, aliases in OHLCV_KEYS.items():
        val = _pick(raw, aliases)
        if val is None:
            raise ValueError(f"row missing '{name}' (tried {aliases})")
        fields[name] = val
    return make_bar(symbol, ts, timeframe=timeframe, **fields)


def bar_from_obj(obj: object, symbol: Symbol, timeframe: str = "1Day") -> Bar:
    """
    Build a ``Bar`` from an object exposing OHLCV *attributes* — e.g. an
    ``alpaca.data.models.Bar``. Looks up ``timestamp`` (falling back to ``t``)
    and ``open/high/low/close/volume`` (with single-letter ``o/h/l/c/v``
    fallbacks). Raises ``ValueError`` if a required attribute is absent.
    """
    def attr(*names: str) -> object:
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is not None:
                    return v
        raise ValueError(f"object {type(obj).__name__} missing attribute {names}")

    return make_bar(
        symbol,
        attr("timestamp", "t"),
        open=attr("open", "o"),
        high=attr("high", "h"),
        low=attr("low", "l"),
        close=attr("close", "c"),
        volume=attr("volume", "v"),
        timeframe=timeframe,
    )
