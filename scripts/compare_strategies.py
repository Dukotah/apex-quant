"""
scripts/compare_strategies.py
=============================
Rank library strategies against each other by a chosen metric, read from stored
backtest results. Read-only and offline: it never touches the broker, never
fetches data, and never re-runs a backtest — it only reads results you already
saved and turns them into a leaderboard.

Each stored result is just a strategy name mapped to its backtest equity curve
(oldest -> newest). The chosen metric (Sharpe, Sortino, total return, max
drawdown, Calmar, annualized return) is computed from that curve with the same
``apex.validation.metrics`` functions the Gauntlet uses, then strategies are
ranked best-first.

Usage:
    python -m scripts.compare_strategies results.json
    python -m scripts.compare_strategies results.json --metric sharpe
    python -m scripts.compare_strategies results.json --metric max_drawdown --top 5

The results file is JSON: {"<strategy>": [equity, equity, ...], ...}.

Importing this module has ZERO side effects. The metric computation is a pure,
deterministic core (``rank_strategies`` / ``compare_strategies``) that does no
I/O and takes any "now" as an injected argument — tested in
tests/test_compare_strategies.py.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Mapping, Optional, Sequence

# --------------------------------------------------------------------- metrics

# Each metric maps a name -> (callable over an equity curve, higher-is-better).
# We build the registry lazily (inside a function) so importing this module pulls
# in nothing and has no side effects.

MetricFn = Callable[[Sequence[float]], float]


def _metric_registry() -> Dict[str, "MetricSpec"]:
    """Build the supported-metric registry. Lazy-imports apex.validation.metrics."""
    from apex.validation import metrics

    def _sharpe(curve: Sequence[float]) -> float:
        return metrics.sharpe_ratio(metrics.returns_from_equity(curve))

    def _sortino(curve: Sequence[float]) -> float:
        return metrics.sortino_ratio(metrics.returns_from_equity(curve))

    return {
        "sharpe": MetricSpec("sharpe", _sharpe, higher_is_better=True),
        "sortino": MetricSpec("sortino", _sortino, higher_is_better=True),
        "total_return": MetricSpec("total_return", metrics.total_return, higher_is_better=True),
        "annualized_return": MetricSpec(
            "annualized_return", metrics.annualized_return, higher_is_better=True
        ),
        "calmar": MetricSpec("calmar", metrics.calmar_ratio, higher_is_better=True),
        "max_drawdown": MetricSpec("max_drawdown", metrics.max_drawdown, higher_is_better=False),
    }


def supported_metrics() -> List[str]:
    """The metric names accepted by --metric, stable order."""
    return list(_metric_registry().keys())


@dataclass(frozen=True)
class MetricSpec:
    """A named metric: how to compute it from an equity curve, and its ranking sense."""
    name: str
    fn: MetricFn
    higher_is_better: bool


@dataclass(frozen=True)
class StrategyScore:
    """One strategy's computed metric and how much usable data backed it."""
    name: str
    metric: str
    value: Optional[float]   # None when the curve is too short to score
    points: int

    @property
    def scored(self) -> bool:
        return self.value is not None


# ------------------------------------------------------------------- pure core

def rank_strategies(
    results: Mapping[str, Sequence[float]],
    metric: str = "sharpe",
) -> List[StrategyScore]:
    """
    PURE CORE. Score every strategy's equity curve by ``metric`` and return them
    ranked best-first. No I/O, no clock, fully deterministic.

    - Curves with fewer than 2 points cannot be scored -> value is None; those
      strategies sort last (fail closed — never rank an unscorable curve highly).
    - Ties (and equal-value strategies) break alphabetically by name for a stable,
      reproducible order.
    - ``metric`` must be one of ``supported_metrics()``; otherwise ValueError.
    """
    registry = _metric_registry()
    if metric not in registry:
        raise ValueError(
            f"unknown metric {metric!r}; choose one of {', '.join(registry)}"
        )
    spec = registry[metric]

    scores: List[StrategyScore] = []
    for name in results:
        curve = list(results[name])
        value = spec.fn(curve) if len(curve) >= 2 else None
        scores.append(StrategyScore(name=name, metric=metric, value=value, points=len(curve)))

    # Sort: scored strategies first, then by metric value (respecting direction),
    # then alphabetically for deterministic tie-breaks.
    sign = -1.0 if spec.higher_is_better else 1.0

    def _key(s: StrategyScore):
        unscored = s.value is None
        ordered = sign * s.value if s.value is not None else 0.0
        return (unscored, ordered, s.name)

    scores.sort(key=_key)
    return scores


def compare_strategies(
    results: Mapping[str, Sequence[float]],
    metric: str = "sharpe",
    *,
    top: Optional[int] = None,
    generated_at: Optional[datetime] = None,
) -> str:
    """
    PURE CORE. Render the ranked leaderboard as text. ``generated_at`` is INJECTED
    (never datetime.now() here) so the output is fully deterministic and testable.
    """
    ranked = rank_strategies(results, metric)
    if top is not None and top > 0:
        ranked = ranked[:top]

    spec = _metric_registry()[metric]
    direction = "higher is better" if spec.higher_is_better else "lower is better"

    lines = [
        "APEX QUANT — STRATEGY COMPARISON",
        "=" * 56,
        f"  metric {metric} ({direction})   strategies {len(results)}",
    ]
    if generated_at is not None:
        lines.append(f"  generated {generated_at:%Y-%m-%d %H:%M}Z")
    lines.append("-" * 56)

    if not ranked:
        lines.append("  no strategy results to compare.")
        return "\n".join(lines)

    width = max(len(s.name) for s in ranked)
    for i, s in enumerate(ranked, start=1):
        if s.scored:
            shown = f"{s.value:>+12.4f}"
        else:
            shown = f"{'n/a':>12}"
        lines.append(f"  {i:>2}. {s.name:<{width}}  {shown}   ({s.points} pts)")
    return "\n".join(lines)


# ----------------------------------------------------------------- I/O wrappers
# (I/O lives only here and in main(), never in the pure core above.)

def load_results(path: str) -> Dict[str, List[float]]:
    """
    Read stored backtest results from a JSON file: {"<strategy>": [equity, ...]}.
    Raises ValueError on a malformed file. I/O wrapper only — kept out of the core.
    """
    import json

    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("results file must be a JSON object mapping strategy -> equity curve")

    out: Dict[str, List[float]] = {}
    for name, curve in raw.items():
        if not isinstance(curve, (list, tuple)):
            raise ValueError(f"equity curve for {name!r} must be a list of numbers")
        out[str(name)] = [float(x) for x in curve]
    return out


# ------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compare_strategies",
        description="Rank library strategies by a chosen metric from stored backtest results "
                    "(read-only; never trades or fetches data).",
    )
    parser.add_argument(
        "results",
        help="path to a JSON results file: {\"<strategy>\": [equity, equity, ...], ...}",
    )
    parser.add_argument(
        "--metric", "-m", default="sharpe", choices=supported_metrics(),
        help="metric to rank by (default: sharpe)",
    )
    parser.add_argument(
        "--top", "-t", type=int, default=None,
        help="show only the top N strategies (default: all)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    args = build_parser().parse_args(argv)
    try:
        results = load_results(args.results)
    except (OSError, ValueError) as exc:
        print(f"compare_strategies: could not read results: {exc}", file=sys.stderr)
        return 2

    print(compare_strategies(
        results, args.metric, top=args.top,
        generated_at=datetime.now(timezone.utc),   # I/O boundary only — never in the core
    ))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
