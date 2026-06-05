"""
research.govcon.event_study
===========================
Does winning a large federal contract predict abnormal stock returns — and is the
effect concentrated in small-caps (the hypothesis)? This is an honest event study,
built to FIND or KILL the edge, not flatter it.

Method
------
- Universe: curated public contractors (research/govcon/universe.py), tagged by
  cap tier and entity-resolution confidence.
- Events: each award >= MIN_AWARD_USD to a name we can map (confidence high/med).
- For each event we measure MARKET-ADJUSTED returns (stock minus SPY) over:
    pre[-10,0]   : run-up INTO the event day — the leakage/date-lag diagnostic.
                   USAspending dates lag the press announcement, so if the move is
                   real it often lands HERE, not after our event day.
    fwd[0,+1], fwd[0,+5], fwd[0,+10] : the tradeable forward windows.
- Aggregate by cap tier, with a t-stat AND a bootstrap p-value: we re-draw the same
  number of RANDOM event dates per stock 2000x and ask how often random dates beat
  the real ones. That controls for each stock's own drift/beta (the thing that made
  the naive single-name test look fake).

Honesty / known limits (printed in the report too):
- Event date = contract action/PoP date, NOT the true press-release timestamp.
- Universe is not survivorship-free (delisted contractors are missing → upward bias).
- Entity resolution is partial; 'med'-confidence names may attribute subsidiary
  awards imperfectly. Treat a positive result as a reason to dig, not to trade.

Run:  python -m research.govcon.event_study
Data is cached under research/govcon/cache/ so re-runs are instant and API-light.
"""
from __future__ import annotations

import json
import math
import os
import random
import statistics
import time
import urllib.request
from datetime import datetime, timezone

from research.govcon.universe import MIN_AWARD_USD, UNIVERSE

CACHE = os.path.join(os.path.dirname(__file__), "cache")
UA = {"User-Agent": "Mozilla/5.0 (research event-study; contact: local)"}
BENCH = "SPY"
START, END = "2016-01-01", "2024-06-30"
random.seed(7)


# --------------------------------------------------------------- cached fetch

def _ensure_cache() -> None:
    os.makedirs(CACHE, exist_ok=True)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)[:60]


def fetch_awards(name: str) -> list[dict]:
    _ensure_cache()
    path = os.path.join(CACHE, f"awards_{_safe(name)}.json")
    if os.path.exists(path):
        return json.load(open(path))
    body = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],
            "recipient_search_text": [name],
            "time_period": [{"start_date": START, "end_date": END}],
        },
        "fields": ["Recipient Name", "Start Date", "Award Amount", "Awarding Agency"],
        "sort": "Award Amount", "order": "desc", "limit": 60,
    }
    req = urllib.request.Request(
        "https://api.usaspending.gov/api/v2/search/spending_by_award/",
        data=json.dumps(body).encode(), headers={**UA, "Content-Type": "application/json"})
    rows = json.load(urllib.request.urlopen(req, timeout=40)).get("results", [])
    json.dump(rows, open(path, "w"))
    time.sleep(0.4)
    return rows


def fetch_prices(ticker: str) -> dict[str, float]:
    _ensure_cache()
    path = os.path.join(CACHE, f"px_{_safe(ticker)}.json")
    if os.path.exists(path):
        return json.load(open(path))
    p1 = int(datetime.fromisoformat(START).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.fromisoformat(END).replace(tzinfo=timezone.utc).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?period1={p1}&period2={p2}&interval=1d&events=div%2Csplit")
    try:
        j = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40))
        r = j["chart"]["result"][0]
        ts = r["timestamp"]
        ind = r["indicators"]
        adj = ind.get("adjclose", [{}])[0].get("adjclose") if ind.get("adjclose") else None
        close = ind["quote"][0]["close"]
        series = adj or close
        out = {}
        for t, c in zip(ts, series):
            if c is not None:
                out[datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")] = float(c)
    except Exception as e:  # noqa: BLE001
        print(f"    price fetch failed for {ticker}: {repr(e)[:80]}")
        out = {}
    json.dump(out, open(path, "w"))
    time.sleep(0.4)
    return out


# --------------------------------------------------------------- event study

def _aligned_index(dates: list[str], target: str) -> int | None:
    """Index of the first trading day >= target (the event day t0)."""
    for i, d in enumerate(dates):
        if d >= target:
            return i
    return None


def _ret(prices: dict, dates: list[str], i0: int, lo: int, hi: int) -> float | None:
    """Return from day i0+lo to i0+hi (close-to-close)."""
    a, b = i0 + lo, i0 + hi
    if a < 0 or b < 0 or a >= len(dates) or b >= len(dates):
        return None
    pa, pb = prices.get(dates[a]), prices.get(dates[b])
    if not pa or not pb:
        return None
    return pb / pa - 1.0


# windows: name -> (lo, hi) relative to event day t0
WINDOWS = {"pre[-10,0]": (-10, 0), "fwd[0,+1]": (0, 1), "fwd[0,+5]": (0, 5), "fwd[0,+10]": (0, 10)}


def fetch_signed_dates(aids: list[str]) -> dict[str, str]:
    """date_signed (the contract-signing date ~ the announcement) per award id.
    Cached in one file so the ~hundreds of detail calls only happen once."""
    _ensure_cache()
    path = os.path.join(CACHE, "signed_dates.json")
    cache = json.load(open(path)) if os.path.exists(path) else {}
    todo = [a for a in aids if a and a not in cache]
    for n, aid in enumerate(todo, 1):
        url = f"https://api.usaspending.gov/api/v2/awards/{aid}/"
        try:
            j = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30))
            cache[aid] = j.get("date_signed")
        except Exception:  # noqa: BLE001
            cache[aid] = None
        if n % 40 == 0:
            json.dump(cache, open(path, "w"))
            print(f"    signed dates: {n}/{len(todo)} fetched...")
        time.sleep(0.2)
    json.dump(cache, open(path, "w"))
    return cache


def build_events(use_signed: bool = True) -> list[dict]:
    raw = []
    skipped_low = 0
    for ticker, name, cap, conf, _note in UNIVERSE:
        if conf == "low":
            skipped_low += 1
            continue
        for a in fetch_awards(name):
            amt = a.get("Award Amount") or 0
            d = a.get("Start Date")
            rname = (a.get("Recipient Name") or "").upper()
            token = name.split()[0]   # recipient must actually contain our search token
            if amt >= MIN_AWARD_USD and d and token in rname:
                raw.append({"ticker": ticker, "date": d, "amount": amt, "cap": cap,
                            "conf": conf, "aid": a.get("generated_internal_id")})
    # Replace PoP-start with the contract SIGNING date (closer to the announcement).
    if use_signed:
        signed = fetch_signed_dates([e["aid"] for e in raw])
        moved = 0
        for e in raw:
            sd = signed.get(e["aid"])
            if sd:
                if sd != e["date"]:
                    moved += 1
                e["date"] = sd
        print(f"  resolved signing dates ({moved} differ from PoP-start)")
    print(f"  built {len(raw)} events from {len({e['ticker'] for e in raw})} mappable names "
          f"({skipped_low} low-confidence names skipped)")
    return raw


def _abnormal(events: list[dict], px: dict, bench_dates: list[str], bench_px: dict):
    """For each event, market-adjusted return per window. Returns rows of dicts."""
    rows = []
    for e in events:
        sp = px.get(e["ticker"])
        if not sp:
            continue
        sdates = sorted(sp)
        i0 = _aligned_index(sdates, e["date"])
        j0 = _aligned_index(bench_dates, e["date"])
        if i0 is None or j0 is None:
            continue
        rec = {"ticker": e["ticker"], "cap": e["cap"], "amount": e["amount"]}
        ok = True
        for w, (lo, hi) in WINDOWS.items():
            rs = _ret(sp, sdates, i0, lo, hi)
            rb = _ret(bench_px, bench_dates, j0, lo, hi)
            if rs is None or rb is None:
                ok = False
                break
            rec[w] = rs - rb
        if ok:
            rows.append(rec)
    return rows


def _bootstrap_pvalue(events: list[dict], px: dict, bench_dates: list[str],
                      bench_px: dict, window: str, observed_mean: float, n_iter: int = 2000) -> float:
    """Empirical p: fraction of random-date redraws whose mean abnormal return >= observed."""
    lo, hi = WINDOWS[window]
    # Pre-index each ticker's tradeable range so random draws are valid.
    per_ticker = {}
    for e in events:
        sp = px.get(e["ticker"])
        if sp and e["ticker"] not in per_ticker:
            per_ticker[e["ticker"]] = sorted(sp)
    valid = [e for e in events if e["ticker"] in per_ticker]
    if not valid:
        return float("nan")
    hits = 0
    for _ in range(n_iter):
        vals = []
        for e in valid:
            sdates = per_ticker[e["ticker"]]
            if len(sdates) < 40:
                continue
            i0 = random.randint(15, len(sdates) - 15)
            d = sdates[i0]
            j0 = _aligned_index(bench_dates, d)
            if j0 is None:
                continue
            rs = _ret(px[e["ticker"]], sdates, i0, lo, hi)
            rb = _ret(bench_px, bench_dates, j0, lo, hi)
            if rs is not None and rb is not None:
                vals.append(rs - rb)
        if vals and statistics.mean(vals) >= observed_mean:
            hits += 1
    return hits / n_iter


def _summary(rows: list[dict], label: str) -> None:
    if not rows:
        print(f"  {label:<14} (no events)")
        return
    print(f"  {label:<14} n={len(rows)}")
    for w in WINDOWS:
        xs = [r[w] for r in rows if w in r]
        if not xs:
            continue
        m = statistics.mean(xs)
        sd = statistics.pstdev(xs) or 1e-9
        t = m / (sd / math.sqrt(len(xs)))
        win = sum(1 for x in xs if x > 0) / len(xs)
        print(f"      {w:<12} mean {m:+6.2%}   t={t:+5.2f}   win {win:4.0%}")


def main() -> None:
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    print("GOVCON CONTRACT-AWARD EVENT STUDY")
    print("=" * 60)
    print(f"  universe={len(UNIVERSE)} names  min award=${MIN_AWARD_USD:,}  window {START}..{END}")
    events = build_events()

    # Fetch prices for every ticker that has events, plus the benchmark.
    tickers = sorted({e["ticker"] for e in events})
    px = {t: fetch_prices(t) for t in tickers}
    bench = fetch_prices(BENCH)
    bench_dates = sorted(bench)

    rows = _abnormal(events, px, bench_dates, bench)
    print(f"  {len(rows)} events with full price coverage\n")

    print("BY CAP TIER (market-adjusted vs SPY):")
    for cap in ("small", "mid", "large"):
        _summary([r for r in rows if r["cap"] == cap], cap)
    print()
    _summary(rows, "ALL")

    # Significance (bootstrap vs random-date drift) on the tradeable windows for
    # each tier — small-cap is the hypothesis; mid-cap is where the t-stats flickered.
    print("\n  BOOTSTRAP p-values (mean abnormal return vs random-date null):")
    for cap in ("small", "mid", "large"):
        sub = [r for r in rows if r["cap"] == cap]
        if not sub:
            continue
        for w in ("fwd[0,+1]", "fwd[0,+5]"):
            obs = statistics.mean([r[w] for r in sub])
            p = _bootstrap_pvalue([{"ticker": r["ticker"]} for r in sub],
                                  px, bench_dates, bench, w, obs)
            flag = "  <-- significant" if p < 0.05 else ""
            print(f"    {cap:<6} {w:<10} mean {obs:+.2%}   p={p:.3f}{flag}")
    print("  (p<0.05 = unlikely to be random drift; high p = no real edge)")

    print("\nCAVEATS: event date = contract/PoP date, NOT the press-release timestamp")
    print("  (so look at pre[-10,0] for leakage); universe not survivorship-free")
    print("  (delisted contractors missing → upward bias); entity resolution partial.")


if __name__ == "__main__":
    main()
