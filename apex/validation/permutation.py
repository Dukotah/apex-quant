"""
apex.validation.permutation
===========================
Monte-Carlo Permutation Test (MCPT) — the price-path-shuffle overfitting gate.

Reference: Bailey et al. 2014, "The Probability of Backtest Overfitting."

Unlike the existing Gate-4 trade bootstrap (monte_carlo.py), which resamples
*realized trades*, this test shuffles the underlying PRICE PATH, re-runs the
whole strategy on each shuffled history, and asks:

    Would the strategy's logic have found a Sharpe this high even with no
    real market structure?

    p = fraction of shuffled runs whose Sharpe >= the real Sharpe.

A low p-value (< significance) means the result is unlikely to come from a
strategy that merely picks up on noise in the price path.  A high p-value
means the Sharpe could plausibly be replicated on a random walk — fail.

Deterministic given a seed (Golden Rule 10).  Pure stdlib.  No I/O.
Fail closed: if the input is too thin to shuffle meaningfully, return
passed=False with p_value=1.0.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Dict, List, Optional

from apex.backtest.backtester import run_backtest as _default_run_backtest
from apex.core.events import MarketEvent
from apex.core.models import Bar
from apex.risk.risk_manager import RiskConfig
from apex.strategy.base_strategy import BaseStrategy
from apex.validation import metrics

# Minimum number of bars per ticker required for the shuffle to be meaningful.
_MIN_BARS_PER_TICKER = 30


@dataclass(frozen=True)
class PermutationResult:
    """Outcome of the Monte-Carlo permutation test (price-path shuffle)."""

    real_sharpe: float
    p_value: float  # P(shuffled Sharpe >= real | no edge) — lower is better
    sharpe_percentile: float  # where real Sharpe sits in null dist (0-100)
    iterations: int  # actual number of shuffled runs completed
    passed: bool  # p_value < significance
    significance: float  # threshold used (stored for auditability)

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"MCPT [{verdict}]: p={self.p_value:.4f}, "
            f"real Sharpe in {self.sharpe_percentile:.1f}th pct of null "
            f"({self.iterations} shuffled paths)"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _group_bars_by_ticker(
    events: List[MarketEvent],
) -> Dict[str, List[Bar]]:
    """
    Return a dict mapping ticker → list[Bar] in chronological order.
    Only events with a bar (not tick) are included.
    """
    per_ticker: Dict[str, List[Bar]] = {}
    for ev in events:
        if ev.bar is None:
            continue
        per_ticker.setdefault(ev.bar.symbol.ticker, []).append(ev.bar)
    return per_ticker


def _close_to_close_returns(bars: List[Bar]) -> List[float]:
    """Compute close-to-close log-style fractional returns for a bar sequence."""
    returns: List[float] = []
    for prev, curr in zip(bars, bars[1:]):
        prev_c = float(prev.close)
        curr_c = float(curr.close)
        if prev_c > 0:
            returns.append(curr_c / prev_c - 1.0)
        else:
            returns.append(0.0)
    return returns


def _reconstruct_bars(
    template_bars: List[Bar],
    shuffled_returns: List[float],
) -> List[Bar]:
    """
    Rebuild a bar sequence with the same timestamps and Symbol as *template_bars*
    but with close prices derived from compounding *shuffled_returns* from the
    same starting price.

    OHLC consistency rules (matching _crash_bar in survivorship_stress):
      - open  = previous bar's close  (gap-open, self-consistent)
      - close = open * (1 + shuffled_return)
      - high  = max(open, close)
      - low   = min(open, close)
      - volume preserved from template

    The first bar is used verbatim (no return before it), which keeps the
    starting price anchor the same across all shuffled runs.

    Invariant: len(result) == len(template_bars).
    """
    if not template_bars:
        return []

    result: List[Bar] = [template_bars[0]]  # anchor bar unchanged
    prev_close = template_bars[0].close

    for i, ret in enumerate(shuffled_returns):
        template = template_bars[i + 1]
        open_ = prev_close
        # Protect against negative prices: floor return so close stays positive.
        raw_close = open_ * Decimal(str(1.0 + ret))
        close = raw_close if raw_close > Decimal("0") else Decimal("0.01")
        high = max(open_, close)
        low = min(open_, close)
        bar = Bar(
            symbol=template.symbol,
            timestamp=template.timestamp,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=template.volume,
            timeframe=template.timeframe,
        )
        result.append(bar)
        prev_close = close

    return result


def _shuffle_events(
    per_ticker: Dict[str, List[Bar]],
    rng: random.Random,
) -> List[MarketEvent]:
    """
    For each ticker, shuffle its close-to-close RETURN MULTISET, reconstruct a
    self-consistent bar sequence, and reassemble all tickers into a
    timestamp-sorted MarketEvent list.

    The per-ticker *timestamps* are preserved exactly — only the price path
    changes.  Volume is also preserved per bar.
    """
    rebuilt: List[MarketEvent] = []
    for ticker, bars in per_ticker.items():
        if len(bars) < 2:
            # Too few bars to shuffle — keep originals so the strategy still
            # has something to see for this ticker.
            for bar in bars:
                rebuilt.append(MarketEvent(bar=bar))
            continue

        returns = _close_to_close_returns(bars)
        rng.shuffle(returns)
        new_bars = _reconstruct_bars(bars, returns)
        for bar in new_bars:
            rebuilt.append(MarketEvent(bar=bar))

    rebuilt.sort(key=lambda ev: (ev.bar.timestamp, ev.bar.symbol.ticker))
    return rebuilt


def _sharpe_from_result(result) -> float:
    """Extract the annualized Sharpe from a BacktestResult."""
    rets = metrics.returns_from_equity(result.equity_curve)
    return metrics.sharpe_ratio(rets) if len(rets) >= 2 else 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def monte_carlo_permutation_test(
    events: List[MarketEvent],
    strategy_factory: Callable[[], BaseStrategy],
    risk_config: RiskConfig,
    *,
    run_backtest_fn: Optional[Callable] = None,
    iterations: int = 200,
    seed: int = 42,
    significance: float = 0.05,
    slippage_pct: Decimal = Decimal("0.001"),
) -> PermutationResult:
    """
    Monte-Carlo Permutation Test: shuffle the price path, re-run the full
    strategy, measure whether the real Sharpe is statistically distinguishable
    from the shuffled-path null distribution.

    Args:
        events:           Chronologically sorted MarketEvents covering the
                          backtest window (one entry per bar, all tickers).
        strategy_factory: Callable that returns a FRESH, stateless strategy
                          instance.  Called once for the real run and once per
                          iteration — strategies are NOT shared across runs.
        risk_config:      Immutable risk parameters for every run.
        run_backtest_fn:  Injectable backtest function (default: the real
                          backtester).  Signature must match
                          ``run_backtest(events, strategy, risk_config,
                          slippage_pct=...) -> BacktestResult``.
        iterations:       Number of shuffled paths to evaluate (default 200).
        seed:             Master RNG seed.  Each iteration uses seed+i for
                          independence while remaining globally reproducible.
        significance:     p-value threshold for "passed" (default 0.05).
        slippage_pct:     Slippage applied uniformly to every run.

    Returns:
        PermutationResult (frozen dataclass).

    Fail-closed contract:
        If any ticker has fewer than _MIN_BARS_PER_TICKER bars, the test
        cannot meaningfully distinguish structure from noise and returns
        passed=False with p_value=1.0 and iterations=0.
    """
    _run = run_backtest_fn if run_backtest_fn is not None else _default_run_backtest

    # ---- Group bars per ticker ------------------------------------------------
    per_ticker = _group_bars_by_ticker(events)

    # ---- Fail-closed: not enough data ----------------------------------------
    if not per_ticker or all(len(bars) < _MIN_BARS_PER_TICKER for bars in per_ticker.values()):
        return PermutationResult(
            real_sharpe=0.0,
            p_value=1.0,
            sharpe_percentile=0.0,
            iterations=0,
            passed=False,
            significance=significance,
        )

    # ---- Real Sharpe (unshuffled) --------------------------------------------
    real_result = _run(
        events,
        strategy_factory(),
        risk_config,
        slippage_pct=slippage_pct,
    )
    real_sharpe = _sharpe_from_result(real_result)

    # ---- Null distribution: shuffled price paths ----------------------------
    null_sharpes: List[float] = []
    for i in range(iterations):
        # Independent seed per iteration — each path is distinct but the whole
        # experiment is deterministic given the master seed.
        iter_rng = random.Random(seed + i)
        shuffled_events = _shuffle_events(per_ticker, iter_rng)
        shuffle_result = _run(
            shuffled_events,
            strategy_factory(),
            risk_config,
            slippage_pct=slippage_pct,
        )
        null_sharpes.append(_sharpe_from_result(shuffle_result))

    # ---- p-value: fraction of null Sharpes >= real Sharpe -------------------
    beats = sum(1 for s in null_sharpes if s >= real_sharpe)
    p_value = beats / len(null_sharpes) if null_sharpes else 1.0
    percentile = 100.0 * (1.0 - p_value)

    return PermutationResult(
        real_sharpe=real_sharpe,
        p_value=p_value,
        sharpe_percentile=percentile,
        iterations=iterations,
        passed=p_value < significance,
        significance=significance,
    )
