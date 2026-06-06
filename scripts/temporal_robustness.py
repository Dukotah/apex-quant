"""
scripts/temporal_robustness.py
==============================
Phase F1.2 — temporal robustness of the single-name value edge (DECISIONS Session 27).

THE QUESTION. The grade-A Sharpe of the CrossAssetValue edge is measured over the full
2005-2026 sample. But what if the edge only lived in one regime (e.g. the mean-reversion
bonanza of 2009-2014)? A regime-dependent edge is not worth deploying: it could be in a
dead zone right now.

THE TEST (no look-ahead, no re-fitting). Slice the event stream into:
  (a) CONSECUTIVE windows  — e.g. five roughly equal sub-periods.
  (b) EXPANDING windows    — 4-yr seed, then add 4 yr at a time (anchored start).

Run the SAME CrossAssetValueStrategy and _risk()/_full_sharpe() from
survivorship_stress.py on each slice, and report per-period full Sharpe + Sharpe@2x.

VERDICT:
  "consistent"        — all sub-period Sharpe@2x > 0 (the edge has never flip-negative)
  "regime-dependent"  — at least one sub-period Sharpe@2x <= 0

The operator can see both the rolling stability and the full-period baseline side by side.

Determinism is guaranteed: no RNG, no datetime.now() in logic.

Run:  python -m scripts.temporal_robustness
      python -m scripts.temporal_robustness --periods 5 --data data/real/single_names.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from decimal import Decimal
from typing import Sequence

from apex.core.events import MarketEvent
from apex.core.models import AssetClass, Symbol
from apex.data.historical_feed import HistoricalDataFeed
from scripts.survivorship_stress import (
    _UNIVERSE,
    _full_sharpe,
)

_DATA = "data/real/single_names.csv"
_BENCHMARK = "SPY"
_SLIPPAGE = Decimal("0.001")


# ----------------------------------------------------------------- period slicing (pure)


def slice_into_periods(
    events: Sequence[MarketEvent],
    n_periods: int,
) -> list[list[MarketEvent]]:
    """
    Split `events` into `n_periods` consecutive chunks of roughly equal size.

    Pure function: given the same events and n_periods returns the same slices.
    Empty events or n_periods <= 0 returns [].  n_periods > len(events) returns
    as many single-event slices as there are events.
    """
    events = list(events)
    if not events or n_periods <= 0:
        return []
    n_periods = min(n_periods, len(events))
    total = len(events)
    # Distribute remainder events into the first (total % n_periods) slices.
    base, rem = divmod(total, n_periods)
    slices: list[list[MarketEvent]] = []
    start = 0
    for i in range(n_periods):
        size = base + (1 if i < rem else 0)
        slices.append(events[start : start + size])
        start += size
    return slices


def slice_expanding(
    events: Sequence[MarketEvent],
    n_periods: int,
) -> list[list[MarketEvent]]:
    """
    Build `n_periods` expanding-window slices, all anchored at the first event.

    Window i covers events[0 : (i+1)*step], where step = total // n_periods.
    The final window is always the full event list.

    Pure function: deterministic given the same inputs.
    """
    events = list(events)
    if not events or n_periods <= 0:
        return []
    n_periods = min(n_periods, len(events))
    total = len(events)
    step = max(1, total // n_periods)
    slices: list[list[MarketEvent]] = []
    for i in range(1, n_periods + 1):
        end = min(i * step, total)
        slices.append(events[:end])
        if end >= total:
            break
    # Ensure the last slice is always the full stream.
    if slices and len(slices[-1]) < total:
        slices.append(events)
    return slices


# ----------------------------------------------------------------- label helpers (pure)


def _date_range_label(period_events: list[MarketEvent]) -> str:
    """Return 'YYYY-MM-DD..YYYY-MM-DD' label for a slice, or '-' if empty."""
    if not period_events:
        return "-"
    first = period_events[0].bar.timestamp.date().isoformat()
    last = period_events[-1].bar.timestamp.date().isoformat()
    return f"{first}..{last}"


def _period_years(period_events: list[MarketEvent]) -> float:
    """Return approximate length of a period in years, or 0 if < 2 events."""
    if len(period_events) < 2:
        return 0.0
    delta = period_events[-1].bar.timestamp - period_events[0].bar.timestamp
    return delta.days / 365.25


# ----------------------------------------------------------------- report


def _run_period(
    period_events: list[MarketEvent],
    label: str,
    window_type: str,
    idx: int,
) -> dict:
    """Run the value edge on one period and return a result dict."""
    sharpe_1x, n_trades = _full_sharpe(period_events, _SLIPPAGE)
    sharpe_2x, _ = _full_sharpe(period_events, _SLIPPAGE * Decimal("2"))
    years = _period_years(period_events)
    return {
        "window_type": window_type,
        "period": idx + 1,
        "label": label,
        "years": years,
        "n_bars": len(period_events),
        "n_trades": n_trades,
        "sharpe_1x": sharpe_1x,
        "sharpe_2x": sharpe_2x,
    }


def _print_table(rows: list[dict], title: str) -> None:
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print(f"{'=' * 72}")
    print(
        f"{'#':>3}  {'Period':^25}  {'yrs':>4}  {'trades':>6}  "
        f"{'Sharpe@1x':>9}  {'Sharpe@2x':>9}  {'pass':>5}"
    )
    print("-" * 72)
    for r in rows:
        passed = "YES" if r["sharpe_2x"] > 0 else "no"
        print(
            f"{r['period']:>3}  {r['label']:^25}  {r['years']:>4.1f}  {r['n_trades']:>6}  "
            f"{r['sharpe_1x']:>9.2f}  {r['sharpe_2x']:>9.2f}  {passed:>5}",
            flush=True,
        )


def _verdict(rows_consec: list[dict], rows_expand: list[dict]) -> str:
    """
    Emit a one-line verdict based on consecutive sub-period Sharpe@2x.
    All > 0 → consistent.  Any <= 0 → regime-dependent.
    """
    if not rows_consec:
        return "VERDICT: no sub-period data — cannot assess temporal robustness."

    failing = [r for r in rows_consec if r["sharpe_2x"] <= 0]
    min_sharpe2 = min(r["sharpe_2x"] for r in rows_consec)
    n = len(rows_consec)

    if not failing:
        return (
            f"VERDICT: consistent — all {n} sub-periods have Sharpe@2x > 0 "
            f"(min {min_sharpe2:.2f}). The value edge is not one-regime."
        )
    else:
        pct = len(failing) / n
        labels = ", ".join(r["label"] for r in failing)
        return (
            f"VERDICT: regime-dependent — {len(failing)}/{n} sub-periods ({pct:.0%}) "
            f"have Sharpe@2x <= 0. Weak periods: {labels}."
        )


def _utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


# ----------------------------------------------------------------- main


def main() -> int:
    _utf8()
    logging.disable(logging.WARNING)

    ap = argparse.ArgumentParser(
        description="Temporal-robustness analysis of the single-name value edge."
    )
    ap.add_argument(
        "--periods",
        type=int,
        default=5,
        help="Number of consecutive sub-periods (default: 5 gives ~4-yr windows over 2005-2026)",
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

    n_periods = args.periods
    print(
        f"Temporal robustness: {len(_UNIVERSE)} names, {len(events)} total bars, "
        f"{n_periods} sub-periods.\n"
        f"Edge baseline (full sample):",
        flush=True,
    )
    full_1x, full_trades = _full_sharpe(events, _SLIPPAGE)
    full_2x, _ = _full_sharpe(events, _SLIPPAGE * Decimal("2"))
    print(
        f"  Sharpe@1x = {full_1x:.2f}  Sharpe@2x = {full_2x:.2f}  trades = {full_trades}",
        flush=True,
    )

    # ---- consecutive sub-periods ----
    consec_slices = slice_into_periods(events, n_periods)
    rows_consec: list[dict] = []
    for i, sl in enumerate(consec_slices):
        label = _date_range_label(sl)
        print(f"  Running consecutive period {i + 1}/{n_periods}: {label} ...", flush=True)
        rows_consec.append(_run_period(sl, label, "consecutive", i))

    _print_table(rows_consec, f"CONSECUTIVE SUB-PERIODS  (n={n_periods})")

    # ---- expanding windows ----
    expand_slices = slice_expanding(events, n_periods)
    rows_expand: list[dict] = []
    for i, sl in enumerate(expand_slices):
        label = _date_range_label(sl)
        print(f"  Running expanding window {i + 1}/{len(expand_slices)}: {label} ...", flush=True)
        rows_expand.append(_run_period(sl, label, "expanding", i))

    _print_table(rows_expand, f"EXPANDING WINDOWS  (n={len(expand_slices)})")

    print(f"\n{_verdict(rows_consec, rows_expand)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
