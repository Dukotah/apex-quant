"""
scripts/survivorship_stress.py
==============================
Phase F1.1 — quantify how badly survivorship bias could be flattering the single-name
VALUE edge (DECISIONS Session 26).

THE PROBLEM. The S26 value edge is grade A, but on a Yahoo universe that contains only
names still listed today. Value BUYS the cheapest = the worst long-horizon performers, and
the names that delisted/blew up over 2005-2026 are disproportionately exactly those deep
laggards. Yahoo silently omits them, so the backtest never pays for the laggards that went
to zero. This bias is invisible to the Gauntlet (it's baked into the input universe), and it
is *especially* dangerous for a buy-the-laggard strategy.

THE TEST (no delisted data required). Model the bias directly: inject random DELISTINGS into
the surviving data. Each non-benchmark name, with an annualized hazard `h`, "delists" at a
random date in the measured period — its price gaps down by `severity` (a terminal crash)
and then stops trading, so any position the strategy is holding eats that loss and gets
stuck near zero (the realistic value trap). Re-run the value backtest many times across a
sweep of `h` and watch how the edge degrades:

  - If the edge survives at plausible hazard rates, it is probably real, not a mirage.
  - If it collapses at a tiny `h`, the grade-A was survivorship sugar.

Large-cap delistings run ~0.5-1%/yr unconditionally; for the deep-laggard slice value
actually holds, 2-5%/yr is a fair stress, 10% a severe one. We report the whole curve.

Deterministic given the seed (Golden Rule 10): the only randomness is the seeded stress RNG.

Run:  python -m scripts.survivorship_stress
      python -m scripts.survivorship_stress --hazards 0,0.02,0.05,0.10 --seeds 8 --severity 0.8
"""

from __future__ import annotations

import argparse
import logging
import random
import statistics
import sys
from decimal import Decimal
from typing import Dict, List, Sequence, Tuple

from apex.backtest.backtester import run_backtest
from apex.core.events import MarketEvent
from apex.core.models import AssetClass, Bar, Symbol
from apex.data.historical_feed import HistoricalDataFeed
from apex.risk.risk_manager import RiskConfig
from apex.strategy.library.cross_asset_value import CrossAssetValueStrategy
from apex.validation import metrics

# Mirror validate_real.validate_value_singlenames so the stress is on the SAME edge.
_UNIVERSE = [
    "AAPL",
    "MSFT",
    "NVDA",
    "ORCL",
    "CSCO",
    "IBM",
    "INTC",
    "QCOM",
    "TXN",
    "ADBE",
    "JPM",
    "BAC",
    "WFC",
    "GS",
    "AXP",
    "C",
    "JNJ",
    "PFE",
    "MRK",
    "ABT",
    "UNH",
    "BMY",
    "PG",
    "KO",
    "PEP",
    "WMT",
    "MCD",
    "HD",
    "NKE",
    "COST",
    "DIS",
    "XOM",
    "CVX",
    "COP",
    "GE",
    "CAT",
    "BA",
    "HON",
    "MMM",
    "UPS",
    "T",
    "VZ",
]
_DATA = "data/real/single_names.csv"
_BENCHMARK = "SPY"  # rides in the data file; never delisted, never traded
_SLIPPAGE = Decimal("0.001")


def _risk() -> RiskConfig:
    return RiskConfig(
        max_position_size_pct=Decimal("0.12"),
        max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("1.0"),
        max_drawdown_pct=Decimal("0.99"),
        max_daily_loss_pct=Decimal("0.99"),
        require_stop_loss=True,
        max_open_positions=15,
    )


def _strategy() -> CrossAssetValueStrategy:
    syms = [Symbol(t, AssetClass.EQUITY) for t in _UNIVERSE]
    return CrossAssetValueStrategy(
        "xasset_value_sn",
        syms,
        value_period=1260,
        skip_recent=252,
        top_k=10,
        exit_rank_buffer=10,
    )


# ----------------------------------------------------------------- injection (pure)


def _crash_bar(bar: Bar, severity: float) -> Bar:
    """A terminal down-gap: close drops by `severity`, OHLC kept self-consistent."""
    factor = Decimal(str(1.0 - severity))
    crashed = bar.close * factor
    return Bar(
        symbol=bar.symbol,
        timestamp=bar.timestamp,
        open=bar.close,  # gaps down from the prior close
        high=bar.close,
        low=crashed,
        close=crashed,
        volume=bar.volume,
    )


def inject_delistings(
    events: Sequence[MarketEvent],
    hazard_annual: float,
    severity: float,
    seed: int,
    protect: frozenset = frozenset({_BENCHMARK}),
) -> Tuple[List[MarketEvent], Dict[str, str]]:
    """
    Return a copy of `events` with random delistings injected, plus {ticker: delist_date}.

    Pure and deterministic given `seed`. A delisted name keeps its bars up to a random date
    in the latter half of its life, has that final bar replaced by a `severity` crash, and
    trades no more. hazard_annual=0 returns the events unchanged.
    """
    rng = random.Random(seed)
    # Group bars per ticker in chronological order (events arrive timestamp-sorted).
    per_ticker: Dict[str, List[MarketEvent]] = {}
    for ev in events:
        per_ticker.setdefault(ev.bar.symbol.ticker, []).append(ev)

    delisted: Dict[str, str] = {}
    for ticker, evs in per_ticker.items():
        if ticker in protect or hazard_annual <= 0 or len(evs) < 4:
            continue
        span_days = (evs[-1].bar.timestamp - evs[0].bar.timestamp).days or 1
        years = span_days / 365.25
        p_delist = 1.0 - (1.0 - hazard_annual) ** years
        if rng.random() >= p_delist:
            continue
        # Delist somewhere in the latter half (past warmup, inside the measured window).
        idx = rng.randint(len(evs) // 2, len(evs) - 1)
        kept = evs[:idx]
        crash_ev = MarketEvent(bar=_crash_bar(evs[idx].bar, severity))
        per_ticker[ticker] = kept + [crash_ev]
        delisted[ticker] = evs[idx].bar.timestamp.date().isoformat()

    rebuilt = [ev for evs in per_ticker.values() for ev in evs]
    rebuilt.sort(key=lambda ev: (ev.bar.timestamp, ev.bar.symbol.ticker))
    return rebuilt, delisted


# ----------------------------------------------------------------- sweep (heavy)


def _full_sharpe(events: Sequence[MarketEvent], slippage: Decimal) -> Tuple[float, int]:
    res = run_backtest(list(events), _strategy(), _risk(), slippage_pct=slippage)
    rets = metrics.returns_from_equity(res.equity_curve)
    sharpe = metrics.sharpe_ratio(rets) if len(rets) >= 2 else 0.0
    return sharpe, res.num_trades


def stress_sweep(
    events: Sequence[MarketEvent], hazards: List[float], seeds: int, severity: float
) -> List[dict]:
    """For each hazard, run `seeds` injected trials at 1x and 2x cost; aggregate."""
    rows: List[dict] = []
    for h in hazards:
        s1x, s2x, dlcounts = [], [], []
        n_trials = 1 if h <= 0 else seeds  # h=0 is deterministic; one run is enough
        for seed in range(n_trials):
            injected, delisted = inject_delistings(events, h, severity, seed)
            dlcounts.append(len(delisted))
            sh1, _ = _full_sharpe(injected, _SLIPPAGE)
            sh2, _ = _full_sharpe(injected, _SLIPPAGE * Decimal("2"))
            s1x.append(sh1)
            s2x.append(sh2)
        rows.append(
            {
                "hazard": h,
                "avg_delisted": statistics.mean(dlcounts),
                "median_sharpe_1x": statistics.median(s1x),
                "median_sharpe_2x": statistics.median(s2x),
                "min_sharpe_2x": min(s2x),
                "pass_rate_2x": sum(1 for s in s2x if s >= 0.5) / len(s2x),
            }
        )
    return rows


def _utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def main() -> int:
    _utf8()
    # Silence per-signal risk rejections (the hold band > position cap logs a WARNING on
    # every blocked entry). They're expected and would both flood and slow the sweep.
    logging.disable(logging.WARNING)
    ap = argparse.ArgumentParser(description="Survivorship-bias stress test for the value edge.")
    ap.add_argument("--hazards", default="0,0.02,0.05,0.10", help="comma annual delist hazards")
    ap.add_argument("--seeds", type=int, default=8, help="trials per non-zero hazard")
    ap.add_argument("--severity", type=float, default=0.8, help="terminal crash fraction")
    ap.add_argument("--data", default=_DATA)
    args = ap.parse_args()

    hazards = [float(x) for x in args.hazards.split(",")]
    syms = [Symbol(t, AssetClass.EQUITY) for t in _UNIVERSE] + [Symbol(_BENCHMARK, AssetClass.ETF)]
    try:
        feed = HistoricalDataFeed(syms, args.data)
        feed.connect()
        try:
            events = list(feed.stream())
        finally:
            feed.disconnect()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not load {args.data}: {exc}", file=sys.stderr)
        print("Regenerate it — see the command in scripts/validate_real.py.", file=sys.stderr)
        return 1

    print(
        f"Survivorship stress: {len(_UNIVERSE)} names, severity={args.severity:.0%}, "
        f"{args.seeds} trials/hazard. Edge passes cost-stress when Sharpe@2x >= 0.50.\n"
    )
    print(
        f"{'hazard/yr':>9} {'~delisted':>10} {'Sharpe@1x':>10} {'Sharpe@2x':>10} "
        f"{'min@2x':>8} {'pass%@2x':>9}"
    )
    rows = []
    for h in hazards:
        (r,) = stress_sweep(events, [h], args.seeds, args.severity)
        rows.append(r)
        print(
            f"{r['hazard']:>9.0%} {r['avg_delisted']:>10.1f} {r['median_sharpe_1x']:>10.2f} "
            f"{r['median_sharpe_2x']:>10.2f} {r['min_sharpe_2x']:>8.2f} {r['pass_rate_2x']:>8.0%}",
            flush=True,
        )
    print(f"\n{_verdict(rows)}")
    return 0


# Large-cap delistings run ~0.5-1%/yr unconditionally; the deep-laggard slice value holds is
# higher, so ~3%/yr is the upper end of a *realistic* survivorship haircut.
_REALISTIC_HAZARD = 0.03
_COST_BAR = 0.5


def _verdict(rows: List[dict]) -> str:
    """One-line read: does the edge survive a realistic survivorship haircut?"""
    realistic = [r for r in rows if 0 < r["hazard"] <= _REALISTIC_HAZARD]
    if not realistic:
        return "VERDICT: no realistic-hazard rows (<= 3%/yr) sampled — inconclusive."
    worst = min(realistic, key=lambda r: r["median_sharpe_2x"])
    median_ok = worst["median_sharpe_2x"] >= _COST_BAR
    robust_tail = all(r["pass_rate_2x"] >= 0.7 for r in realistic)
    if median_ok and robust_tail:
        tag = "SURVIVES"
        note = "median edge clears the cost bar through realistic hazard, tail mostly holds"
    elif median_ok:
        tag = "SURVIVES (tail-sensitive)"
        note = "median holds but some high-delisting universes dip under the cost bar"
    else:
        tag = "FRAGILE / likely survivorship-flattered"
        note = "the median edge caves at a realistic hazard"
    return (
        f"VERDICT: {tag} — at {worst['hazard']:.0%}/yr hazard the median Sharpe@2x is "
        f"{worst['median_sharpe_2x']:.2f} (pass {worst['pass_rate_2x']:.0%}). {note}."
    )


if __name__ == "__main__":
    raise SystemExit(main())
