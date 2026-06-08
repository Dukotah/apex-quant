"""
scripts/screener_yahoo.py
=========================
A RESEARCH screener — a starting point for a future momentum / "runners" sleeve.
It pulls Yahoo Finance's free predefined screens (day gainers, most actives) plus
a derived gap-up filter, merges them into ONE candidate list, and tags each ticker
with the screen(s) it showed up in. Output is a CSV (or stdout) you eyeball or feed
into later research — it does NOT place orders, size positions, or touch the live
engine in any way. (CLAUDE.md golden rules 2/3: only the RiskManager ever trades;
this is universe research that lives entirely outside the trading path.)

Two layers, deliberately separated for testability (the export_status pattern):

  * PURE CORE — ``parse_quotes`` + ``merge_screens`` and the small math helpers.
    Given raw Yahoo JSON (already fetched) they return plain, deterministic
    dataclasses: no network, no clock, no randomness. These are what the tests
    exercise, so the suite never hits Yahoo and can't go flaky on a rate limit.

  * THIN I/O — ``_default_fetcher`` / ``fetch_screens`` / ``main``. The only places
    that touch the network (lazy ``httpx``) and the filesystem. Fail-soft: a screen
    that errors or returns junk degrades to an empty list rather than crashing the
    run, so one bad endpoint never sinks the whole scrape.

THE SCREENS (Yahoo predefined ``scrIds``):
  - ``day_gainers``  — biggest % movers up today  -> tag ``gainer``
  - ``most_actives`` — highest volume today        -> tag ``active``
  - ``gapper``       — DERIVED, not a Yahoo screen: a ticker earns this tag when its
    open gapped up >= ``gap_pct_min`` from the prior close AND traded at >=
    ``rel_vol_min`` x its average volume (optionally under a ``price_max`` ceiling for
    the classic low-priced runner). Float isn't in the free payload, so relative
    volume + gap + price stand in for the "low-float runner" heuristic.

All percentages are in HUMAN units (5.6 == 5.6%); relative volume is a ratio (2.0 ==
2x average). Money is Decimal at every edge (golden rule 14).

Run (research, manual — never wired into the cron):
    python -m scripts.screener_yahoo --out data/runners.csv
    python -m scripts.screener_yahoo --price-max 20 --gap-min 5 --rel-vol-min 2 --stdout
"""

from __future__ import annotations

import argparse
import csv
import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("apex.screener_yahoo")

# Yahoo's public predefined-screener endpoint. Returns a JSON envelope:
# {"finance": {"result": [{"quotes": [ {quote}, ... ]}]}}.
YAHOO_SCREENER_URL = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"

# A browser-ish UA — the bare endpoint 403s an empty/blank agent.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# The two real Yahoo screens we fetch and their candidate tags. "gapper" is derived
# locally in merge_screens, so it is intentionally NOT in this map.
DEFAULT_SCREENS: Tuple[Tuple[str, str], ...] = (
    ("day_gainers", "gainer"),
    ("most_actives", "active"),
)

# Defaults for the derived gapper tag (overridable on the CLI).
DEFAULT_GAP_PCT_MIN = Decimal("3")  # open gapped up >= 3% from prior close
DEFAULT_REL_VOL_MIN = Decimal("1.5")  # traded >= 1.5x average volume

_ZERO = Decimal("0")


# --------------------------------------------------------------------- helpers


def _field(quote: Dict[str, object], key: str) -> object:
    """
    Read a Yahoo quote field, unwrapping the ``{"raw": ..., "fmt": ...}`` form some
    endpoints use. Missing -> None (the coercers below treat that as absent).
    """
    value = quote.get(key)
    if isinstance(value, dict):
        return value.get("raw")
    return value


def _dec(value: object, default: Optional[Decimal] = None) -> Optional[Decimal]:
    """Coerce any numeric (via str, to dodge binary-float artifacts) to Decimal."""
    if value is None or isinstance(value, bool):
        return default
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    if not d.is_finite():
        return default
    return d


def _int(value: object, default: Optional[int] = None) -> Optional[int]:
    """Coerce a numeric to int, tolerating floats/strings; None on failure."""
    d = _dec(value)
    if d is None:
        return default
    try:
        return int(d)
    except (InvalidOperation, ValueError):
        return default


def gap_pct(open_price: Optional[Decimal], prev_close: Optional[Decimal]) -> Optional[Decimal]:
    """
    Overnight gap as a percentage: (open - prev_close) / prev_close * 100.

    Positive = gapped up. None when either input is missing or prior close is zero
    (fail-soft — an unknown gap is not a zero gap).
    """
    if open_price is None or prev_close is None or prev_close == _ZERO:
        return None
    return (open_price - prev_close) / prev_close * Decimal("100")


def relative_volume(volume: Optional[int], avg_volume: Optional[int]) -> Optional[Decimal]:
    """Volume / average volume as a ratio (2.0 == 2x). None when avg is missing/zero."""
    if volume is None or avg_volume is None or avg_volume == 0:
        return None
    return Decimal(volume) / Decimal(avg_volume)


# ----------------------------------------------------------------- pure models


@dataclass(frozen=True)
class Quote:
    """One normalized Yahoo quote. Money is Decimal; volumes are int; pct in % units."""

    ticker: str
    price: Optional[Decimal]
    change_pct: Optional[Decimal]
    prev_close: Optional[Decimal]
    open_price: Optional[Decimal]
    volume: Optional[int]
    avg_volume: Optional[int]
    market_cap: Optional[Decimal]


@dataclass(frozen=True)
class Candidate:
    """A merged runner candidate: one ticker, the screens it hit, derived stats."""

    ticker: str
    price: Optional[Decimal]
    change_pct: Optional[Decimal]
    gap_pct: Optional[Decimal]
    rel_volume: Optional[Decimal]
    volume: Optional[int]
    market_cap: Optional[Decimal]
    screens: Tuple[str, ...]  # sorted, e.g. ("active", "gainer", "gapper")


CSV_COLUMNS: Tuple[str, ...] = (
    "ticker",
    "price",
    "change_pct",
    "gap_pct",
    "rel_volume",
    "volume",
    "market_cap",
    "screens",
)


# ------------------------------------------------------------------ pure core


def parse_quotes(payload: Dict[str, object]) -> List[Quote]:
    """
    Pull the quote list out of a Yahoo screener JSON envelope into Quotes.

    Fail-soft at every step: a malformed envelope yields []; a quote with no symbol
    is skipped; any individual missing field degrades to None rather than raising.
    """
    quotes: List[Quote] = []
    try:
        results = payload["finance"]["result"]  # type: ignore[index]
        raw_quotes = results[0]["quotes"] if results else []  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return quotes
    if not isinstance(raw_quotes, list):
        return quotes

    for raw in raw_quotes:
        if not isinstance(raw, dict):
            continue
        ticker = raw.get("symbol")
        if not ticker or not isinstance(ticker, str):
            continue
        quotes.append(
            Quote(
                ticker=ticker,
                price=_dec(_field(raw, "regularMarketPrice")),
                change_pct=_dec(_field(raw, "regularMarketChangePercent")),
                prev_close=_dec(_field(raw, "regularMarketPreviousClose")),
                open_price=_dec(_field(raw, "regularMarketOpen")),
                volume=_int(_field(raw, "regularMarketVolume")),
                avg_volume=_int(_field(raw, "averageDailyVolume3Month")),
                market_cap=_dec(_field(raw, "marketCap")),
            )
        )
    return quotes


def _merge_quote(into: Dict[str, object], q: Quote) -> None:
    """
    Fold a Quote's fields into an accumulator dict, preferring the first non-None
    value seen for each field (screens can report partial data; don't clobber a real
    number with a later None).
    """
    for key, val in (
        ("price", q.price),
        ("change_pct", q.change_pct),
        ("prev_close", q.prev_close),
        ("open_price", q.open_price),
        ("volume", q.volume),
        ("avg_volume", q.avg_volume),
        ("market_cap", q.market_cap),
    ):
        if into.get(key) is None and val is not None:
            into[key] = val


def merge_screens(
    tagged_screens: Sequence[Tuple[str, Sequence[Quote]]],
    *,
    gap_pct_min: Decimal = DEFAULT_GAP_PCT_MIN,
    rel_vol_min: Decimal = DEFAULT_REL_VOL_MIN,
    price_max: Optional[Decimal] = None,
) -> List[Candidate]:
    """
    Merge tagged screen results into one de-duplicated, sorted candidate list.

    ``tagged_screens`` is [(tag, [Quote, ...]), ...] — e.g. [("gainer", gainers),
    ("active", actives)]. A ticker appearing in several screens collects every tag.
    Each merged ticker also earns the derived ``gapper`` tag when it gapped up
    >= ``gap_pct_min`` AND traded >= ``rel_vol_min`` x average volume, and (if
    ``price_max`` is set) trades at or below that ceiling.

    Pure and deterministic. Sorted by day change descending (biggest runners first),
    ties broken by relative volume, then ticker — so the order is stable.
    """
    acc: Dict[str, Dict[str, object]] = {}
    tags: Dict[str, set] = {}

    for tag, quotes in tagged_screens:
        for q in quotes:
            slot = acc.setdefault(q.ticker, {})
            _merge_quote(slot, q)
            tags.setdefault(q.ticker, set()).add(tag)

    candidates: List[Candidate] = []
    for ticker, slot in acc.items():
        price = slot.get("price")  # type: ignore[assignment]
        g = gap_pct(slot.get("open_price"), slot.get("prev_close"))  # type: ignore[arg-type]
        rv = relative_volume(slot.get("volume"), slot.get("avg_volume"))  # type: ignore[arg-type]

        is_gapper = (
            g is not None
            and g >= gap_pct_min
            and rv is not None
            and rv >= rel_vol_min
            and (price_max is None or (price is not None and price <= price_max))
        )
        if is_gapper:
            tags[ticker].add("gapper")

        candidates.append(
            Candidate(
                ticker=ticker,
                price=price,  # type: ignore[arg-type]
                change_pct=slot.get("change_pct"),  # type: ignore[arg-type]
                gap_pct=g,
                rel_volume=rv,
                volume=slot.get("volume"),  # type: ignore[arg-type]
                market_cap=slot.get("market_cap"),  # type: ignore[arg-type]
                screens=tuple(sorted(tags[ticker])),
            )
        )

    candidates.sort(
        key=lambda c: (
            c.change_pct if c.change_pct is not None else Decimal("-1e9"),
            c.rel_volume if c.rel_volume is not None else _ZERO,
            c.ticker,
        ),
        reverse=True,
    )
    return candidates


def candidate_to_row(c: Candidate) -> Dict[str, str]:
    """
    Flatten a Candidate to string CSV cells (empty string for unknowns).

    Decimals are rounded to 2 dp for human readability ONLY here, at the output edge —
    the Candidate itself keeps full-precision Decimal truth.
    """

    def num(v: Optional[Decimal]) -> str:
        return "" if v is None else str(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def whole(v: object) -> str:
        return "" if v is None else str(v)

    return {
        "ticker": c.ticker,
        "price": num(c.price),
        "change_pct": num(c.change_pct),
        "gap_pct": num(c.gap_pct),
        "rel_volume": num(c.rel_volume),
        "volume": whole(c.volume),
        "market_cap": whole(c.market_cap),
        "screens": "|".join(c.screens),
    }


# ------------------------------------------------------------------- thin I/O


def _default_fetcher(scr_id: str, count: int) -> Dict[str, object]:  # pragma: no cover - network
    """Fetch one predefined Yahoo screen as parsed JSON. Lazy httpx import."""
    import httpx

    params = {"count": str(count), "scrIds": scr_id}
    resp = httpx.get(YAHOO_SCREENER_URL, params=params, headers=_DEFAULT_HEADERS, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def fetch_screens(
    screens: Sequence[Tuple[str, str]],
    *,
    count: int,
    fetcher: Callable[[str, int], Dict[str, object]],
) -> List[Tuple[str, List[Quote]]]:  # pragma: no cover - thin I/O wiring
    """
    Fetch + parse each (scr_id, tag) screen. Fail-soft: a screen that raises or
    returns junk contributes an empty list and logs, so one bad endpoint doesn't
    abort the scrape.
    """
    out: List[Tuple[str, List[Quote]]] = []
    for scr_id, tag in screens:
        try:
            payload = fetcher(scr_id, count)
            quotes = parse_quotes(payload)
            logger.info("screen %s (%s): %d quotes", scr_id, tag, len(quotes))
        except Exception as exc:  # noqa: BLE001 — one screen failing must not kill the run
            logger.warning("screen %s failed: %s", scr_id, exc)
            quotes = []
        out.append((tag, quotes))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - CLI wiring
    """Fetch the screens, merge to a tagged candidate list, write CSV (or stdout)."""
    parser = argparse.ArgumentParser(description="Yahoo Finance runners screener (research).")
    parser.add_argument("--count", type=int, default=50, help="quotes per screen (default 50)")
    parser.add_argument(
        "--gap-min", default=str(DEFAULT_GAP_PCT_MIN), help="min gap-up %% for the gapper tag"
    )
    parser.add_argument(
        "--rel-vol-min", default=str(DEFAULT_REL_VOL_MIN), help="min rel-volume for the gapper tag"
    )
    parser.add_argument(
        "--price-max", default=None, help="optional price ceiling for the gapper tag"
    )
    parser.add_argument("--out", default="data/runners.csv", help="output CSV path")
    parser.add_argument("--stdout", action="store_true", help="print CSV to stdout instead")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    tagged = fetch_screens(DEFAULT_SCREENS, count=args.count, fetcher=_default_fetcher)
    candidates = merge_screens(
        tagged,
        gap_pct_min=Decimal(str(args.gap_min)),
        rel_vol_min=Decimal(str(args.rel_vol_min)),
        price_max=Decimal(str(args.price_max)) if args.price_max is not None else None,
    )
    rows = [candidate_to_row(c) for c in candidates]

    if args.stdout:
        import sys

        writer = csv.DictWriter(sys.stdout, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    else:
        out_path = Path(args.out)
        if str(out_path.parent) not in ("", "."):
            out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        logger.info("Wrote %d candidates to %s", len(rows), out_path)

    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(main(sys.argv[1:]))
