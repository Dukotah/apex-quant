# Apex Quant — Decisions Log

> Running record of design decisions. **Paste this at the start of every session**
> so Claude has continuity. **Append to it at the end of every session.**
> Newest entries at the top.

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
