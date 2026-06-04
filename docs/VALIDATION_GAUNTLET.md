# Apex Quant — The Validation Gauntlet

> The single highest-leverage component in this entire project. A strategy that
> hasn't passed the Gauntlet **cannot reach paper trading**, and one that hasn't
> survived 30+ days of paper **cannot reach live**. This is what separates real
> edges from mirages — and it's the part we can make genuinely world-class,
> because it's pure engineering rigor, not prediction.

---

## Why This Exists

Research is unambiguous: ~80% of strategies that look good in a single backtest
fail in live markets. They fail because a single backtest measures one thing —
**how well the rules fit one specific slice of history** — and rewards exactly
the behavior that destroys you live: curve-fitting to noise.

The Gauntlet's job is to make overfitting *expensive*. Every test below attacks
the strategy from a different angle. A genuine edge survives all of them. A
mirage dies in at least one. **We want mirages to die here — cheaply, in code —
not later, in your live account.**

A strategy passing the Gauntlet is **not** a promise it will make money. It is a
statement that *if* it has a real edge, we haven't fooled ourselves about it, and
*if* it doesn't, we've given it every chance to expose itself. That distinction
is the entire value proposition.

---

## The Seven Gates

A strategy must pass **all seven** to earn `paper_approved` status. Each gate is
enforced in code (`apex/validation/`), not by judgment.

### Gate 1 — In-Sample Sanity
The baseline. Run the strategy on the training period. It must clear minimum bars
or there's no point continuing.
- Sharpe ≥ 1.0
- Max drawdown ≤ 25%
- ≥ 50 trades (statistical significance — fewer trades = luck, not edge)
- Profit factor ≥ 1.3

*Fails here → the idea doesn't even work on the data it was built on. Reject.*

### Gate 2 — Out-of-Sample Holdout
Split history: train on the first 70%, then test on the **last 30% the strategy
has never seen.** No peeking, no re-tuning.
- Out-of-sample Sharpe ≥ 70% of in-sample Sharpe.

*This is the single most important gate.* A strategy that's 1.8 Sharpe in-sample
and 0.3 out-of-sample is overfit — it memorized the training data. Reject.

### Gate 3 — Walk-Forward Analysis
The realistic version of Gate 2. Instead of one split, roll a window forward
through history:
```
[train 2yr][test 6mo] → slide → [train 2yr][test 6mo] → slide → ...
```
Re-optimize (or just re-run) on each training window, record performance only on
the *following* unseen test window, then stitch all test windows into one
continuous out-of-sample equity curve.
- Stitched walk-forward Sharpe ≥ 0.7
- Walk-forward efficiency (WF return / in-sample return) ≥ 0.5
- No single test window with a catastrophic loss (> 2× the worst in-sample DD)

*This mimics how the strategy would have actually been deployed over time. The
gold standard for non-overfit validation.*

### Gate 4 — Monte Carlo Trade Resampling
The backtest's equity curve is **one ordering** of the trades. Was the result
skill, or did three lucky trades early compound into a pretty chart? Test it:
- Take the realized trade returns. Resample them (bootstrap, 1000+ iterations) in
  random order to build a distribution of possible equity curves.
- Also run a randomized-entry version (same number of trades, random timing) as a
  null hypothesis.
- The strategy's real Sharpe must sit in the **top 5%** of the random-entry null
  distribution (i.e. p < 0.05 that the edge is luck).
- The 5th-percentile max drawdown from resampling is the **realistic** worst case
  to plan position sizing around — not the single backtest's DD, which is
  optimistically lucky.

*Catches "lucky sequence" strategies that one backtest flatters.*

### Gate 5 — Transaction Cost Stress
Re-run the winning backtest with progressively worse cost assumptions:
- Base: 0.1% slippage + commission.
- Stress: 0.2%, 0.3%, 0.5% slippage.
- The strategy must remain profitable (Sharpe > 0.5) at **2× the expected cost.**

*Many "edges" are real but smaller than their trading costs. A high-turnover
strategy that dies at 0.2% slippage is not deployable. This gate quietly kills
most overtrading strategies — which is exactly why we favor daily/weekly.*

### Gate 6 — Parameter Sensitivity
A robust edge works across a *neighborhood* of parameter values, not one magic
number. For each tunable parameter (e.g. RSI threshold, lookback length):
- Sweep ±20% around the chosen value.
- Performance must degrade *gracefully*, not fall off a cliff.
- Metric: the chosen params should not be a sharp solitary peak ("a needle"). If
  RSI<10 is great but RSI<8 and RSI<12 are losers, you've fit to noise.

*Catches the most common overfit signature: a strategy balanced on a knife's edge
of one specific parameter combination.*

### Gate 7 — Benchmark & Correlation
An edge has to be worth the risk and add something the portfolio doesn't have.
- Risk-adjusted return must beat SPY buy-and-hold (higher Sharpe), **OR**
- Correlation to SPY (and to every already-approved strategy) must be < 0.5,
  so it diversifies even if its standalone return is modest.

*A strategy that just gives you correlated beta you could get from an index fund
isn't an edge — it's leverage on the market dressed up as alpha.*

---

## The Realistic Output: a Confidence Grade, Not a Promise

The Gauntlet does NOT output "this will be profitable." It outputs an honest,
multi-dimensional report:

```
STRATEGY: dual_momentum_v1
═══════════════════════════════════════════
Gate 1 In-Sample Sanity ............ PASS (Sharpe 1.42, 87 trades, DD 18%)
Gate 2 Out-of-Sample ............... PASS (OOS Sharpe 1.11 = 78% of IS) ✓
Gate 3 Walk-Forward ................ PASS (WF Sharpe 0.94, efficiency 0.66)
Gate 4 Monte Carlo ................. PASS (p=0.012, realistic DD 24%)
Gate 5 Cost Stress ................. PASS (Sharpe 0.81 at 2× cost)
Gate 6 Param Sensitivity ........... WARN (mild peak at lookback=12)
Gate 7 Benchmark/Correlation ....... PASS (corr to SPY 0.38)
═══════════════════════════════════════════
VERDICT: PAPER-APPROVED (grade B+)
Realistic expectation: positive risk-adjusted edge LIKELY but not certain.
Plan position sizing around the Monte Carlo 24% drawdown, not the 18% backtest.
Re-validate after 30 days of paper. Quarantine if live Sharpe < 0.66.
```

**Grades:**
- **A** — passes all 7 cleanly. Rare. Deploy with confidence (still small at first).
- **B** — passes all 7, some warnings. Deploy to paper, watch closely.
- **C** — passes 5-6. Marginal. Paper only, low conviction, likely decay risk.
- **FAIL** — fails any of Gates 1-5. Does not advance. Archive and learn from it.

Gates 1-5 are **hard fails**. Gates 6-7 can issue warnings that lower the grade
without blocking, since a diversifying strategy with mild parameter sensitivity
can still earn its place in a portfolio.

---

## Post-Deployment: The Gauntlet Never Stops

Passing isn't permanent. Two ongoing checks run forever:

**Live-vs-Backtest Drift Monitor**
- Continuously compare rolling live performance to backtest expectation.
- If live 30-day Sharpe < 70% of validated Sharpe → **auto-quarantine** (stop
  allocating capital, keep logging, alert). This is the alpha-decay kill switch.

**Periodic Re-Validation**
- Re-run the full Gauntlet quarterly on updated data.
- A strategy that no longer passes is retired *before* it does serious damage —
  because the RiskManager caps the bleed in the meantime.

---

## What This Buys You (stated honestly)

It does NOT buy a winning algo. Nothing does.

It buys: **the truth about a strategy before it costs you money instead of after.**
Overfit strategies die cheaply in code. Real edges get deployed with sizing based
on realistic (not lucky) drawdowns. Decaying edges get cut automatically. You stay
in the game long enough for a genuine edge — if you have one — to actually pay off.

That discipline is the real, measurable difference between the retail traders who
survive three years and the 90% who don't. It is the most valuable thing this
entire project does.
