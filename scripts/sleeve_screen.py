"""
scripts/sleeve_screen.py
========================
Pick trend-following sleeves by what actually matters (Session 9's law): UNCORRELATED
return drivers, not sleeve count. For each candidate ETF it computes the *trend-sleeve*
return stream — the daily return only while price is above its 200-day SMA (what the
deployed MultiAssetTrendStrategy actually harvests), zero when flat — then:

  1. the trend-sleeve correlation matrix (the honest diversification measure),
  2. each sleeve's standalone trend Sharpe,
  3. a greedy maximally-uncorrelated selection of K sleeves.

The greedy set is the candidate to run through the Gauntlet (validate_real). This
turns "guess some tickers" into a repeatable, principled screen.

Run:  python -m scripts.sleeve_screen [K]
"""
from __future__ import annotations

import statistics
import sys

from apex.core.models import AssetClass, Symbol
from apex.data.historical_feed import HistoricalDataFeed
from apex.strategy import indicators as ind

POOL = ["SPY", "EFA", "TLT", "GLD", "DBC", "UUP", "DBA", "FXY", "DBB", "DBO", "TIP", "BWX", "HYG"]
PATH = "data/real/sleeve_pool.csv"
SLOW = 200
ANN = 252 ** 0.5


def _load():
    syms = [Symbol(t, AssetClass.ETF) for t in POOL]
    feed = HistoricalDataFeed(syms, PATH)
    feed.connect()
    ev = list(feed.stream())
    feed.disconnect()
    px = {t: {} for t in POOL}
    for e in ev:
        b = e.bar
        px[b.symbol.ticker][b.timestamp] = float(b.close)
    return {t: v for t, v in px.items() if v}


def _trend_returns(px):
    """ticker -> {date: trend-sleeve return} (the daily return while in uptrend, else 0)."""
    out = {}
    for t, dmap in px.items():
        ds = sorted(dmap)
        cs = [dmap[d] for d in ds]
        sma = ind.sma(cs, SLOW)
        rr = {}
        for i in range(1, len(cs)):
            in_trend = sma[i - 1] is not None and cs[i - 1] > sma[i - 1]
            rr[ds[i]] = (cs[i] / cs[i - 1] - 1) if (in_trend and cs[i - 1]) else 0.0
        out[t] = rr
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    px = _load()
    names = [t for t in POOL if t in px]
    tr = _trend_returns(px)
    common = sorted(set.intersection(*[set(tr[t]) for t in names]))
    vec = {t: [tr[t][d] for d in common] for t in names}
    print(f"sleeve screen — {len(names)} candidates, {len(common)} common days "
          f"({common[0].date()}..{common[-1].date()})\n")

    def corr(a, b):
        try:
            return statistics.correlation(vec[a], vec[b])
        except Exception:  # noqa: BLE001
            return 0.0

    sharpe = {t: (statistics.mean(vec[t]) / (statistics.pstdev(vec[t]) or 1e-9)) * ANN for t in names}

    # Greedy: seed with the best standalone Sharpe, then repeatedly add the sleeve with
    # the lowest average |correlation| to those already chosen.
    chosen = [max(names, key=lambda t: sharpe[t])]
    while len(chosen) < min(k, len(names)):
        cand = [t for t in names if t not in chosen]
        nxt = min(cand, key=lambda t: statistics.mean(abs(corr(t, s)) for s in chosen))
        chosen.append(nxt)

    print("standalone trend Sharpe (annualized):")
    for t in sorted(names, key=lambda x: -sharpe[x]):
        mark = " *" if t in chosen else "  "
        print(f"  {mark} {t:<5} {sharpe[t]:+.2f}")

    print(f"\nGREEDY UNCORRELATED SET (K={len(chosen)}): {chosen}")
    print("  avg |corr| within set: "
          f"{statistics.mean(abs(corr(a, b)) for a in chosen for b in chosen if a != b):.3f}")
    print("\n  pairwise trend-sleeve correlations (selected set):")
    print("        " + "".join(f"{t:>6}" for t in chosen))
    for a in chosen:
        row = "".join(f"{corr(a, b):+6.2f}" for b in chosen)
        print(f"  {a:<5} {row}")
    print(f"\n  -> run it: add a validate_real variant for {chosen} and gauntlet it.")


if __name__ == "__main__":
    main()
