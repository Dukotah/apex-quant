# Apex Quant — Build Roadmap

The system is built in 5 sequential phases. Each phase has concrete deliverables.
**Complete phases in order.** Each session builds ONE module from the current phase.

Status legend: ✅ done · 🔲 to build · 🚧 in progress

---

## Phase 1 — Core Event System & Data Models
*The skeleton everything depends on. Get this perfect.*

| Module | File | Status |
|--------|------|--------|
| Data models | `apex/core/models.py` | ✅ |
| Event taxonomy | `apex/core/events.py` | ✅ |
| App config + MODE switch | `apex/core/config.py` | ✅ |
| Event bus | `apex/core/event_bus.py` | 🔲 |
| Clock (real + simulated) | `apex/core/clock.py` | 🔲 |
| Tests for models/events | `tests/test_models.py`, `tests/test_events.py` | 🔲 |

**Event bus requirements:** a central queue (collections.deque) with `put(event)`,
`get()`, and `is_empty()`. Optional pub/sub subscriber registration by EventType.
Never silently drops events. Thread-safe if live mode needs it.

**Clock requirements:** abstract `Clock` with `now() -> datetime`. `RealClock`
returns wall-clock UTC. `SimulatedClock` returns the timestamp of the current
backtest bar and enforces monotonicity (time never goes backward). This is what
makes backtests deterministic.

**Done when:** push a fake MarketEvent onto the bus, a subscriber receives it;
all model validation tests pass (bad bars rejected, etc.).

---

## Phase 2 — Data Feed Layer
*Normalized data flowing into the bus from any source.*

| Module | File | Status |
|--------|------|--------|
| Base feed | `apex/data/base_feed.py` | ✅ |
| Historical replay (CSV/Parquet) | `apex/data/historical_feed.py` | ✅ |
| Alpaca live/paper feed | `apex/data/alpaca_feed.py` | ✅ (DI seam; 15 tests) |
| Source normalizer | `apex/data/normalizer.py` | ✅ (24 tests) |
| Tests | `tests/test_historical_feed.py`, `tests/test_alpaca_feed.py`, `tests/test_normalizer.py` | ✅ |

**Historical feed:** reads a CSV/Parquet of OHLCV, yields MarketEvents in strict
chronological order, then stops (ending the backtest). Handles multiple symbols
interleaved by timestamp. This powers ALL backtesting.

**Alpaca feed:** uses `alpaca-py` SDK. Fetches historical bars for backtest warmup
and streams live bars (IEX free feed) for paper/live. Reconnection with
exponential backoff. Gap detection. UTC normalization. Bad-tick filtering.

**Done when:** historical feed replays a year of NVDA daily bars in correct order
and a logger subscriber prints each one.

---

## Phase 3 — Strategy Layer & Indicators
*Where alpha lives. Pure, testable, I/O-free.*

| Module | File | Status |
|--------|------|--------|
| Base strategy | `apex/strategy/base_strategy.py` | ✅ |
| Indicator library | `apex/strategy/indicators.py` | ✅ |
| Reference: SMA crossover | `apex/strategy/library/sma_crossover.py` | ✅ |
| Indicator tests | `tests/test_indicators.py` | ✅ |
| Strategy tests | `tests/test_sma_crossover.py` | ✅ |
| Library: dual_momentum, rsi2_mean_reversion, rsi2_vol_filtered, etf_rotation | `apex/strategy/library/` | ✅ |

**Indicators:** SMA, EMA, RSI, MACD, Bollinger Bands, ATR, VWAP. Stateless,
operate on lists of Decimal. Each tested against hand-computed known values.
Handle insufficient-data windows gracefully (return None, never garbage).

**SMA crossover:** the reference strategy that validates the whole hook system.
Buy when fast MA crosses above slow MA, sell on cross below. Emits SignalEvents
with a suggested stop-loss.

**Done when:** SMA crossover emits correct signals when fed historical bars.

---

## Phase 4 — Risk Manager & Portfolio
*The non-negotiable safety core. Most important phase. Take your time.*

| Module | File | Status |
|--------|------|--------|
| Risk manager + config | `apex/risk/risk_manager.py` | ✅ (now reduce-aware) |
| Portfolio tracker | `apex/risk/portfolio.py` | ✅ |
| Position sizer | (currently inside risk_manager) | ✅ |
| Risk manager tests | `tests/test_risk_manager.py` | ✅ (38 cases) |
| Portfolio tests | `tests/test_portfolio.py` | ✅ |

**Portfolio:** consumes FillEvents, tracks positions/cash/equity, computes
realized + unrealized P&L, peak equity, current drawdown, daily start equity.
Exposes a read-only snapshot to the risk manager (the closed loop). Marks
positions to market on each MarketEvent.

**Risk manager tests:** formalize the smoke test — compliant signal sized
correctly; missing stop rejected; oversized position capped; drawdown breach
halts; daily loss halt; whitelist enforcement; leverage cap; fail-closed on
malformed input.

**Done when:** full risk test suite green; drawdown breach demonstrably halts.

---

## Phase 5 — Execution Layer & Integration
*Connect approved orders to fills. Paper/live via one flag.*

| Module | File | Status |
|--------|------|--------|
| Base execution | `apex/execution/base_execution.py` | ✅ |
| Simulated execution | `apex/execution/simulated.py` | ✅ |
| Alpaca execution | `apex/execution/alpaca.py` | ✅ (DI seam; 17 tests) |
| Execution factory | `apex/execution/factory.py` | ✅ (paper+live Alpaca wired) |
| Main engine loop | `apex/execution/engine.py` | ✅ |
| Backtester + Gauntlet runner | `apex/backtest/` | ✅ |
| Run-once entry point | `scripts/run_once.py` | ✅ (cron cycle; 6 tests) |
| Integration tests | `tests/test_engine.py`, `tests/test_backtester.py`, `tests/test_alpaca_execution.py`, `tests/test_run_once.py` | ✅ |

**Simulated execution:** models fills using next-bar-open + configurable slippage
+ commission. Powers backtest and paper-fill simulation. Deterministic.

**Alpaca execution:** real submission via `alpaca-py`. Idempotency keys (no
double-submit). Partial fill handling. Disconnect → safe mode (cancel + halt).
Startup reconciliation against broker truth.

**Factory:** reads `APEX_MODE` + `APEX_BROKER`, returns the right engine. The
ONE place the paper/live decision is made.

**Engine loop:** the orchestrator. Wires feed → bus → strategy → risk →
execution → portfolio → back to risk. Catches strategy exceptions (quarantine,
don't crash). Honors HaltEvents.

**run_once.py:** what the GitHub Actions cron calls. Loads config, runs one
evaluation cycle (fetch latest bars → evaluate strategies → risk → execute →
persist state), exits cleanly. Idempotent.

**Done when:** flipping `APEX_MODE=paper`→`live` routes orders to Alpaca with
zero strategy changes; a full backtest runs end-to-end and prints a P&L report.

---

## Post-Phase-5: Going Live Checklist
- [ ] 30+ days of paper trading with Sharpe ≥ 1.0
- [ ] Paper results within ~80% of backtest projection
- [ ] `APEX_MODE=live`, `APEX_BROKER=alpaca`, live keys in env (never committed)
- [ ] Kill switch tested (set a halt env var, confirm orders blocked)
- [ ] First live order = smallest possible size; verify fill in Alpaca dashboard
- [ ] Start with capital you can afford to lose entirely

---

## The Validation Gauntlet (cross-cutting — the differentiator)
*The highest-leverage component. See docs/VALIDATION_GAUNTLET.md.*

| Module | File | Status |
|--------|------|--------|
| Performance metrics | `apex/validation/metrics.py` | ✅ tested |
| Monte Carlo resampling (Gate 4) | `apex/validation/monte_carlo.py` | ✅ tested |
| Walk-forward framework (Gate 3) | `apex/validation/walk_forward.py` | ✅ (needs backtester to plug in) |
| Gauntlet orchestrator (all 7 gates) | `apex/validation/gauntlet.py` | ✅ tested |
| Metrics tests | `tests/test_metrics.py` | ✅ |
| Monte Carlo tests | `tests/test_monte_carlo.py` | ✅ |
| Gauntlet tests | `tests/test_gauntlet.py` | ✅ |
| Cost-stress + param-sweep wiring | `apex/backtest/gauntlet_runner.py` | ✅ (backtester now feeds all 7 gates) |
| Drift monitor (live-vs-backtest) | `apex/validation/drift_monitor.py` | ✅ |

The statistical core is built and tested (28 tests passing). Gates 3, 5, and 6
have framework code that activates once the Phase 5 backtester exists to feed
them real per-window equity curves. Verified working: the Gauntlet correctly
passes a real edge (grade A) and kills an overfit mirage at Gate 2.

**Done when:** a strategy can be run through all 7 gates against the real
backtester and produce a graded report; the drift monitor auto-quarantines a
live strategy whose Sharpe decays below the floor.

---

## Post-Phase-5: Going Live Checklist

Before the first real-money trade:
