"""
scripts/fetch_yahoo.py
=====================
Download free daily OHLCV history from Yahoo Finance's public chart API (no key)
and write a combined CSV that HistoricalDataFeed / run_gauntlet_from_csv can read
directly — i.e. columns: ``timestamp,symbol,open,high,low,close,volume``.

This exists so a strategy can be validated on REAL history without paid data or
broker keys. It is a developer utility, not part of the trading runtime (the
runtime gets its data from a DataFeed). Network I/O lives here, at the edge.

Usage:
    python -m scripts.fetch_yahoo SPY EFA AGG --range 15y --out data/real/dm.csv

Notes:
  - Yahoo's endpoint is undocumented/unofficial and rate-limits aggressively; a
    User-Agent header is required. Rows with a null close (holidays/halts) are
    skipped. Timestamps are the bar dates in UTC.
  - Adjusted close is used when present (splits/dividends) so momentum/return
    math reflects total return, not raw price.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import List

_RANGE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
# period1/period2 (epoch seconds) force DAILY granularity over any span — unlike
# range=max, which Yahoo silently downsamples to monthly bars.
_PERIOD_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
               "?period1={p1}&period2={p2}&interval=1d")
_HEADERS = {"User-Agent": "Mozilla/5.0 (apex-quant data fetch)"}


def fetch_symbol(symbol: str, rng: str = "15y", start: str | None = None) -> List[dict]:
    """
    Return a list of OHLCV row dicts for one symbol (skipping null bars).

    If ``start`` (YYYY-MM-DD) is given, fetch DAILY bars from that date to now via
    period1/period2 (so long histories stay daily). Otherwise use the ``rng`` shortcut.
    """
    if start is not None:
        p1 = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        url = _PERIOD_URL.format(sym=symbol, p1=p1, p2=9999999999)
    else:
        url = _RANGE_URL.format(sym=symbol, rng=rng)
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - public API
        payload = json.load(resp)

    result = payload["chart"]["result"][0]
    stamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    # Prefer adjusted close (total return) when Yahoo provides it. Guard both the
    # absent-key and present-but-empty-list cases ([] -> IndexError on [0]).
    adjclose_list = result["indicators"].get("adjclose") or []
    adj = adjclose_list[0].get("adjclose") if adjclose_list else None

    rows: List[dict] = []
    for i, ts in enumerate(stamps):
        o, h, lo, c, v = (quote["open"][i], quote["high"][i], quote["low"][i],
                          quote["close"][i], quote["volume"][i])
        if None in (o, h, lo, c):
            continue  # holiday / halted bar — skip, don't fabricate
        # CRITICAL: O/H/L and close must share ONE adjustment basis. Yahoo gives
        # raw O/H/L but a split/dividend-adjusted close; mixing them yields corrupt
        # bars (e.g. an adjusted close below the raw low) that wreck sizing/P&L.
        # Scale O/H/L by the same ratio (adjclose/close) so the whole bar is on the
        # adjusted (total-return) basis used for strategy math.
        adj_c = adj[i] if (adj and i < len(adj) and adj[i] is not None) else c
        ratio = (adj_c / c) if c else 1.0
        o, h, lo, close = o * ratio, h * ratio, lo * ratio, adj_c
        date = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        rows.append({
            "timestamp": date, "symbol": symbol,
            "open": f"{o:.6f}", "high": f"{h:.6f}", "low": f"{lo:.6f}",
            "close": f"{close:.6f}", "volume": int(v or 0),
        })
    return rows


def fetch_combined(symbols: List[str], rng: str, out_path: Path, start: str | None = None) -> int:
    """Fetch every symbol and write one combined, chronologically-grouped CSV."""
    all_rows: List[dict] = []
    for sym in symbols:
        rows = fetch_symbol(sym, rng, start=start)
        print(f"  {sym}: {len(rows)} bars "
              f"({rows[0]['timestamp']} -> {rows[-1]['timestamp']})" if rows else f"  {sym}: 0 bars")
        all_rows.extend(rows)

    all_rows.sort(key=lambda r: (r["timestamp"], r["symbol"]))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["timestamp", "symbol", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"Wrote {len(all_rows)} rows for {len(symbols)} symbols -> {out_path}")
    return len(all_rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Download free daily OHLCV from Yahoo Finance.")
    ap.add_argument("symbols", nargs="+", help="tickers, e.g. SPY EFA AGG")
    ap.add_argument("--range", default="15y", help="Yahoo range (e.g. 5y, 10y, 15y)")
    ap.add_argument("--start", default=None, help="start date YYYY-MM-DD for DAILY long history (overrides --range)")
    ap.add_argument("--out", default="data/real/ohlcv.csv", help="output CSV path")
    args = ap.parse_args()
    try:
        fetch_combined(args.symbols, args.range, Path(args.out), start=args.start)
    except Exception as exc:  # noqa: BLE001
        print(f"Fetch failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
