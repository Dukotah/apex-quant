"""
apex.backtest.allocator
=======================
Phase F3.3 — the multi-strategy capital-ALLOCATION engine.

Sessions 22-29 proved a SECOND, uncorrelated edge: the single-name VALUE sleeve runs at
corr +0.24 to the deployed TREND sleeve, and a 20% value / 80% trend blend lifts the book's
Sharpe 0.82 -> 0.99 with drawdown flat at 7% (DECISIONS S29). `scripts/allocate.py` proved
that *once* as an ad-hoc script; this module is the reusable, tested vehicle.

WHAT IT DOES. Each sleeve is backtested on its OWN universe at full capital; the resulting
daily-return streams are aligned on their common trading days and blended by a configured
CAPITAL WEIGHT. The blend is just the weighted sum of daily returns — a clean capital split,
no cross-sleeve interaction (the universes are disjoint: trend trades asset-class ETFs, value
trades single names). The engine reports the blended equity/Sharpe/Sortino/drawdown, each
sleeve's standalone Sharpe, and the pairwise return correlations — everything needed to see
whether the blend actually beats the best single sleeve.

LIVE GATING (rule: build the vehicle, don't fund it). A sleeve carries a `funded` flag.
`target_weights()` is the research/backtest split (what we'd run if every edge were proven);
`live_weights()` zeroes any UNFUNDED sleeve and renormalizes across the rest. The value sleeve
ships `funded=False` because its edge is still on a SURVIVOR universe — live value capital
stays blocked until survivorship-free validation (W8, DECISIONS S28/S30). So the live split is
100% trend today and flips to ~80/20 only by setting one flag, after W8 clears.

Pure and deterministic: the blend/align/metric helpers are side-effect-free; the only I/O is
the per-sleeve backtest, which is injectable (`run_backtest_fn`) so the engine unit-tests
offline with canned results. Money/allocation weights are Decimal at the config boundary;
the return-stream math is float, matching apex.validation.metrics.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Mapping, Sequence

from apex.core.events import MarketEvent
from apex.execution.engine import BacktestResult
from apex.risk.risk_manager import RiskConfig
from apex.strategy.base_strategy import BaseStrategy
from apex.validation import metrics

# The per-sleeve backtest signature the engine depends on (apex.backtest.run_backtest).
RunBacktestFn = Callable[..., BacktestResult]

# A tolerance for "weights sum to 1" — guards against Decimal/float rounding, not sloppiness.
_WEIGHT_SUM_TOL = Decimal("0.0001")


# --------------------------------------------------------------------- config


@dataclass(frozen=True)
class Sleeve:
    """One strategy sleeve in the allocation: a name, its capital weight, and a fund gate.

    `weight` is the TARGET (research) capital fraction. `funded` is the live gate: an
    unfunded sleeve is still backtested and measured but receives ZERO live capital
    (its target weight is redistributed to funded sleeves by `AllocationConfig.live_weights`).
    """

    name: str
    weight: Decimal
    funded: bool = True


@dataclass(frozen=True)
class AllocationConfig:
    """An immutable capital split across sleeves. Validates fail-closed at construction.

    The target weights must be a genuine allocation: every weight in [0, 1] and the set
    summing to 1 (within rounding tolerance). A malformed split raises rather than silently
    trading a book that doesn't add up — uncertainty defaults to "no", never "trade".
    """

    sleeves: tuple[Sleeve, ...]

    def __post_init__(self) -> None:
        if not self.sleeves:
            raise ValueError("AllocationConfig needs at least one sleeve.")
        names = [s.name for s in self.sleeves]
        if len(set(names)) != len(names):
            raise ValueError(f"Sleeve names must be unique, got {names}.")
        for s in self.sleeves:
            if not (Decimal("0") <= s.weight <= Decimal("1")):
                raise ValueError(f"Sleeve {s.name!r} weight {s.weight} outside [0, 1].")
        total = sum((s.weight for s in self.sleeves), Decimal("0"))
        if abs(total - Decimal("1")) > _WEIGHT_SUM_TOL:
            raise ValueError(f"Sleeve weights must sum to 1, got {total}.")

    def names(self) -> list[str]:
        return [s.name for s in self.sleeves]

    def target_weights(self) -> dict[str, float]:
        """The configured research split (every sleeve, funded or not)."""
        return {s.name: float(s.weight) for s in self.sleeves}

    def live_weights(self) -> dict[str, float]:
        """The deployable split: unfunded sleeves get 0, the rest renormalized to sum 1.

        If NO sleeve is funded, returns all-zero (nothing trades) — fail closed.
        """
        funded_total = sum((s.weight for s in self.sleeves if s.funded), Decimal("0"))
        if funded_total == 0:
            return {s.name: 0.0 for s in self.sleeves}
        return {s.name: float(s.weight / funded_total) if s.funded else 0.0 for s in self.sleeves}


# --------------------------------------------------------------------- sleeve spec / results


@dataclass(frozen=True)
class SleeveSpec:
    """Everything needed to backtest one sleeve at full capital.

    `name` must match a Sleeve in the AllocationConfig — that's how a measured return
    stream is paired with its capital weight.
    """

    name: str
    events: Sequence[MarketEvent]
    strategy: BaseStrategy
    risk_config: RiskConfig


@dataclass(frozen=True)
class SleeveResult:
    """One sleeve's standalone outcome over the common (aligned) window."""

    name: str
    weight: float
    standalone_sharpe: float
    standalone_max_drawdown: float


@dataclass(frozen=True)
class AllocationResult:
    """The blended book plus the per-sleeve and correlation detail behind it."""

    dates: list[date] = field(default_factory=list)
    blended_returns: list[float] = field(default_factory=list)
    blended_equity: list[float] = field(default_factory=list)
    blended_sharpe: float = 0.0
    blended_sortino: float = 0.0
    blended_max_drawdown: float = 0.0
    sleeves: tuple[SleeveResult, ...] = ()
    correlations: dict[tuple[str, str], float] = field(default_factory=dict)

    @property
    def best_standalone_sharpe(self) -> float:
        return max((s.standalone_sharpe for s in self.sleeves), default=0.0)

    @property
    def lift(self) -> float:
        """How much the blend beats the best single sleeve on Sharpe (the whole point)."""
        return self.blended_sharpe - self.best_standalone_sharpe

    def summary(self) -> str:
        parts = ", ".join(
            f"{s.name} {s.weight:.0%} (Sharpe {s.standalone_sharpe:.2f})" for s in self.sleeves
        )
        corr = ", ".join(f"corr({a},{b})={c:+.2f}" for (a, b), c in self.correlations.items())
        return (
            f"Allocation over {len(self.dates)} common days: {parts}. "
            f"Blended Sharpe {self.blended_sharpe:.2f} (lift {self.lift:+.2f}), "
            f"maxDD {self.blended_max_drawdown:.0%}. {corr}".rstrip()
        )


# --------------------------------------------------------------------- pure helpers


def returns_by_date(equity: Sequence[float], timestamps: Sequence[datetime]) -> dict[date, float]:
    """Map each date to that day's return. ``returns[i]`` pairs with ``timestamps[i + 1]``.

    Mirrors scripts/allocate.py so the engine reproduces the F3.2 measurement exactly.
    """
    rets = metrics.returns_from_equity(equity)
    out: dict[date, float] = {}
    for i, r in enumerate(rets):
        out[timestamps[i + 1].date()] = r
    return out


def align_streams(
    streams: Mapping[str, Mapping[date, float]],
) -> tuple[list[date], dict[str, list[float]]]:
    """Intersect all sleeves on their COMMON trading days; return sorted dates + aligned lists.

    A blend is only meaningful where every sleeve has a return, so we take the intersection.
    Empty if the sleeves never overlap.
    """
    if not streams:
        return [], {}
    common: set[date] | None = None
    for s in streams.values():
        keys = set(s.keys())
        common = keys if common is None else (common & keys)
    dates = sorted(common or set())
    aligned = {name: [s[d] for d in dates] for name, s in streams.items()}
    return dates, aligned


def blend(aligned: Mapping[str, Sequence[float]], weights: Mapping[str, float]) -> list[float]:
    """Weighted daily return across sleeves: ``sum_s weight[s] * return_s[t]`` for each day t.

    Every sleeve in `aligned` must have a weight. All sleeves must already share length
    (use ``align_streams`` first).
    """
    if not aligned:
        return []
    length = len(next(iter(aligned.values())))
    out = [0.0] * length
    for name, series in aligned.items():
        w = weights[name]
        for t, r in enumerate(series):
            out[t] += w * r
    return out


def equity_from_returns(returns: Sequence[float], start: float = 1.0) -> list[float]:
    """Rebuild a normalized equity curve from a daily-return series."""
    eq = [start]
    for r in returns:
        eq.append(eq[-1] * (1.0 + r))
    return eq


def _pairwise_correlations(aligned: Mapping[str, Sequence[float]]) -> dict[tuple[str, str], float]:
    """Pearson correlation for every unordered pair of sleeves (stable name order)."""
    names = list(aligned.keys())
    out: dict[tuple[str, str], float] = {}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            out[(a, b)] = metrics.correlation(aligned[a], aligned[b])
    return out


def inverse_vol_weights(aligned: Mapping[str, Sequence[float]]) -> dict[str, float]:
    """Cross-sleeve inverse-volatility weights: each sleeve weighted ∝ 1 / realized_vol.

    Realized vol is the population standard deviation of the sleeve's return series.
    A sleeve with zero (or near-zero) vol falls back to an equal share of the total
    inverse-vol budget, so the function never crashes and always sums to 1.

    This is the DeMiguel 1/N / Garleanu-Pedersen cross-sleeve risk-parity calibration:
    calmer sleeves receive a larger capital share, reducing the portfolio's overall
    realized volatility without requiring a mean estimate. Deterministic.

    Args:
        aligned: ``{sleeve_name: [daily_return, ...]}`` — all series already share length
            (use ``align_streams`` first).

    Returns:
        ``{sleeve_name: weight}`` summing to 1.0 (or an empty dict if ``aligned`` is empty).
    """
    if not aligned:
        return {}

    names = list(aligned.keys())
    vols: dict[str, float] = {}
    for name, series in aligned.items():
        if len(series) < 2:
            vols[name] = 0.0
        else:
            vols[name] = statistics.pstdev(series)

    # Inverse-vol for each sleeve; zero-vol sleeves fall back to 1.0 (equal share).
    inv_vols: dict[str, float] = {name: (1.0 / v if v > 0.0 else 1.0) for name, v in vols.items()}
    total_inv = sum(inv_vols.values())
    if total_inv == 0.0:
        # Degenerate: all vols are zero and the fallback sum is also zero (impossible
        # given the fallback of 1.0, but guard defensively).
        equal = 1.0 / len(names)
        return {name: equal for name in names}

    return {name: inv_vols[name] / total_inv for name in names}


def tolerance_band_rebalance(
    current: Mapping[str, float],
    target: Mapping[str, float],
    bands: Mapping[str, float],
) -> dict[str, float]:
    """Trade-to-edge rebalancing with per-sleeve no-trade tolerance bands.

    For each sleeve:
    - If ``|current[s] - target[s]| <= bands[s]``: KEEP current weight (no trade).
    - Otherwise: move to the near EDGE of the band — i.e. ``target[s] ± bands[s]``
      toward the current weight (the minimum trade that puts the sleeve inside the band).

    The result is renormalized to sum to 1.0 so the book stays fully invested.
    This implements the Garleanu-Pedersen insight: slower/costlier-to-trade sleeves
    should get wider bands (passed in via ``bands`` by the caller).

    Args:
        current: ``{sleeve_name: current_weight}`` — the live allocation fractions.
        target:  ``{sleeve_name: target_weight}``  — the desired long-run allocation.
        bands:   ``{sleeve_name: half_bandwidth}``  — the ± tolerance for each sleeve.
            All three mappings must share the same key set; raises ``ValueError`` otherwise.

    Returns:
        ``{sleeve_name: new_weight}`` summing to 1.0. Deterministic.

    Raises:
        ``ValueError`` if the key sets of the three mappings are inconsistent.
    """
    cur_keys = set(current)
    tgt_keys = set(target)
    band_keys = set(bands)
    if cur_keys != tgt_keys or cur_keys != band_keys:
        raise ValueError(
            f"Key mismatch: current={sorted(cur_keys)}, "
            f"target={sorted(tgt_keys)}, bands={sorted(band_keys)}."
        )
    if not cur_keys:
        return {}

    proposed: dict[str, float] = {}
    for name in current:
        c = current[name]
        t = target[name]
        b = bands[name]
        diff = c - t
        if abs(diff) <= b:
            # Inside (or on) the band — keep exactly, no trade.
            proposed[name] = c
        else:
            # Outside: snap to the near edge of the band (sign of diff tells direction).
            proposed[name] = t + b * (1.0 if diff > 0.0 else -1.0)

    # Renormalize so the book stays fully invested.
    total = sum(proposed.values())
    if total == 0.0:
        equal = 1.0 / len(proposed)
        return {name: equal for name in proposed}
    return {name: w / total for name, w in proposed.items()}


# --------------------------------------------------------------------- the engine


_WEIGHTING_MODES = frozenset({"config", "inverse_vol"})


def run_allocation_backtest(
    specs: Sequence[SleeveSpec],
    config: AllocationConfig,
    *,
    slippage_pct: Decimal = Decimal("0.001"),
    weights: Mapping[str, float] | None = None,
    weighting: str = "config",
    run_backtest_fn: RunBacktestFn | None = None,
) -> AllocationResult:
    """Backtest each sleeve at full capital, align on common days, and blend by capital weight.

    Args:
        specs: one per sleeve; each ``name`` must appear in ``config``.
        config: the capital split (validated at construction).
        slippage_pct: per-trade slippage passed to each sleeve backtest.
        weights: the split to blend with; defaults to ``config.target_weights()`` (the research
            split). Pass ``config.live_weights()`` to model the deployable, W8-gated split.
            Ignored when ``weighting="inverse_vol"``.
        weighting: blend-weight source — one of:
            - ``"config"`` (default): use ``weights`` or ``config.target_weights()``.
              Byte-identical to the previous behaviour.
            - ``"inverse_vol"``: derive weights from the aligned return streams via
              :func:`inverse_vol_weights` (calmer sleeves receive more capital). The
              ``weights`` argument is ignored in this mode.
        run_backtest_fn: the per-sleeve backtester; defaults to ``apex.backtest.run_backtest``
            (injectable so the engine unit-tests offline with canned results).

    Returns an :class:`AllocationResult`. Fails closed: a spec whose name isn't in the config,
    or a config sleeve with no spec, raises rather than silently dropping capital.

    Raises:
        ``ValueError`` for an unknown ``weighting`` string.
    """
    if weighting not in _WEIGHTING_MODES:
        raise ValueError(
            f"Unknown weighting {weighting!r}. Valid options: {sorted(_WEIGHTING_MODES)}."
        )
    spec_names = [s.name for s in specs]
    if len(set(spec_names)) != len(spec_names):
        raise ValueError(f"Duplicate sleeve specs: {spec_names}.")
    if set(spec_names) != set(config.names()):
        raise ValueError(
            f"Specs {sorted(spec_names)} must match config sleeves {sorted(config.names())}."
        )

    if run_backtest_fn is None:
        from apex.backtest.backtester import run_backtest

        run_backtest_fn = run_backtest

    # 1. Backtest each sleeve at full capital; collect its daily-return-by-date stream.
    streams: dict[str, dict[date, float]] = {}
    for spec in specs:
        result = run_backtest_fn(
            list(spec.events), spec.strategy, spec.risk_config, slippage_pct=slippage_pct
        )
        streams[spec.name] = returns_by_date(result.equity_curve, result.equity_timestamps)

    # 2. Align on the common window.
    dates, aligned = align_streams(streams)

    # 3. Resolve the blend weights based on the chosen weighting mode.
    if weighting == "inverse_vol":
        use_weights: dict[str, float] = inverse_vol_weights(aligned)
    else:
        use_weights = dict(weights) if weights is not None else config.target_weights()

    blended = blend(aligned, use_weights) if aligned else []
    blended_equity = equity_from_returns(blended)

    # 4. Per-sleeve standalone metrics over the SAME common window (apples to apples).
    weight_by_name = {s.name: s.weight for s in config.sleeves}
    sleeve_results = tuple(
        SleeveResult(
            name=name,
            weight=use_weights.get(name, 0.0),
            standalone_sharpe=metrics.sharpe_ratio(series),
            standalone_max_drawdown=metrics.max_drawdown(equity_from_returns(series)),
        )
        for name, series in aligned.items()
    )
    # `weight_by_name` kept for callers; not used directly above so the shown weight reflects
    # the split actually blended with (target vs live), not just the config target.
    _ = weight_by_name

    return AllocationResult(
        dates=dates,
        blended_returns=blended,
        blended_equity=blended_equity,
        blended_sharpe=metrics.sharpe_ratio(blended),
        blended_sortino=metrics.sortino_ratio(blended),
        blended_max_drawdown=metrics.max_drawdown(blended_equity),
        sleeves=sleeve_results,
        correlations=_pairwise_correlations(aligned),
    )
