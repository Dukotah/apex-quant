"""
scripts/regime_report.py
========================
A standalone REGIME-SEGMENTED performance report. A strategy whose headline
Sharpe is grade-A but which makes ALL its money in one volatility regime (e.g. a
calm bull) and bleeds in another (e.g. a high-vol crash) is a regime bet, not an
edge. This tool surfaces that by splitting a return series into volatility
regimes and reporting the key metrics SEPARATELY within each one.

    python -m scripts.regime_report                       # default state DB, paper
    python -m scripts.regime_report --mode live           # a different mode
    python -m scripts.regime_report --returns r.json      # explicit return series
    python -m scripts.regime_report --vol-window 20 --lookback 60

Read-only by design: it never touches the broker, never sends over the network,
and importing the module has ZERO side effects (every state/config dependency is
lazily imported INSIDE the function that needs it).

The number-crunching lives in :func:`build_regime_report`, a PURE deterministic
function of a plain return series plus injected parameters (no wall clock, no
I/O). The CLI is a thin shell that only READS the equity curve out of SQLite,
converts it to returns, and hands it to that pure core.

Regime labelling reuses the EXISTING gate components rather than reinventing
volatility detection:
  - ``apex.strategy.regime.VolatilityRegimeClassifier`` assigns each period a
    LOW / NORMAL / HIGH / UNKNOWN volatility band by ranking the trailing
    realized vol against a longer lookback distribution.
  - ``apex.validation.regime_split_metrics.regime_split_metrics`` slices the
    returns by those labels and scores each bucket on its own equity curve.

Statistical code uses float to match the ``apex.validation.metrics`` layer it
builds on (Golden Rule: follow the layer's convention). Tested in
tests/test_regime_report.py.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Hashable, Optional, Sequence

if TYPE_CHECKING:
    from apex.strategy.regime import VolatilityRegime
    from apex.validation.regime_split_metrics import RegimeMetrics


# ------------------------------------------------------------------- pure core


def _equity_from_returns(returns: Sequence[float], start: float = 1.0) -> list[float]:
    """Compound per-period returns into an equity/price curve (len = len+1)."""
    equity = [start]
    for r in returns:
        equity.append(equity[-1] * (1.0 + float(r)))
    return equity


def label_returns_by_volatility(
    returns: Sequence[float],
    *,
    vol_window: int = 20,
    lookback: int = 60,
    low_pct: float = 0.25,
    high_pct: float = 0.75,
) -> list["VolatilityRegime"]:
    """
    Assign every return a volatility-regime label using the EXISTING
    ``VolatilityRegimeClassifier`` gate component (we reuse it, we don't reinvent
    vol detection).

    We reconstruct a synthetic price curve by compounding the returns, then walk
    it forward: the label for return ``i`` is the classifier's verdict on the
    prices observed UP TO AND INCLUDING the close that produced return ``i``.
    This is causal — a period's label never peeks at future data — and
    deterministic. Early periods without enough history classify as UNKNOWN.

    Returns a list of ``VolatilityRegime`` enum members, one per input return.
    """
    from apex.strategy.regime import VolatilityRegimeClassifier

    clf = VolatilityRegimeClassifier(
        vol_window=vol_window,
        lookback=lookback,
        low_pct=low_pct,
        high_pct=high_pct,
    )
    prices = _equity_from_returns(returns)
    labels: list["VolatilityRegime"] = []
    # return i is realized at prices[i + 1]; classify the prefix prices[: i + 2].
    for i in range(len(returns)):
        result = clf.classify(prices[: i + 2])
        labels.append(result.regime)
    return labels


@dataclass(frozen=True)
class RegimeReport:
    """The full regime-segmented result: per-regime metrics plus context."""

    n_returns: int
    n_classified: int  # returns that landed in a non-UNKNOWN regime
    per_regime: dict[Hashable, "RegimeMetrics"]

    def regimes(self) -> list[Hashable]:
        """Distinct regime labels present, in a stable display order."""
        return _ordered_regimes(self.per_regime.keys())


# Stable, human-meaningful ordering for the volatility bands (calm -> turbulent),
# with UNKNOWN last and any unexpected label appended alphabetically after.
_REGIME_ORDER = ["low", "normal", "high", "unknown"]


def _regime_key(label: Hashable) -> str:
    """Normalize a regime label to its string value for ordering/display."""
    value = getattr(label, "value", label)
    return str(value)


def _ordered_regimes(labels) -> list[Hashable]:
    """Order regime labels calm->turbulent (LOW, NORMAL, HIGH, UNKNOWN, other)."""

    def sort_key(label: Hashable) -> tuple[int, str]:
        key = _regime_key(label)
        if key in _REGIME_ORDER:
            return (_REGIME_ORDER.index(key), key)
        return (len(_REGIME_ORDER), key)

    return sorted(labels, key=sort_key)


def compute_regime_report(
    returns: Sequence[float],
    *,
    vol_window: int = 20,
    lookback: int = 60,
    low_pct: float = 0.25,
    high_pct: float = 0.75,
    periods_per_year: int = 252,
    risk_free_rate: float = 0.0,
) -> Optional[RegimeReport]:
    """
    Split ``returns`` by volatility regime and compute per-regime metrics.
    PURE and deterministic — same inputs always produce the same report.

    Returns None when there are too few returns to classify ANY period (fewer
    than the classifier's minimum history), rather than fabricating a report —
    fail closed (Golden Rule 6/10). A single all-UNKNOWN report is still useful,
    so we only bail when there are zero returns; otherwise we report what we can.
    """
    rets = [float(r) for r in returns]
    if not rets:
        return None

    from apex.validation.regime_split_metrics import regime_split_metrics

    labels = label_returns_by_volatility(
        rets,
        vol_window=vol_window,
        lookback=lookback,
        low_pct=low_pct,
        high_pct=high_pct,
    )
    per_regime = regime_split_metrics(
        rets,
        labels,
        periods_per_year=periods_per_year,
        risk_free_rate=risk_free_rate,
    )

    from apex.strategy.regime import VolatilityRegime

    n_classified = sum(1 for label in labels if label is not VolatilityRegime.UNKNOWN)
    return RegimeReport(
        n_returns=len(rets),
        n_classified=n_classified,
        per_regime=per_regime,
    )


def build_regime_report(
    returns: Sequence[float],
    *,
    label: str = "performance",
    vol_window: int = 20,
    lookback: int = 60,
    low_pct: float = 0.25,
    high_pct: float = 0.75,
    periods_per_year: int = 252,
    risk_free_rate: float = 0.0,
) -> str:
    """
    Render a plain-text regime-segmented report from a return series. PURE: no
    I/O, no wall-clock — given the same returns and parameters it always returns
    the same text.
    """
    header = f"APEX QUANT — REGIME-SEGMENTED REPORT ({label})"
    bar = "=" * 72

    report = compute_regime_report(
        returns,
        vol_window=vol_window,
        lookback=lookback,
        low_pct=low_pct,
        high_pct=high_pct,
        periods_per_year=periods_per_year,
        risk_free_rate=risk_free_rate,
    )
    if report is None:
        return f"{header}\n{bar}\n  not enough data — need at least 1 return."

    lines = [
        header,
        bar,
        f"  returns {report.n_returns}   classified {report.n_classified}"
        f"   (vol_window={vol_window}, lookback={lookback})",
        "-" * 72,
        f"  {'regime':<8} {'n':>5} {'share':>7} {'Sharpe':>8} "
        f"{'total':>9} {'annual':>9} {'maxDD':>8}",
        "-" * 72,
    ]

    for regime in report.regimes():
        m = report.per_regime[regime]
        lines.append(
            f"  {_regime_key(regime):<8} {m.n_periods:>5} {m.fraction:>7.1%} "
            f"{m.sharpe_ratio:>+8.2f} {m.total_return:>+9.1%} "
            f"{m.annualized_return:>+9.1%} {m.max_drawdown:>8.1%}"
        )

    return "\n".join(lines)


# ----------------------------------------------------------------- I/O wrappers
# (I/O lives only here and in main(), never in the pure core above.)


def _equities_to_returns(equities: Sequence[float]) -> list[float]:
    """Convert an equity curve (oldest -> newest) into period returns."""
    from apex.validation import metrics

    return metrics.returns_from_equity([float(e) for e in equities])


def _load_returns_from_db(db_path: str, mode: str) -> list[float]:  # pragma: no cover
    """
    Read the equity curve for ``mode`` out of the run_once state DB and convert
    it to per-period returns. State store is imported lazily so importing this
    module has no side effects.
    """
    from scripts.run_once import StateStore

    store = StateStore(db_path)
    try:
        rows = store.history(mode)
        equities = [float(r["equity"]) for r in rows]
    finally:
        store.close()
    return _equities_to_returns(equities)


def _load_returns_from_json(path: str) -> list[float]:  # pragma: no cover
    """
    Read an explicit return series from a JSON file: a flat list of numbers
    (each a per-period return as a fraction, 0.01 = +1%).
    """
    import json

    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, (list, tuple)):
        raise ValueError("returns file must be a JSON array of numbers")
    return [float(x) for x in raw]


# ------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:  # pragma: no cover
    parser = argparse.ArgumentParser(
        prog="regime_report",
        description="Print a regime-segmented performance report (per-regime Sharpe, "
        "return, drawdown) from the run_once state DB or an explicit return series. "
        "Read-only; never trades or fetches data.",
    )
    parser.add_argument(
        "--db",
        default="state/apex_state.db",
        help="Path to the run_once SQLite state DB (default: state/apex_state.db).",
    )
    parser.add_argument(
        "--mode",
        default="paper",
        help="Which run mode's history to report on (default: paper).",
    )
    parser.add_argument(
        "--returns",
        default=None,
        help="Path to a JSON array of per-period returns; overrides --db/--mode.",
    )
    parser.add_argument(
        "--vol-window",
        type=int,
        default=20,
        help="Realized-vol window for the regime classifier (default: 20).",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=60,
        help="Lookback distribution length for the classifier (default: 60).",
    )
    parser.add_argument(
        "--periods",
        type=int,
        default=252,
        help="Periods per year for annualization (default: 252 trading days).",
    )
    parser.add_argument(
        "--risk-free",
        type=float,
        default=0.0,
        help="Annual risk-free rate used by Sharpe/Sortino (default: 0.0).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover
    try:
        import sys

        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    args = build_parser().parse_args(argv)

    if args.returns is not None:
        try:
            returns = _load_returns_from_json(args.returns)
        except (OSError, ValueError) as exc:
            print(f"regime_report: could not read returns: {exc}", file=sys.stderr)
            return 2
        report_label = args.returns
    else:
        returns = _load_returns_from_db(args.db, args.mode)
        report_label = args.mode

    if not returns:
        print(f"No returns to report (source: {report_label}) — nothing to do.")
        return 0

    print(
        build_regime_report(
            returns,
            label=report_label,
            vol_window=args.vol_window,
            lookback=args.lookback,
            periods_per_year=args.periods,
            risk_free_rate=args.risk_free,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
