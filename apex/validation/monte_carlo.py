"""
apex.validation.monte_carlo
===========================
Gate 4 of the Validation Gauntlet: Monte Carlo trade resampling.

A backtest's equity curve is just ONE ordering of the trades. Maybe the edge is
real — or maybe three lucky early trades compounded into a pretty chart. This
module answers: "How likely is this result if the strategy had no real edge?"

Two techniques:
  1. Bootstrap resampling of realized trade returns → distribution of outcomes,
     so we can read off a REALISTIC worst-case drawdown (not the lucky backtest one).
  2. A null-hypothesis test: compare the real Sharpe against a distribution of
     Sharpes from random-ordered trades. If the real result isn't in the top 5%,
     we can't distinguish it from luck → fail the gate.

Uses a SEEDED RNG so results are reproducible (determinism is sacred here).
Pure stdlib (random + the metrics module) — runs on the free CI runner.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from apex.validation import metrics


@dataclass(frozen=True)
class MonteCarloResult:
    """Outcome of the Monte Carlo gate."""
    real_sharpe: float
    p_value: float                  # P(result this good | no real edge)
    sharpe_percentile: float        # where the real Sharpe sits in the null dist
    realistic_max_drawdown: float   # 95th-percentile DD — plan sizing around THIS
    median_total_return: float
    worst_5pct_total_return: float
    iterations: int
    passed: bool                    # p_value < 0.05

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"Monte Carlo [{verdict}]: p={self.p_value:.4f}, "
            f"real Sharpe in {self.sharpe_percentile:.1f}th pct of null, "
            f"realistic max DD {self.realistic_max_drawdown:.1%}"
        )


def _equity_from_trade_returns(trade_returns: list[float], start: float = 1.0) -> list[float]:
    """Compound a sequence of per-trade returns into an equity curve."""
    equity = [start]
    for r in trade_returns:
        equity.append(equity[-1] * (1.0 + r))
    return equity


def run_monte_carlo(
    trade_returns: list[float],
    iterations: int = 2000,
    seed: int = 42,
    significance: float = 0.05,
) -> MonteCarloResult:
    """
    Run the Monte Carlo gate on a strategy's realized trade returns.

    Args:
        trade_returns: per-trade returns as fractions (0.02 = +2% on that trade).
        iterations: number of resamples (>= 1000 recommended).
        seed: RNG seed for reproducibility.
        significance: p-value threshold to pass (default 0.05).

    Returns a MonteCarloResult. Requires a meaningful number of trades; with too
    few, the test is not informative and we fail closed (passed=False).
    """
    rng = random.Random(seed)
    n = len(trade_returns)

    if n < 30:
        # Too few trades to say anything statistically. Fail closed.
        real_eq = _equity_from_trade_returns(trade_returns)
        return MonteCarloResult(
            real_sharpe=metrics.sharpe_ratio(metrics.returns_from_equity(real_eq)),
            p_value=1.0,
            sharpe_percentile=0.0,
            realistic_max_drawdown=metrics.max_drawdown(real_eq),
            median_total_return=metrics.total_return(real_eq),
            worst_5pct_total_return=metrics.total_return(real_eq),
            iterations=0,
            passed=False,
        )

    # Real strategy's Sharpe (on its actual trade sequence).
    real_equity = _equity_from_trade_returns(trade_returns)
    real_sharpe = metrics.sharpe_ratio(metrics.returns_from_equity(real_equity))

    # --- Bootstrap: resample trades WITH replacement to map the outcome space. ---
    boot_drawdowns: list[float] = []
    boot_total_returns: list[float] = []
    for _ in range(iterations):
        sample = [trade_returns[rng.randrange(n)] for _ in range(n)]
        eq = _equity_from_trade_returns(sample)
        boot_drawdowns.append(metrics.max_drawdown(eq))
        boot_total_returns.append(metrics.total_return(eq))

    boot_drawdowns.sort()
    boot_total_returns.sort()
    # 95th-percentile drawdown = realistic worst case to size around.
    realistic_dd = boot_drawdowns[int(0.95 * len(boot_drawdowns))]
    median_ret = boot_total_returns[len(boot_total_returns) // 2]
    worst_5pct_ret = boot_total_returns[int(0.05 * len(boot_total_returns))]

    # --- Null hypothesis: shuffle trade ORDER (no replacement). If the edge is ---
    # real it shouldn't depend on order much; this builds a luck distribution. ---
    null_sharpes: list[float] = []
    for _ in range(iterations):
        shuffled = trade_returns[:]
        rng.shuffle(shuffled)
        eq = _equity_from_trade_returns(shuffled)
        null_sharpes.append(metrics.sharpe_ratio(metrics.returns_from_equity(eq)))

    # Note: shuffling order alone preserves Sharpe for i.i.d. returns, so we also
    # build a sign-randomized null to test whether the MAGNITUDE of edge is real.
    sign_null_sharpes: list[float] = []
    for _ in range(iterations):
        randomized = [
            r if rng.random() > 0.5 else -r for r in trade_returns
        ]
        eq = _equity_from_trade_returns(randomized)
        sign_null_sharpes.append(metrics.sharpe_ratio(metrics.returns_from_equity(eq)))

    sign_null_sharpes.sort()
    # p-value: fraction of the sign-randomized null that beats the real Sharpe.
    beats = sum(1 for s in sign_null_sharpes if s >= real_sharpe)
    p_value = beats / len(sign_null_sharpes)
    percentile = 100.0 * (1.0 - p_value)

    return MonteCarloResult(
        real_sharpe=real_sharpe,
        p_value=p_value,
        sharpe_percentile=percentile,
        realistic_max_drawdown=realistic_dd,
        median_total_return=median_ret,
        worst_5pct_total_return=worst_5pct_ret,
        iterations=iterations,
        passed=p_value < significance,
    )
