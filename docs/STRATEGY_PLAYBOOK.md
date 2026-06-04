# Apex Quant — Strategy Research & Starter Playbook

> Grounded in research (June 2026). This is the curated starting point so we're
> not guessing. Every strategy here is documented, time-tested, and fits this
> architecture (daily bars, free IEX data, scheduled cron runs).

---

## The Honest Baseline (read this first)

The numbers are sobering and we design around them, not against them:

- ~90% of retail algo traders fail to beat a simple S&P 500 buy-and-hold in
  their first year.
- ~80% of strategies that look good in backtest fail live, due to overfitting
  to historical noise.
- Discretionary day traders fare even worse (70-80% lose money).

**Conclusion:** the edge is not cleverness. The edge is *discipline + survival*.
Our architecture already enforces the things that keep you in the surviving
minority: deterministic risk caps, mandatory stops, walk-forward validation,
and a 30-day paper gate. We lean into low-frequency, structurally-sound
strategies and avoid the games we'd lose.

---

## What This Architecture Is GOOD At

✅ **Low-frequency, daily-or-slower, rules-based strategies** where the edge is
   structural (behavioral/risk premia) rather than speed-based.
✅ Momentum / trend-following (monthly or weekly rebalance).
✅ Mean-reversion on liquid ETFs/large-caps (daily bars, 1-3 day holds).
✅ Asset-class rotation and tactical allocation.
✅ Anything where low turnover keeps transaction costs negligible.

## What This Architecture Is BAD At (don't try)

❌ High-frequency trading, scalping, market-making — we have no latency edge and
   would be the retail counterparty to institutional order flow.
❌ Sub-minute signals — free IEX data + GitHub cron (~5 min granularity) can't
   support it.
❌ News/event arbitrage requiring millisecond reaction.
❌ Anything that depends on being *faster* than the market.

> Institutions create the conditions that trigger naive retail algos and profit
> from the reversal. We don't play the speed game; we harvest slow, documented
> risk premia.

---

## The Four Starter Strategies

Build these in order. They're complementary: momentum profits from trends,
mean-reversion from dislocations within trends. Run them together.

### 1. Dual Momentum — THE ANCHOR ⭐
*File:* `apex/strategy/library/dual_momentum.py`
*Source:* Gary Antonacci, "Dual Momentum Investing" (2014)
*Timeframe:* Monthly rebalance. Lowest turnover. Best architecture fit.

**Thesis:** Combine *relative* momentum (own the best-performing asset) with
*absolute* momentum (only own it if its own trend is positive, else sit in bonds/cash).

**Rules (classic GEM version):**
- Universe: SPY (US equities), EFA/VEU (international), AGG/BIL (bonds/cash).
- Monthly, at close of last trading day:
  - Compute trailing 12-month return for SPY and the international ETF.
  - If SPY's 12-mo return > 0 (absolute momentum positive):
      own whichever of SPY / international has the higher 12-mo return.
  - Else: own bonds (AGG) or cash (BIL).
- Hold one asset at a time. Rebalance only when the selection changes.

**Documented performance (be skeptical, results vary by window):**
- Antonacci's 39-yr test: 17.43%/yr vs 8.85% index, 22.7% max DD vs 60.21%.
- Independent ETF replication: ~6.75%/yr, ~30% max DD (failed to beat SPY in
  that window). Treat the lower number as the realistic prior.

**Why it fits us:** monthly turnover = near-zero costs; uses only free daily
data; simple = overfitting-resistant; the absolute-momentum switch is itself a
drawdown guardrail that complements our RiskManager.

---

### 2. RSI(2) Mean Reversion — THE TACTICAL COMPLEMENT
*File:* `apex/strategy/library/rsi2_mean_reversion.py`
*Source:* Larry Connors, "Short-Term Trading Strategies That Work" (2008)
*Timeframe:* Daily bars, 1-3 day holds.

**Thesis:** Even in uptrends, short-term panic creates oversold dips that snap
back. Buy the dip *only* when the long-term trend is up.

**Rules (long-only version):**
- Trend filter: price > 200-day SMA (only trade longs in uptrends).
- Entry: 2-period RSI < 10 (Connors found < 5 even better) → BUY at close.
- Exit: price closes above the 5-day SMA, OR a time stop of N days.
- Suggested stop-loss: provided to RiskManager (it validates/sizes).

**Documented performance:** ~9%/yr on SPY while invested only ~28% of the time,
but ~34% max DD in volatile periods (hence the volatility filter below).

**Why it fits us:** daily bars, liquid ETFs, low frequency. Allocate only
15-25% of capital to it (it's tactical, not a whole portfolio). Naturally
complements Dual Momentum.

---

### 3. Volatility-Filtered RSI(2) — THE IMPROVEMENT
*File:* `apex/strategy/library/rsi2_vol_filtered.py`
*Source:* Refinement documented across quant blogs (2026).

**The upgrade over #2:** only take RSI(2) signals when ATR(14) is within ~1
standard deviation of its 100-day mean. Skip entries during volatility spikes,
where mean-reversion setups fail spectacularly.

**Documented effect:** ~20% fewer trades, but profit factor improves by ~0.3.
This is the kind of risk-aware refinement our whole system is built around.

**Build note:** this is strategy #2 plus one extra gate in `on_bar`. Build #2
first, then subclass or copy + add the ATR filter.

---

### 4. Weekly ETF Momentum Rotation — THE DIVERSIFIER
*File:* `apex/strategy/library/etf_rotation.py`
*Source:* Standard cross-sectional momentum (well-documented retail blueprint).

**Thesis:** Rank a basket of sector/asset ETFs by recent return; own the top N,
volatility-scaled; rebalance weekly to keep turnover (and costs) low.

**Rules:**
- Universe: a basket of liquid sector ETFs (XLK, XLF, XLE, XLV, XLY, ...) plus
  a bond ETF as the "risk-off" sleeve.
- Weekly, at Friday close:
  - Rank by trailing 3-month (or 6-month) return.
  - Allocate to the top 1-3 ETFs, position size scaled inversely to each ETF's
    recent volatility (lower vol = larger size).
  - If no ETF has positive absolute momentum, rotate to bonds.

**Why it fits us:** weekly turnover, diversified across sectors, the absolute-
momentum overlay is another built-in drawdown defense.

---

## Design Lessons Baked Into These Choices

These come straight from the research and shape how we build EVERY strategy:

1. **Alpha decays.** Past returns are not a contract — they're incomplete
   evidence about a market that no longer exists in the same form. Edges fade
   when structure changes, when the trade gets crowded, or when it was never
   real. → We monitor live-vs-backtest divergence and retire strategies that
   drift (see "Strategy Lifecycle" below).

2. **Costs + overfitting kill more strategies than bad signals.** → Favor low
   turnover (monthly/weekly > daily > intraday). Model realistic slippage +
   commission in the backtester. Always walk-forward test.

3. **Combine uncorrelated edges.** Momentum + mean-reversion together smooth the
   equity curve because they profit in different regimes. Don't run one alone.

4. **Simplicity beats complexity.** Every parameter you add is a chance to
   overfit. The classics endure because they have few knobs. Resist the urge to
   "optimize" — that's usually just curve-fitting in disguise.

5. **The absolute-momentum switch is free risk management.** Strategies that
   step aside to bonds/cash when their own trend turns negative have a built-in
   drawdown defense that stacks on top of our RiskManager.

---

## Strategy Lifecycle (how we keep edges alive)

```
RESEARCH → BACKTEST → WALK-FORWARD → PAPER (30+ days) → LIVE (small) → MONITOR
                                                                          │
                          ┌───────────────────────────────────────────────┘
                          ▼
   Live Sharpe < 70% of backtest Sharpe for 30 days?  → QUARANTINE the strategy
   (stop allocating, keep logging, investigate). This is alpha-decay defense.
```

A strategy is never "done." It's alive, monitored, and retired when it stops
working — before it does real damage, because the RiskManager caps the bleed.

---

## Recommended Build Order (strategies)

1. `dual_momentum.py` — the anchor, simplest, lowest risk. Build first.
2. `rsi2_mean_reversion.py` — the tactical complement.
3. `rsi2_vol_filtered.py` — the improvement (builds on #2).
4. `etf_rotation.py` — the diversifier.

Then run all four together in paper, each with a capped capital allocation,
and let the equity curves teach you which regimes favor which.

---

## Backtest Validation Gates (before ANY strategy goes to paper)

A strategy must clear ALL of these or it's rejected:
- [ ] Sharpe ratio ≥ 1.0 (in-sample)
- [ ] Out-of-sample Sharpe ≥ 70% of in-sample (walk-forward — anti-overfit)
- [ ] Max drawdown ≤ 25%
- [ ] ≥ 50 trades in the backtest (statistical significance)
- [ ] Profit factor ≥ 1.3
- [ ] Survives realistic slippage (0.1%) + commission modeling
- [ ] Beats SPY buy-and-hold on a risk-adjusted basis (or has low correlation to it)

These gates are enforced in code (Phase 3 backtester + Phase 4 risk approval),
not left to judgment.
