"""
apex.validation.metrics
=======================
The statistical foundation every Gauntlet gate builds on. Pure functions that
turn a series of returns or trade results into the risk-adjusted metrics that
actually matter.

Deliberately dependency-light (stdlib math + statistics) so it runs anywhere,
including the free GitHub Actions runner, with no heavy installs.

All functions are pure and deterministic given their inputs. Tested in
tests/test_metrics.py against hand-computed values.
"""
from __future__ import annotations

import math
import statistics
from typing import Sequence

TRADING_DAYS_PER_YEAR = 252


def total_return(equity_curve: Sequence[float]) -> float:
    """Cumulative return of an equity curve, as a fraction (0.20 = +20%)."""
    if len(equity_curve) < 2 or equity_curve[0] == 0:
        return 0.0
    return equity_curve[-1] / equity_curve[0] - 1.0


def returns_from_equity(equity_curve: Sequence[float]) -> list[float]:
    """Convert an equity curve into a list of period-over-period returns."""
    out: list[float] = []
    for prev, curr in zip(equity_curve, equity_curve[1:]):
        if prev == 0:
            out.append(0.0)
        else:
            out.append(curr / prev - 1.0)
    return out


def sharpe_ratio(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    Annualized Sharpe ratio. The headline risk-adjusted return metric.

    Sharpe = (mean excess return / std of returns) * sqrt(periods_per_year)

    Returns 0.0 if there's no variance (can't divide by zero) or too few points.
    A Sharpe < 1 is weak, 1-2 is decent, > 2 is excellent (and rare, and suspect).
    """
    if len(returns) < 2:
        return 0.0
    per_period_rf = risk_free_rate / periods_per_year
    excess = [r - per_period_rf for r in returns]
    mean = statistics.fmean(excess)
    sd = statistics.pstdev(excess)
    if sd == 0:
        return 0.0
    return (mean / sd) * math.sqrt(periods_per_year)


def sortino_ratio(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    Like Sharpe but only penalizes DOWNSIDE volatility. Upside swings shouldn't
    count as 'risk'. Often a fairer measure for asymmetric strategies.
    """
    if len(returns) < 2:
        return 0.0
    per_period_rf = risk_free_rate / periods_per_year
    excess = [r - per_period_rf for r in returns]
    downside = [min(0.0, r) for r in excess]
    downside_dev = math.sqrt(statistics.fmean([d * d for d in downside]))
    if downside_dev == 0:
        return 0.0
    return (statistics.fmean(excess) / downside_dev) * math.sqrt(periods_per_year)


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """
    Worst peak-to-trough decline, as a positive fraction (0.25 = -25% drawdown).
    The number that actually determines whether you can stomach a strategy and
    how much you can size into it.
    """
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for value in equity_curve:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak
            if dd > worst:
                worst = dd
    return worst


def profit_factor(trade_returns: Sequence[float]) -> float:
    """
    Gross profit / gross loss across trades. > 1 means profitable.
    > 1.3 is our minimum bar; > 2 is strong. inf if there are no losing trades
    (treat with suspicion — usually too few trades).
    """
    gross_profit = sum(r for r in trade_returns if r > 0)
    gross_loss = abs(sum(r for r in trade_returns if r < 0))
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def win_rate(trade_returns: Sequence[float]) -> float:
    """Fraction of trades that were profitable (0.0-1.0)."""
    if not trade_returns:
        return 0.0
    wins = sum(1 for r in trade_returns if r > 0)
    return wins / len(trade_returns)


def annualized_return(
    equity_curve: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Compound annual growth rate implied by the equity curve."""
    if len(equity_curve) < 2 or equity_curve[0] <= 0:
        return 0.0
    periods = len(equity_curve) - 1
    growth = equity_curve[-1] / equity_curve[0]
    if growth <= 0:
        return -1.0
    return growth ** (periods_per_year / periods) - 1.0


def calmar_ratio(
    equity_curve: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Annualized return / max drawdown. Reward per unit of worst-case pain."""
    mdd = max_drawdown(equity_curve)
    if mdd == 0:
        return 0.0
    return annualized_return(equity_curve, periods_per_year) / mdd


def correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """
    Pearson correlation between two return series. Used by Gate 7 to confirm a
    strategy diversifies (low correlation to SPY and to other approved strategies).
    Returns 0.0 if undefined.
    """
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a, b = a[:n], b[:n]
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    denom = math.sqrt(var_a * var_b)
    if denom == 0:
        return 0.0
    return cov / denom


# =============================================================================
# Overfitting-aware statistics (Bailey & Lopez de Prado).
#
# A single backtest Sharpe is a biased, noisy estimate. These functions ask the
# harder question the Gauntlet's hard gates depend on: *given the sample length,
# the non-normality of the returns, and how many strategies were tried, how
# likely is this Sharpe to be a real edge rather than the best of many lucky
# draws?* They are the statistical core of the new overfitting gates.
#
# References:
#   Bailey, D. & Lopez de Prado, M. (2012) "The Sharpe Ratio Efficient Frontier"
#   Bailey, D. & Lopez de Prado, M. (2014) "The Deflated Sharpe Ratio"
#   Bailey, Borwein, Lopez de Prado & Zhu (2017) "The Probability of Backtest
#       Overfitting" (the CSCV / PBO framework)
# =============================================================================


def _norm_cdf(x: float) -> float:
    """Standard-normal CDF via the error function (stdlib, deterministic)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """
    Inverse standard-normal CDF (quantile function). Acklam's rational
    approximation — accurate to ~1.15e-9 across (0, 1), pure stdlib, fully
    deterministic. Clamps the open interval so the tails never blow up.
    """
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

    # Coefficients for Acklam's algorithm.
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]

    p_low = 0.02425
    p_high = 1.0 - p_low

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)


def skewness(returns: Sequence[float]) -> float:
    """
    Population (Fisher) skewness of a return series. A negative skew means a fat
    LEFT tail (crash risk) — exactly the shape a naive Sharpe flatters. Returns
    0.0 when undefined (too few points or zero variance).
    """
    n = len(returns)
    if n < 3:
        return 0.0
    mean = statistics.fmean(returns)
    sd = statistics.pstdev(returns)
    if sd == 0:
        return 0.0
    return statistics.fmean([((r - mean) / sd) ** 3 for r in returns])


def kurtosis(returns: Sequence[float], *, excess: bool = False) -> float:
    """
    Population kurtosis of a return series. ``excess=False`` (default) returns
    the RAW kurtosis where a normal distribution scores 3.0 — this is the γ4 the
    PSR/DSR formulas expect. ``excess=True`` subtracts 3 (normal → 0). Fat tails
    push it well above 3, inflating Sharpe's standard error. 0.0 (or -3.0 excess)
    when undefined.
    """
    n = len(returns)
    if n < 4:
        return 0.0 if not excess else -3.0
    mean = statistics.fmean(returns)
    sd = statistics.pstdev(returns)
    if sd == 0:
        return 0.0 if not excess else -3.0
    k = statistics.fmean([((r - mean) / sd) ** 4 for r in returns])
    return k - 3.0 if excess else k


def _per_period_sharpe(returns: Sequence[float], risk_free_rate: float,
                       periods_per_year: int) -> float:
    """Non-annualized (per-period) Sharpe — what the PSR/DSR formulas operate on."""
    per_period_rf = risk_free_rate / periods_per_year
    excess = [r - per_period_rf for r in returns]
    sd = statistics.pstdev(excess)
    if sd == 0:
        return 0.0
    return statistics.fmean(excess) / sd


def probabilistic_sharpe_ratio(
    returns: Sequence[float],
    reference_sharpe: float = 0.0,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    *,
    annualized_reference: bool = True,
) -> float:
    """
    Probabilistic Sharpe Ratio (Bailey & Lopez de Prado, 2012).

    The probability that the strategy's TRUE Sharpe exceeds ``reference_sharpe``,
    given the observed sample. Unlike the point Sharpe it accounts for sample
    length AND the non-normality of returns (skew and fat tails both REDUCE
    confidence). Returned as a probability in [0, 1].

        PSR(SR*) = Φ( (SR_hat - SR*) · sqrt(n - 1)
                      / sqrt(1 - γ3·SR_hat + (γ4 - 1)/4 · SR_hat²) )

    where SR_hat and SR* are NON-annualized (per-period) Sharpes, γ3 is skewness
    and γ4 is raw kurtosis (normal = 3).

    Args:
        reference_sharpe: the benchmark Sharpe to beat. By default it is treated
            as ANNUALIZED (the natural way to express "beat Sharpe 1.0") and is
            de-annualized internally; pass ``annualized_reference=False`` to give
            an already-per-period reference.

    Fails CLOSED: returns 0.0 (no confidence) when there are too few points or no
    variance — never a falsely reassuring number.
    """
    n = len(returns)
    if n < 4:
        return 0.0
    sr_hat = _per_period_sharpe(returns, risk_free_rate, periods_per_year)
    if sr_hat == 0.0:
        # Either no variance or exactly-zero mean: no demonstrable edge.
        return 0.0
    sr_star = (reference_sharpe / math.sqrt(periods_per_year)
               if annualized_reference else reference_sharpe)
    g3 = skewness(returns)
    g4 = kurtosis(returns, excess=False)
    denom_sq = 1.0 - g3 * sr_hat + ((g4 - 1.0) / 4.0) * sr_hat * sr_hat
    if denom_sq <= 0:
        # Degenerate standard error — cannot make a confident claim. Fail closed.
        return 0.0
    z = (sr_hat - sr_star) * math.sqrt(n - 1) / math.sqrt(denom_sq)
    return _norm_cdf(z)


def expected_max_sharpe(num_trials: int, sharpe_variance: float) -> float:
    """
    Expected MAXIMUM (per-period) Sharpe obtainable by pure luck after running
    ``num_trials`` independent backtests whose Sharpe estimates have variance
    ``sharpe_variance`` (Bailey & Lopez de Prado 2014, the DSR's "SR0").

        E[max SR] ≈ sqrt(V) · [ (1 - γ)·Φ⁻¹(1 - 1/N)
                                + γ·Φ⁻¹(1 - 1/(N·e)) ]

    where γ ≈ 0.5772 is the Euler-Mascheroni constant. This is the bar a Sharpe
    must clear just to NOT be the best of many coin flips. Returns 0.0 for
    degenerate inputs (≤ 1 trial or non-positive variance).
    """
    if num_trials < 2 or sharpe_variance <= 0:
        return 0.0
    euler = 0.5772156649015329
    n = float(num_trials)
    term = ((1.0 - euler) * _norm_ppf(1.0 - 1.0 / n)
            + euler * _norm_ppf(1.0 - 1.0 / (n * math.e)))
    return math.sqrt(sharpe_variance) * term


def deflated_sharpe_ratio(
    returns: Sequence[float],
    num_trials: int,
    trial_sharpe_variance: float | None = None,
    trial_sharpes: Sequence[float] | None = None,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014).

    The PSR evaluated against the EXPECTED-MAXIMUM Sharpe a researcher would hit
    by luck after ``num_trials`` attempts — the single most direct antidote to
    selection bias / backtest overfitting. A high point Sharpe found after 500
    parameter tries deflates toward 0; the same Sharpe found on the first try
    barely deflates at all. Returned as a probability in [0, 1].

    Provide the spread of the trials' Sharpes ONE of two ways:
        * ``trial_sharpe_variance``: the variance of the trial Sharpe estimates
          (per-period). Used directly.
        * ``trial_sharpes``: the list of trial Sharpes; their sample variance is
          computed for you. If these are annualized they are de-annualized first
          so the variance is on the same per-period scale as the formula.

    If neither is given, the variance is unknown and we FAIL CLOSED → 0.0.
    """
    n = len(returns)
    if n < 4 or num_trials < 1:
        return 0.0

    if trial_sharpe_variance is None:
        if trial_sharpes is not None and len(trial_sharpes) >= 2:
            per_period = [s / math.sqrt(periods_per_year) for s in trial_sharpes]
            trial_sharpe_variance = statistics.variance(per_period)
        else:
            # No information about how many independent shots were taken → cannot
            # deflate honestly. Fail closed.
            return 0.0

    sr0_annualized = (expected_max_sharpe(num_trials, trial_sharpe_variance)
                      * math.sqrt(periods_per_year))
    return probabilistic_sharpe_ratio(
        returns,
        reference_sharpe=sr0_annualized,
        risk_free_rate=risk_free_rate,
        periods_per_year=periods_per_year,
        annualized_reference=True,
    )


def min_track_record_length(
    returns: Sequence[float],
    reference_sharpe: float = 0.0,
    target_confidence: float = 0.95,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    *,
    annualized_reference: bool = True,
) -> float:
    """
    Minimum Track Record Length (Bailey & Lopez de Prado, 2012): how many return
    observations you would need before you could state, at ``target_confidence``,
    that the true Sharpe exceeds ``reference_sharpe``.

        MinTRL = 1 + [1 - γ3·SR_hat + (γ4 - 1)/4 · SR_hat²]
                     · (Φ⁻¹(conf) / (SR_hat - SR*))²

    Returns ``math.inf`` when the observed Sharpe does not even beat the
    reference (you can never reach confidence — fail closed), and 0.0 for
    degenerate input.
    """
    n = len(returns)
    if n < 4:
        return math.inf
    sr_hat = _per_period_sharpe(returns, risk_free_rate, periods_per_year)
    sr_star = (reference_sharpe / math.sqrt(periods_per_year)
               if annualized_reference else reference_sharpe)
    if sr_hat <= sr_star:
        return math.inf
    g3 = skewness(returns)
    g4 = kurtosis(returns, excess=False)
    denom_sq = 1.0 - g3 * sr_hat + ((g4 - 1.0) / 4.0) * sr_hat * sr_hat
    if denom_sq <= 0 or not 0.0 < target_confidence < 1.0:
        return math.inf
    z = _norm_ppf(target_confidence)
    return 1.0 + denom_sq * (z / (sr_hat - sr_star)) ** 2


def probability_of_backtest_overfitting(
    performance_matrix: Sequence[Sequence[float]],
    n_splits: int = 16,
    seed: int = 42,
) -> float:
    """
    Probability of Backtest Overfitting (PBO) via Combinatorially-Symmetric
    Cross-Validation (Bailey, Borwein, Lopez de Prado & Zhu, 2017).

    Input is a PERFORMANCE MATRIX: ``performance_matrix[t][c]`` is the
    performance (e.g. Sharpe, or any larger-is-better score) of strategy
    configuration ``c`` in time-slice ``t``. There are ``T`` rows (time slices,
    must be even and ≥ 4) and ``N`` columns (the configurations that were tried).

    CSCV splits the T rows into two equal halves every possible way (C(T, T/2)
    combinations, halved by symmetry). For each split:
        * pick the config that was BEST in-sample (one half),
        * record its RANK out-of-sample (the other half),
        * its relative rank ω = rank / (N + 1); the logit λ = ln(ω / (1 - ω)).
    PBO is the fraction of splits where the in-sample champion lands in the
    BOTTOM half out-of-sample (λ < 0) — i.e. where chasing the best backtest
    actively HURT you out of sample. PBO near 0.5 means the selection is no
    better than random: classic overfitting.

    To stay cheap on the free CI runner, when C(T, T/2) exceeds ``n_splits`` we
    sample ``n_splits`` distinct combinations using a SEEDED RNG (determinism is
    sacred) rather than enumerating all of them. With few enough rows we
    enumerate exhaustively.

    Fails CLOSED: returns 1.0 (maximally overfit / no confidence) when the matrix
    is too small or malformed to evaluate.
    """
    import itertools
    import random as _random

    T = len(performance_matrix)
    if T < 4 or T % 2 != 0:
        return 1.0
    N = len(performance_matrix[0])
    if N < 2 or any(len(row) != N for row in performance_matrix):
        return 1.0

    half = T // 2
    all_rows = list(range(T))

    # Enumerate or sample the in-sample row sets. By symmetry only half of the
    # C(T, half) combinations are distinct partitions, but evaluating both
    # orientations is harmless and keeps the code simple, so we cap by n_splits.
    total_combos = math.comb(T, half)
    if total_combos <= n_splits:
        is_row_sets = [set(c) for c in itertools.combinations(all_rows, half)]
    else:
        rng = _random.Random(seed)
        seen: set[frozenset[int]] = set()
        is_row_sets = []
        # Deterministic sampling without replacement of distinct combinations.
        attempts = 0
        max_attempts = n_splits * 50
        while len(is_row_sets) < n_splits and attempts < max_attempts:
            combo = frozenset(rng.sample(all_rows, half))
            attempts += 1
            if combo not in seen:
                seen.add(combo)
                is_row_sets.append(set(combo))

    if not is_row_sets:
        return 1.0

    logits: list[float] = []
    for is_rows in is_row_sets:
        oos_rows = [r for r in all_rows if r not in is_rows]

        # In-sample mean performance per configuration.
        is_perf = [
            statistics.fmean([performance_matrix[t][c] for t in is_rows])
            for c in range(N)
        ]
        # Best config in sample (ties → lowest index, deterministic).
        best_c = max(range(N), key=lambda c: is_perf[c])

        # Out-of-sample mean performance per configuration → rank the champion.
        oos_perf = [
            statistics.fmean([performance_matrix[t][c] for t in oos_rows])
            for c in range(N)
        ]
        # Rank = number of configs the champion beats or ties (1..N), so a higher
        # OOS performance gives a higher rank.
        champion_oos = oos_perf[best_c]
        rank = sum(1 for c in range(N) if oos_perf[c] <= champion_oos)
        omega = rank / (N + 1)
        # Guard the open interval before the logit.
        omega = min(max(omega, 1e-12), 1.0 - 1e-12)
        logits.append(math.log(omega / (1.0 - omega)))

    if not logits:
        return 1.0
    # PBO = P(λ < 0): fraction of splits where the IS champion was below-median OOS.
    below = sum(1 for lam in logits if lam < 0.0)
    return below / len(logits)


def turnover(weights_over_time: Sequence[Sequence[float]]) -> float:
    """
    Average one-way portfolio turnover per rebalance: the mean over periods of
    0.5 · Σ|w_t,i − w_{t-1},i|. A turnover of 1.0 means the whole book is
    replaced each period (very cost-sensitive); 0.05 means a slow, capacity-
    friendly strategy. Returns 0.0 when there is nothing to compare.
    """
    if len(weights_over_time) < 2:
        return 0.0
    width = len(weights_over_time[0])
    if width == 0 or any(len(w) != width for w in weights_over_time):
        return 0.0
    deltas: list[float] = []
    for prev, curr in zip(weights_over_time, weights_over_time[1:]):
        deltas.append(0.5 * sum(abs(c - p) for p, c in zip(prev, curr)))
    return statistics.fmean(deltas) if deltas else 0.0


def capacity_score(
    annualized_return_estimate: float,
    annual_turnover: float,
    cost_per_turn: float = 0.001,
) -> float:
    """
    A coarse turnover/capacity sanity ratio: how many multiples of its expected
    trading cost the strategy's gross return covers.

        capacity = annualized_return / (annual_turnover · cost_per_turn)

    A value of 1.0 means trading costs eat the ENTIRE edge; > 5 is comfortable,
    < 2 is fragile and capacity-constrained. ``annual_turnover`` is one-way
    turnover summed over a year (e.g. per-rebalance turnover × rebalances/year).
    Returns ``math.inf`` when the strategy never trades (no cost drag) and a
    positive return, and 0.0 when there is no positive return to defend.
    """
    if annualized_return_estimate <= 0:
        return 0.0
    annual_cost = annual_turnover * cost_per_turn
    if annual_cost <= 0:
        return math.inf
    return annualized_return_estimate / annual_cost
