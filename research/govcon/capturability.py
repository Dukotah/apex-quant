"""
research.govcon.capturability
=============================
The make-or-break test: is the mid-cap contract-award edge actually TRADEABLE by
someone acting only on PUBLIC information, net of costs?

The event study (event_study.py) measured returns from the signing-day CLOSE — but
you can't trade at a close that precedes public disclosure. DoD publicly announces
awards > $7.5M in its daily digest the EVENING of the signing date (after the close),
so the first moment a public trader can act is the NEXT market OPEN. This module
measures the realistic, capturable edge:

  signing day T = date_signed (≈ the announcement evening).
  - OVERNIGHT GAP  : T close -> T+1 open   (the announcement-night jump — UNCAPTURABLE)
  - REALISTIC ENTRY: open of T+1 / T+2 / T+3, held to +5 trading days, market-adjusted
                     vs SPY, MINUS round-trip slippage. The T+1/T+2/T+3 decay curve is
                     robust to exactly when the award went public.

If most of the edge is in the overnight gap (uncapturable) or dies under slippage,
it's an academic fact, not a strategy. If the T+1-open entry survives costs and the
bootstrap, it's real, tradeable alpha and earns a Gauntlet run.

Honest assumption: disclosure timing is modeled by the DoD daily-digest convention
(announce ~5pm ET on the signing day), not scraped per-award. The T+1/T+2/T+3 decay
curve is exactly the robustness check for that assumption. Prices are split/div
adjusted consistently (adj_open = open * adjclose/close) to avoid the raw/adjusted
mixing bug.

Run:  python -m research.govcon.capturability
"""
from __future__ import annotations

import json
import os
import random
import statistics
import time
import urllib.request
from datetime import datetime, timezone

from research.govcon.event_study import (BENCH, CACHE, END, START, UA, _aligned_index,
                                         build_events)

random.seed(11)

# Round-trip cost (bps) by cap tier — small/mid-caps have wider spreads + slippage.
COST_BPS = {"small": 45, "mid": 30, "large": 15}
ENTRY_DELAYS = (1, 2, 3)   # enter at the open this many trading days after signing
HOLD = 5                   # hold this many trading days from entry


def fetch_ohlc(ticker: str) -> dict[str, tuple[float, float]]:
    """Split/div-adjusted (open, close) per date. adj_open = open*adjclose/close."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"ohlc_{ticker}.json")
    if os.path.exists(path):
        return {d: tuple(v) for d, v in json.load(open(path)).items()}
    p1 = int(datetime.fromisoformat(START).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.fromisoformat(END).replace(tzinfo=timezone.utc).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?period1={p1}&period2={p2}&interval=1d&events=div%2Csplit")
    out: dict[str, tuple[float, float]] = {}
    try:
        j = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40))
        r = j["chart"]["result"][0]
        ts = r["timestamp"]
        q = r["indicators"]["quote"][0]
        adj = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose")
        op, cl = q["open"], q["close"]
        for i, t in enumerate(ts):
            o, c = op[i], cl[i]
            a = adj[i] if adj else c
            if o and c and a:
                ratio = a / c
                out[datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")] = (o * ratio, a)
    except Exception as e:  # noqa: BLE001
        print(f"    ohlc fetch failed for {ticker}: {repr(e)[:80]}")
    json.dump({d: list(v) for d, v in out.items()}, open(path, "w"))
    time.sleep(0.3)
    return out


def _open(px, dates, i):
    return px[dates[i]][0] if 0 <= i < len(dates) else None


def _close(px, dates, i):
    return px[dates[i]][1] if 0 <= i < len(dates) else None


def _seg(px, dates, i_from, i_to, use_open_entry):
    """Return from (open or close of i_from) to close of i_to."""
    if not (0 <= i_from < len(dates) and 0 <= i_to < len(dates)):
        return None
    p_in = _open(px, dates, i_from) if use_open_entry else _close(px, dates, i_from)
    p_out = _close(px, dates, i_to)
    if not p_in or not p_out:
        return None
    return p_out / p_in - 1.0


def main() -> None:
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("GOVCON CAPTURABILITY TEST — can a public trader actually capture it?")
    print("=" * 64)
    events = build_events(use_signed=True)   # cached awards + signing dates
    tickers = sorted({e["ticker"] for e in events})
    px = {t: fetch_ohlc(t) for t in tickers}
    spy = fetch_ohlc(BENCH)
    spy_dates = sorted(spy)

    # SAME event set as the validated event study: require +/-10 trading-day coverage
    # (stock AND SPY). Without this the filter sweeps in early-history events where the
    # whole sector drifts vs SPY, which lights up even the large-cap control. We change
    # ONLY the entry timing (close -> next open); the universe stays apples-to-apples.
    def covered(sd, i0, jd, j0):
        return (i0 is not None and j0 is not None and i0 >= 10 and j0 >= 10
                and i0 + 10 < len(sd) and j0 + 10 < len(jd))

    rows = []
    for e in events:
        sp = px.get(e["ticker"])
        if not sp:
            continue
        sd = sorted(sp)
        i0 = _aligned_index(sd, e["date"])
        j0 = _aligned_index(spy_dates, sd[i0]) if i0 is not None and i0 < len(sd) else None
        if not covered(sd, i0, spy_dates, j0):
            continue
        rec = {"cap": e["cap"], "ticker": e["ticker"]}
        # IDEAL capture: close[T]->close[T+5] (what the event study measured — the
        # unattainable best case, since you can't trade the pre-disclosure close).
        ri = _seg(sp, sd, i0, i0 + HOLD, False)
        bi = _seg(spy, spy_dates, j0, j0 + HOLD, False)
        if ri is not None and bi is not None:
            rec["ideal"] = ri - bi
        # Uncapturable overnight gap: close[T] -> open[T+1].
        p_tc, p_o1 = _close(sp, sd, i0), _open(sp, sd, i0 + 1)
        sc, so = _close(spy, spy_dates, j0), _open(spy, spy_dates, j0 + 1)
        if p_tc and p_o1 and sc and so:
            rec["gap"] = (p_o1 / p_tc - 1) - (so / sc - 1)
        # REALISTIC capture: enter open[T+k], same exit close[T+5]. Isolates what you
        # lose by entering one (or more) opens after the public announcement.
        for k in ENTRY_DELAYS:
            rs = _seg(sp, sd, i0 + k, i0 + HOLD, True)
            rb = _seg(spy, spy_dates, j0 + k, j0 + HOLD, True)
            if rs is not None and rb is not None and i0 + k <= i0 + HOLD:
                rec[f"k{k}"] = rs - rb
        rows.append(rec)

    def mean_of(sub, key, cost_bps=0.0):
        xs = [r[key] - cost_bps / 1e4 for r in sub if key in r]
        if not xs:
            return None
        return statistics.mean(xs), sum(1 for x in xs if x > 0) / len(xs), len(xs)

    def boot_p(sub, k, cost_bps):
        """p-value: random-date open[+k]->+HOLD entries on the SAME stocks beat observed."""
        c = cost_bps / 1e4
        obs = statistics.mean([r[f"k{k}"] - c for r in sub if f"k{k}" in r])
        per = {t: sorted(px[t]) for t in {r["ticker"] for r in sub} if px.get(t)}
        hits, NIT = 0, 1500
        for _ in range(NIT):
            vals = []
            for r in sub:
                d = per.get(r["ticker"])
                if not d or len(d) < 40:
                    continue
                i = random.randint(12, len(d) - HOLD - 4)
                j = _aligned_index(spy_dates, d[i])
                if j is None:
                    continue
                rs = _seg(px[r["ticker"]], d, i + k, i + HOLD, True)
                rb = _seg(spy, spy_dates, j + k, j + HOLD, True)
                if rs is not None and rb is not None:
                    vals.append(rs - rb - c)
            if vals and statistics.mean(vals) >= obs:
                hits += 1
        return obs, hits / NIT

    print(f"  {len(rows)} events (matched to event-study coverage)  hold={HOLD}d  costs(bps) {COST_BPS}")
    print("  (large-cap is the CONTROL — it must stay ~0 or the method is broken)\n")
    for cap in ("small", "mid", "large"):
        sub = [r for r in rows if r["cap"] == cap]
        if not sub:
            continue
        idl = mean_of(sub, "ideal")
        gap = mean_of(sub, "gap")
        print(f"  {cap.upper():<6} n={len(sub)}")
        if idl:
            print(f"      ideal  close[T]->close[T+5]      {idl[0]:+6.2%}   (unattainable best case)")
        if gap:
            print(f"      gap    close[T]->open[T+1]       {gap[0]:+6.2%}   (uncapturable)")
        for k in ENTRY_DELAYS:
            net = mean_of(sub, f"k{k}", COST_BPS[cap])
            if not net:
                continue
            obs, p = boot_p(sub, k, COST_BPS[cap])
            flag = "  <-- survives" if (p < 0.05 and obs > 0) else ""
            print(f"      real   open[T+{k}]->close[T+5] NET {net[0]:+6.2%}  "
                  f"win {net[1]:3.0%}  bootstrap p={p:.3f}{flag}")
        print()

    print("  VERDICT: trust the BOOTSTRAP p (vs random dates on the same stocks), not raw")
    print("  means. If large-cap control is ~0 and mid-cap real-NET T+1 is positive with")
    print("  p<0.05, it survives contact with reality -> wrap it + run the Gauntlet.")
    print("  Assumption: disclosure ~5pm ET on signing day (DoD digest); T+1/2/3 = robustness.")


if __name__ == "__main__":
    main()
