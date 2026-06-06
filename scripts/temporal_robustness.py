"""
scripts/temporal_robustness.py
==============================
Phase F1.2 — temporal robustness of the single-name value edge (DECISIONS Session 27).

THE QUESTION. The grade-A Sharpe of the CrossAssetValue edge is measured over the full
2005-2026 sample. But what if the edge only lived in one regime (e.g. the 2009-2014
mean-reversion bonanza)? A regime-dependent edge could be in a dead zone right now.

THE METHOD (correct for a LONG-lookback strategy). The value signal needs ~5y (1,513 bars)
of warmup before it trades, so you CANNOT just backtest 4-year slices of the data — every
slice would be warmup-starved and trade nothing (a trap an earlier draft of this tool fell
into). Instead: run ONE full backtest with full warmup, then slice the resulting daily
EQUITY CURVE by calendar year and compute each year's Sharpe. That isolates per-regime
performance from the actually-traded curve. We do it at 1x and 2x cost. Years before the
first trade are flat (no variance) and are flagged WARMUP, not counted against the edge.

VERDICT (over ACTIVE years only — those where the book actually moved):
  "consistent"        — no active year is materially negative (the edge never flipped)
  "regime-dependent"  — one or more active years are materially negative

Determinism: no RNG, no datetime.now() in logic.

Run:  python -m scripts.temporal_robustness
"""

from __future__ import annotations

import argparse
import logging
import sys
from decimal import Decimal

from apex.backtest.backtester import run_backtest
from apex.core.models import AssetClass, Symbol
from apex.data.historical_feed import HistoricalDataFeed
from apex.validation import metrics
from scripts.survivorship_stress import _UNIVERSE, _risk, _strategy

_DATA = "data/real/single_names.csv"
_BENCHMARK = "SPY"
_SLIPPAGE = Decimal("0.001")
# A year is "materially negative" only below this Sharpe — tiny wobbles in a thin
# transition year shouldn't read as a regime failure.
_NEG_TOL = -0.25


# ----------------------------------------------------------------- slicing (pure)


def slice_curve_by_year(equity: list[float], timestamps: list) -> list[tuple[int, list[float]]]:
    """
    Group a daily equity curve into per-calendar-year segments.

    Pure: returns [(year, [equity...]), ...] ordered by year. `equity` and `timestamps`
    are parallel lists (as produced by a backtest result). Mismatched lengths are zipped
    to the shorter.
    """
    buckets: dict[int, list[float]] = {}
    for e, ts in zip(equity, timestamps):
        buckets.setdefault(ts.year, []).append(e)
    return [(y, buckets[y]) for y in sorted(buckets)]


def year_sharpe(segment: list[float]) -> float:
    """Annualized Sharpe of one year's equity segment (0.0 if < 2 points)."""
    rets = metrics.returns_from_equity(segment)
    return metrics.sharpe_ratio(rets) if len(rets) >= 2 else 0.0


def is_active(segment: list[float]) -> bool:
    """A year is 'active' (post-warmup, actually trading) if its equity moved at all."""
    return len(segment) >= 2 and max(segment) != min(segment)


# ----------------------------------------------------------------- report


def _per_year(equity: list[float], timestamps: list, slippage: Decimal) -> dict:
    """{year: sharpe} for one backtest's curve (used for both 1x and 2x)."""
    return {y: year_sharpe(seg) for y, seg in slice_curve_by_year(equity, timestamps)}


def _verdict(year_rows: list[dict]) -> str:
    """Consistency over ACTIVE years using the 2x-cost Sharpe."""
    active = [r for r in year_rows if r["active"]]
    if not active:
        return "VERDICT: no active years — strategy never traded (check warmup/data)."
    negatives = [r for r in active if r["sharpe_2x"] < _NEG_TOL]
    n = len(active)
    pos = sum(1 for r in active if r["sharpe_2x"] > 0)
    if not negatives:
        return (
            f"VERDICT: consistent — across {n} active years none is materially negative "
            f"(Sharpe@2x < {_NEG_TOL}); {pos}/{n} are outright positive. Not one-regime."
        )
    labels = ", ".join(str(r["year"]) for r in negatives)
    return (
        f"VERDICT: regime-dependent — {len(negatives)}/{n} active years are materially "
        f"negative at 2x cost ({labels}). The edge is not uniform across regimes."
    )


def _utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def main() -> int:
    _utf8()
    logging.disable(logging.WARNING)
    ap = argparse.ArgumentParser(
        description="Temporal-robustness (per-calendar-year) of the single-name value edge."
    )
    ap.add_argument("--data", default=_DATA)
    args = ap.parse_args()

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
        f"Temporal robustness (per-year, single full backtest): {len(_UNIVERSE)} names, "
        f"{len(events)} bars.\nRunning the full backtest at 1x and 2x cost ...",
        flush=True,
    )
    res1 = run_backtest(events, _strategy(), _risk(), slippage_pct=_SLIPPAGE)
    res2 = run_backtest(events, _strategy(), _risk(), slippage_pct=_SLIPPAGE * Decimal("2"))
    full1 = metrics.sharpe_ratio(metrics.returns_from_equity(res1.equity_curve))
    full2 = metrics.sharpe_ratio(metrics.returns_from_equity(res2.equity_curve))
    print(f"  full-sample Sharpe@1x={full1:.2f}  Sharpe@2x={full2:.2f}  trades={res1.num_trades}\n")

    s1 = _per_year(res1.equity_curve, res1.equity_timestamps, _SLIPPAGE)
    s2 = _per_year(res2.equity_curve, res2.equity_timestamps, _SLIPPAGE * Decimal("2"))
    active_map = {
        y: is_active(seg)
        for y, seg in slice_curve_by_year(res1.equity_curve, res1.equity_timestamps)
    }
    rows = [
        {"year": y, "sharpe_1x": s1[y], "sharpe_2x": s2.get(y, 0.0), "active": active_map[y]}
        for y in sorted(s1)
    ]

    print(f"{'year':>6}  {'Sharpe@1x':>9}  {'Sharpe@2x':>9}  {'state':>7}")
    print("-" * 38)
    for r in rows:
        state = "active" if r["active"] else "warmup"
        print(
            f"{r['year']:>6}  {r['sharpe_1x']:>9.2f}  {r['sharpe_2x']:>9.2f}  {state:>7}",
            flush=True,
        )
    print(f"\n{_verdict(rows)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
