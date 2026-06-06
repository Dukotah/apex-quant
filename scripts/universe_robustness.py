"""
scripts/universe_robustness.py
==============================
Phase F1.3 — Universe robustness of the value edge.

QUESTION: does the value edge depend on the exact 42 names, or is it broad?

METHOD. Repeatedly draw random SUBSETS of _UNIVERSE (default 30 of 42), run the
SAME CrossAssetValueStrategy on each subset, and report the DISPERSION of Sharpe
and Sharpe@2x across N seeded draws. If the edge is broad, the median and minimum
Sharpe will stay healthy across random subsets; if it depends on a handful of
lucky names, the spread will be wide and the minimum poor.

PURE HELPERS (unit-tested, no I/O):
  draw_subset(universe, size, seed)   -> list[str]  — deterministic subset draw
  filter_events_to(events, tickers)   -> list[MarketEvent] — restrict event stream

HEAVY LOGIC (in main, not tested):
  load events once, run draw x N subsets, print dispersion table + verdict.

Run:
  python -m scripts.universe_robustness
  python -m scripts.universe_robustness --size 30 --draws 20 --seed0 0 --data data/real/single_names.csv
"""

from __future__ import annotations

import argparse
import logging
import random
import statistics
import sys
from decimal import Decimal
from typing import Sequence

from apex.backtest.backtester import run_backtest
from apex.core.events import MarketEvent
from apex.core.models import AssetClass, Symbol
from apex.data.historical_feed import HistoricalDataFeed
from apex.strategy.library.cross_asset_value import CrossAssetValueStrategy
from apex.validation import metrics

# Reuse the shared universe, risk config, and Sharpe helper from F1.1.
from scripts.survivorship_stress import _UNIVERSE, _risk

_DATA = "data/real/single_names.csv"
_BENCHMARK = "SPY"
_SLIPPAGE = Decimal("0.001")
_COST_BAR = 0.5  # minimum Sharpe@2x that clears the cost hurdle


# ------------------------------------------------------------------ pure helpers


def draw_subset(universe: Sequence[str], size: int, seed: int) -> list[str]:
    """
    Return a deterministic, sorted random subset of `universe` with `size` elements.

    Deterministic: same universe + size + seed always yields the same list.
    Sorted: deterministic output ordering, independent of insertion order.

    Args:
        universe: collection of ticker strings to draw from.
        size:     number of tickers to select (must be 1 <= size <= len(universe)).
        seed:     integer seed for the RNG — ensures reproducibility.

    Returns:
        Sorted list of `size` tickers drawn without replacement from `universe`.

    Raises:
        ValueError: if size is out of range.
    """
    uniq = list(dict.fromkeys(universe))  # deduplicate while preserving first-seen order
    if not 1 <= size <= len(uniq):
        raise ValueError(f"size must be between 1 and {len(uniq)} (len(universe)); got {size}")
    rng = random.Random(seed)
    chosen = rng.sample(uniq, size)
    return sorted(chosen)


def filter_events_to(
    events: Sequence[MarketEvent],
    tickers: Sequence[str],
) -> list[MarketEvent]:
    """
    Return a new list containing only events whose bar belongs to a ticker in `tickers`.

    Pure: does not mutate `events`. Preserves the original event order (assumed
    chronological). The benchmark (SPY) is always included even if absent from `tickers`
    so that value-score comparisons work correctly across subsets.

    Args:
        events:  full MarketEvent stream (all tickers).
        tickers: whitelist of ticker strings to keep.

    Returns:
        Filtered list of MarketEvents.
    """
    keep = frozenset(tickers)
    return [ev for ev in events if ev.bar.symbol.ticker in keep]


# ------------------------------------------------------------------ subset strategy builder


def _strategy_for(tickers: list[str]) -> CrossAssetValueStrategy:
    """Build a CrossAssetValueStrategy scoped to `tickers` (same params as survivorship_stress)."""
    syms = [Symbol(t, AssetClass.EQUITY) for t in tickers]
    return CrossAssetValueStrategy(
        "xasset_value_subset",
        syms,
        value_period=1260,
        skip_recent=252,
        top_k=10,
        exit_rank_buffer=10,
    )


# ------------------------------------------------------------------ sweep (heavy, in main only)


def run_subset_sweep(
    events: Sequence[MarketEvent],
    universe: list[str],
    size: int,
    n_draws: int,
    seed0: int,
) -> list[dict]:
    """
    Run `n_draws` random subset backtests and return per-draw result dicts.

    Each draw uses seed = seed0 + draw_index so results are independent and reproducible.
    Events are filtered to the subset tickers for each draw.

    Args:
        events:   full event list (all tickers + benchmark).
        universe: list of tickers to draw from.
        size:     subset size per draw.
        n_draws:  number of random subsets to evaluate.
        seed0:    base seed; draw i uses seed0 + i.

    Returns:
        List of dicts with keys: draw, seed, tickers, sharpe_1x, sharpe_2x.
    """
    rows: list[dict] = []
    for i in range(n_draws):
        seed = seed0 + i
        subset = draw_subset(universe, size, seed)
        # Always include SPY so HistoricalDataFeed events for it are present, but the
        # strategy only ranks the equity subset (SPY is not in the strategy's symbol list).
        subset_events = filter_events_to(events, subset + [_BENCHMARK])
        strategy = _strategy_for(subset)
        result = run_backtest(subset_events, strategy, _risk(), slippage_pct=_SLIPPAGE)
        rets = metrics.returns_from_equity(result.equity_curve)
        sh1x = metrics.sharpe_ratio(rets) if len(rets) >= 2 else 0.0

        result2 = run_backtest(
            subset_events, _strategy_for(subset), _risk(), slippage_pct=_SLIPPAGE * Decimal("2")
        )
        rets2 = metrics.returns_from_equity(result2.equity_curve)
        sh2x = metrics.sharpe_ratio(rets2) if len(rets2) >= 2 else 0.0

        rows.append(
            {
                "draw": i,
                "seed": seed,
                "tickers": subset,
                "sharpe_1x": sh1x,
                "sharpe_2x": sh2x,
            }
        )
    return rows


# ------------------------------------------------------------------ reporting


def _summarise(rows: list[dict]) -> dict:
    """Aggregate per-draw rows into dispersion statistics."""
    s1 = [r["sharpe_1x"] for r in rows]
    s2 = [r["sharpe_2x"] for r in rows]
    pass_count = sum(1 for s in s2 if s >= _COST_BAR)
    return {
        "n": len(rows),
        "median_1x": statistics.median(s1),
        "min_1x": min(s1),
        "max_1x": max(s1),
        "median_2x": statistics.median(s2),
        "min_2x": min(s2),
        "max_2x": max(s2),
        "pct_pass_2x": pass_count / len(rows) if rows else 0.0,
    }


def _verdict(summary: dict, size: int, universe_size: int) -> str:
    """One-line read: is the value edge broad across random subsets?"""
    med2 = summary["median_2x"]
    pct = summary["pct_pass_2x"]
    mn2 = summary["min_2x"]
    n = summary["n"]

    if med2 >= _COST_BAR and pct >= 0.70:
        tag = "BROAD"
        note = (
            f"median Sharpe@2x {med2:.2f} clears the {_COST_BAR} bar and "
            f"{pct:.0%} of {n} random {size}-of-{universe_size} subsets pass"
        )
    elif med2 >= _COST_BAR:
        tag = "MODERATELY BROAD (tail-sensitive)"
        note = (
            f"median Sharpe@2x {med2:.2f} holds but only {pct:.0%} of draws pass "
            f"(min {mn2:.2f}); a few subsets are weak"
        )
    else:
        tag = "CONCENTRATED / name-dependent"
        note = (
            f"median Sharpe@2x {med2:.2f} is below the {_COST_BAR} cost bar; "
            f"the edge may depend on specific names in the full universe"
        )
    return f"VERDICT: {tag} — {note}."


def _utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _utf8()
    logging.disable(logging.WARNING)

    ap = argparse.ArgumentParser(
        description="Universe robustness: value edge across random subsets."
    )
    ap.add_argument("--size", type=int, default=30, help="subset size per draw (default 30)")
    ap.add_argument("--draws", type=int, default=20, help="number of random subsets (default 20)")
    ap.add_argument(
        "--seed0", type=int, default=0, help="base seed; draw i uses seed0+i (default 0)"
    )
    ap.add_argument("--data", default=_DATA)
    args = ap.parse_args()

    universe = list(_UNIVERSE)
    if args.size > len(universe):
        print(
            f"--size {args.size} exceeds universe size {len(universe)}; clamping.",
            file=sys.stderr,
        )
        args.size = len(universe)

    syms = [Symbol(t, AssetClass.EQUITY) for t in universe] + [Symbol(_BENCHMARK, AssetClass.ETF)]
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
        f"Universe robustness: {len(universe)} names, subset size={args.size}, "
        f"{args.draws} random draws, base-seed={args.seed0}. "
        f"Edge passes cost-stress when Sharpe@2x >= {_COST_BAR}.\n"
    )

    # Header
    print(f"{'draw':>5} {'seed':>6} {'Sharpe@1x':>10} {'Sharpe@2x':>10}")
    print("-" * 35)

    rows = []
    for i in range(args.draws):
        seed = args.seed0 + i
        subset = draw_subset(universe, args.size, seed)
        subset_events = filter_events_to(events, subset + [_BENCHMARK])
        strategy = _strategy_for(subset)

        result = run_backtest(subset_events, strategy, _risk(), slippage_pct=_SLIPPAGE)
        rets = metrics.returns_from_equity(result.equity_curve)
        sh1x = metrics.sharpe_ratio(rets) if len(rets) >= 2 else 0.0

        result2 = run_backtest(
            subset_events, _strategy_for(subset), _risk(), slippage_pct=_SLIPPAGE * Decimal("2")
        )
        rets2 = metrics.returns_from_equity(result2.equity_curve)
        sh2x = metrics.sharpe_ratio(rets2) if len(rets2) >= 2 else 0.0

        rows.append({"draw": i, "seed": seed, "sharpe_1x": sh1x, "sharpe_2x": sh2x})
        print(f"{i:>5} {seed:>6} {sh1x:>10.2f} {sh2x:>10.2f}", flush=True)

    print("-" * 35)
    summary = _summarise(rows)
    print(
        f"\nSummary ({summary['n']} draws, subset {args.size} of {len(universe)}):\n"
        f"  Sharpe@1x  median={summary['median_1x']:.2f}  "
        f"min={summary['min_1x']:.2f}  max={summary['max_1x']:.2f}\n"
        f"  Sharpe@2x  median={summary['median_2x']:.2f}  "
        f"min={summary['min_2x']:.2f}  max={summary['max_2x']:.2f}  "
        f"pass%={summary['pct_pass_2x']:.0%}\n"
    )
    print(_verdict(summary, args.size, len(universe)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
