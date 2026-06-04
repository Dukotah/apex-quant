# Apex Quant — Decisions Log

> Running record of design decisions. **Paste this at the start of every session**
> so Claude has continuity. **Append to it at the end of every session.**
> Newest entries at the top.

---

## Session 2 — Massive sweep: Phase 2 data feed, Phase 4/5 modules, all 4 strategies, full Gauntlet integration

**Context:** the repo had been delivered as an unextracted tarball
(`Downloads/apex-quant.tar.gz`); extracted to `~/apex-quant`. Baseline confirmed
at 79 tests. This session took the suite to **262 tests passing**.

**What was built (one parallel sweep + sequential integration):**
- `apex/data/historical_feed.py` — CSV/Parquet replay → chronological MarketEvents
  (stdlib CSV core, lazy-pandas Parquet, stable sort, bad-row skip+count). +15 tests.
- `apex/risk/portfolio.py` — position/cash/equity/drawdown tracker exposing the exact
  6-attr snapshot the RiskManager reads. +20 tests.
- `apex/execution/simulated.py` — deterministic paper fills (adverse slippage +
  commission, "SIM-N" ids, fail-closed on no-price / non-MARKET). +39 tests.
- `apex/execution/factory.py` — the ONE mode→engine switch (backtest/paper→sim,
  live→NotImplementedError, fail-closed).
- `apex/execution/engine.py` — `TradingEngine` orchestrator: next-bar-open fills
  (no look-ahead), per-day equity, trade-return capture, halt enforcement, strategy
  quarantine. +10 tests (with factory).
- `apex/backtest/{synthetic,backtester,gauntlet_runner}.py` — the adapter that turns a
  strategy run into `(equity_curve, trade_returns)` and drives all 7 Gauntlet gates;
  seeded synthetic data generator for reproducible demos. +4 tests.
- `scripts/run_backtest.py` — capstone CLI: runs dual_momentum / rsi2 through the full Gauntlet.
- Strategies implemented from their stubs: `dual_momentum`, `rsi2_mean_reversion`,
  `rsi2_vol_filtered`, `etf_rotation` (+57 strategy tests).
- `tests/test_risk_manager.py` — formalized the smoke test into 38 cases.

**Key decisions:**
- **RiskManager made reduce-aware (FROZEN FILE EDIT — explicitly approved).** The
  original sizer sized *every* signal (incl. SELL-to-exit) by remaining exposure room,
  so once fully invested an exit sized to 0 and was rejected — no strategy could close
  or rotate. Added a reduce path: a SELL-while-long / BUY-while-short is sized to flatten
  (≤ held qty), exempt from the exposure/leverage caps and the mandatory-stop requirement
  (de-risking must always be allowed). Entry behaviour is byte-for-byte unchanged; all 38
  risk tests still pass. *A risk manager that won't let you close a position is itself a bug.*
- **Engine sizes entries against a portfolio projected free of pending exits.** A rotation
  emits SELL(old)+BUY(new) in one bar; the BUY would otherwise be sized against the still-
  invested portfolio and rejected. `_project_after_exits` removes the exiting positions'
  exposure so the BUY sizes into the capital its SELL is about to free. Both fill next-open.
- **Two engine bugs found via end-to-end runs and fixed:** (1) the drawdown/daily-loss halt
  is *lazy* — only evaluated when a signal is processed (fine in practice, documented);
  (2) the engine reset the portfolio's daily baseline but never called
  `risk_manager.reset_daily()`, so a daily-loss halt was *permanent* — it silently killed
  ~90% of RSI2's trades (9 vs 89). Now reset on each day boundary (drawdown halts stay sticky).
- **No look-ahead:** signals decided on bar T's close fill at bar T+1's open. The single
  most important anti-overfitting property of the backtester.
- **Single-strategy backtests use a full-deployment RiskConfig** (100% position/exposure);
  the 5%/50% retail caps are for multi-strategy *live* capital sharing, not for measuring
  one strategy's raw edge through the Gauntlet.
- **Synthetic data caveat:** no market-data vendor is wired yet, so demos use a seeded
  synthetic generator. Grades demonstrate the *pipeline*, not a real edge. Swap in a
  HistoricalDataFeed on real OHLCV for a real run — nothing else changes.

**Verified end-to-end:** dual_momentum rotates SPY↔EFA↔AGG and runs all 7 gates →
honest **FAIL** (only ~10 trades < the 50-trade significance bar; low-turnover by nature).
RSI2 runs all 7 gates with real statistics (89 trades, Monte Carlo actually executes) →
honest **FAIL** on edgeless random-walk data. The Gauntlet's ability to *pass* a real edge
remains covered by `test_gauntlet.py`.

**Honest finding to revisit:** Gate 1's `MIN_TRADES=50` structurally fails low-turnover
strategies (monthly dual momentum). Consider a regime-aware minimum (e.g. scale by
rebalance frequency) so the anchor strategy can be fairly graded.

**Next:** real data ingestion (`alpaca_feed.py` / load real OHLCV CSVs) to run the Gauntlet
on actual history; then `execution/alpaca.py` + wire `run_once.py` for paper trading.

---

## Session 1 — Phase 1 complete + indicators + first strategy

**What was built (all tested — 79 tests total passing):**
- `apex/core/event_bus.py` — central FIFO queue + pub/sub. Thread-safe, never
  drops events, fails loud (re-raises handler errors after running all handlers).
- `apex/core/clock.py` — `Clock` ABC, `RealClock` (wall UTC), `SimulatedClock`
  (backtest, enforces monotonicity — time can't go backward).
- `apex/strategy/indicators.py` — SMA, EMA, RSI (Wilder), MACD, Bollinger Bands,
  ATR, rolling_return, crosses_above/below. Same-length output, None during
  warmup, deterministic. Verified against hand-computed values.
- `apex/strategy/library/sma_crossover.py` — first COMPLETE working strategy.
  Validates the whole pipeline: bars → indicator → SignalEvent. Long/flat with
  suggested stop. Self-contained price buffer, fully unit-tested.

**Key decisions:**
- Indicators work in float internally (speed, comparative) while money math stays
  Decimal elsewhere.
- Clock monotonicity enforced — out-of-order bars raise, never silently corrupt.
- Event bus fails loud — a raising subscriber doesn't get swallowed.
- Strategies keep their own price buffer (testable in isolation). SMA crossover
  is the template shape for all future strategies.
- SMA crossover is a pipeline test / teaching example, NOT a deploy target.

**Status:** Phase 1 DONE. Phase 3 indicators + reference strategy DONE. Vertical
slice works end-to-end (data → strategy → risk → validation).

**Next (dependency order):** historical_feed.py (Phase 2) → portfolio.py (Phase 4)
→ simulated execution + engine + backtester (Phase 5, activates remaining Gauntlet
gates) → implement real strategies (dual_momentum first) → run full Gauntlet.

---

## Session 0.6 — The Validation Gauntlet (the differentiator)

**What was built (all tested, 28 tests passing):**
- `docs/VALIDATION_GAUNTLET.md` — full spec of the 7-gate validation system.
- `apex/validation/metrics.py` — Sharpe, Sortino, max drawdown, profit factor,
  Calmar, correlation, annualized return. Pure stdlib, hand-verified.
- `apex/validation/monte_carlo.py` — Gate 4. Bootstrap + sign-randomized null
  hypothesis. Distinguishes real edge from lucky sequence; outputs realistic
  (95th-pct) drawdown to size around. Seeded/reproducible.
- `apex/validation/walk_forward.py` — Gate 3. Rolling train/test windowing that
  stitches out-of-sample test curves. Backtest fn injected (plugs into Phase 5).
- `apex/validation/gauntlet.py` — orchestrator. All 7 gates + grading rubric
  (A/B/C/FAIL) + auto-quarantine floor computation. Outputs an honest graded
  report, never a profit promise.

**Key decisions:**
- **The Gauntlet is THE differentiator.** Not strategy cleverness — validation
  rigor. Its job is to make overfitting expensive: mirages die cheaply in code,
  not expensively in the live account.
- **7 gates:** in-sample sanity, out-of-sample holdout, walk-forward, Monte
  Carlo, cost stress, parameter sensitivity, benchmark/correlation. Gates 1-5 are
  hard fails; 6-7 can only warn (a diversifier with mild param sensitivity can
  still earn a place).
- **Output is a confidence GRADE, not a profit promise.** Explicitly designed to
  never claim profitability — only "if there's a real edge, we haven't fooled
  ourselves; if not, we gave it every chance to expose itself."
- **Size around the Monte Carlo realistic drawdown**, never the backtest's lucky
  DD. This is baked into the report.
- **Auto-quarantine floor = 70% of validated Sharpe.** The alpha-decay kill switch.
- **Stdlib-only** (math/statistics/random) so it runs on the free CI runner with
  no heavy installs.

**Verified end-to-end:** a real edge passes all gates (grade A); an overfit
mirage with in-sample Sharpe 7.12 gets killed at Gate 2 (OOS Sharpe collapses to
12% of in-sample). Exactly the intended behavior.

**What still needs Phase 5:** cost-stress and parameter-sweep gates have
framework code but need the real backtester to feed them equity curves. Drift
monitor (live-vs-backtest) still to build.

**Next:** Phase 1 finish — `event_bus.py` + `clock.py`. Then the backtester
(Phase 5) so the remaining gates activate against real strategy runs.

---

## Session 0.5 — Strategy Research & Starter Specs

**What was added:**
- `docs/STRATEGY_PLAYBOOK.md` — researched strategy guidance (June 2026).
- Four spec'd strategy stubs in `apex/strategy/library/` with full rules in
  docstrings: `dual_momentum.py`, `rsi2_mean_reversion.py`,
  `rsi2_vol_filtered.py`, `etf_rotation.py`. All raise NotImplementedError until
  built in Phase 3.

**Key decisions (grounded in research):**
- **This architecture's lane = low-frequency, daily-or-slower, rules-based
  strategies harvesting documented risk premia.** NOT HFT/scalping/market-making
  (no latency edge; we'd be retail order-flow fodder).
- **Build order: Dual Momentum first.** Lowest turnover, fewest params, monthly
  rebalance, built-in absolute-momentum drawdown switch. Then RSI(2) as the
  complementary tactical sleeve (capped 15-25% of capital).
- **Run momentum + mean-reversion together** — uncorrelated edges smooth the
  equity curve (they win in different regimes).
- **Alpha decay is assumed, not hoped against.** Strategy Lifecycle: quarantine
  any strategy whose live Sharpe < 70% of backtest Sharpe for 30 days.
- **Backtest validation gates mandatory** (Sharpe ≥1.0, walk-forward OOS ≥70% of
  in-sample, max DD ≤25%, ≥50 trades, profit factor ≥1.3, survives slippage).
  Enforced in code.
- **Honest baseline acknowledged:** ~90% of retail algos fail year one; ~80% of
  good-backtest strategies fail live. Edge = discipline + survival, enforced by
  the RiskManager + 30-day paper gate.

**Next:** Phase 1 finish — `event_bus.py` + `clock.py` + model/event tests.

---

## Session 0 — Framework Foundation (initial scaffold)

**What was built:**
- `apex/core/models.py` — asset-agnostic frozen data models: `Bar`, `Tick`,
  `Symbol` (with `AssetClass` enum + contract multiplier for futures/crypto),
  `Position`, and order enums. Bars self-validate in `__post_init__` (reject
  negative prices, high<low, naive timestamps).
- `apex/core/events.py` — the event taxonomy: `MarketEvent`, `SignalEvent`,
  `OrderEvent`, `FillEvent`, `HaltEvent`. All frozen. Each event carries a UUID
  and links back to its parent (signal_id, order_id) for full traceability.
- `apex/core/config.py` — `AppConfig` with the `ExecutionMode` switch
  (backtest/paper/live) and `Broker` enum. `from_env()` raises if LIVE is paired
  with the simulated broker (safety).
- `apex/data/base_feed.py` — `BaseDataFeed` ABC. Context-manager support.
- `apex/strategy/base_strategy.py` — `BaseStrategy` ABC with `on_bar`/`on_tick`
  hooks + `StrategyContext` (read-only state view).
- `apex/risk/risk_manager.py` — `RiskManager` (the gatekeeper) + frozen
  `RiskConfig`. Smoke-tested and working.
- `apex/execution/base_execution.py` — `BaseExecutionEngine` ABC with the
  paper/live abstraction and fill-callback wiring.

**Key design decisions (the "why"):**
- **Signals carry conviction (`strength` 0..1), not quantity.** Strategies express
  *how confident*, the risk manager translates that into *how many shares* within
  hard caps. This keeps sizing logic in exactly one place.
- **Position sizing lives inside the RiskManager**, not a separate sizer module
  (for now). It's tightly coupled to the exposure/leverage checks, so co-locating
  prevents a signal being "approved" then separately "sized too big."
- **Risk checks fail closed** — the `evaluate()` method wraps everything in
  try/except and REJECTS on any exception. Safety beats availability.
- **Drawdown breach sets a persistent `_halted` flag** on the risk manager and
  emits a `HaltEvent`. Daily-loss halts can be reset via `reset_daily()`;
  drawdown halts are sticky until manual review.
- **Decimal everywhere for money.** No floats in price/quantity/cash math.
- **All timestamps UTC, timezone-aware**, enforced at model construction.

**Verified:**
- Smoke test passed: compliant signal → 25 shares of AAPL ($100k equity × 5% ÷
  $200); missing stop → rejected; 15% drawdown → system halt + rejection.

**Free-stack decisions:**
- Broker: **Alpaca** (free paper, commission-free live, IEX free data feed).
- Runtime: **GitHub Actions cron** for scheduled runs (free, public repo);
  **Oracle Cloud Always Free** VM if a persistent process is ever needed.
- State: **SQLite** in-repo to start (zero setup); Supabase free tier if the
  dashboard sibling project is wired in.
- AI: strategies authored in **Claude Pro chat** and pasted in → $0 API cost.
  Runtime is fully deterministic and calls no LLM.

**Next:** Phase 1 finish — `event_bus.py` + `clock.py` + model/event tests.

---

<!-- TEMPLATE for future sessions — copy this block:

## Session N — <title>

**What was built:**
- 

**Key design decisions:**
- 

**Verified:**
- 

**Next:**
- 

-->
