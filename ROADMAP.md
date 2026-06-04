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
| Historical replay (CSV/Parquet) | `apex/data/historical_feed.py` | 🔲 |
| Alpaca live/paper feed | `apex/data/alpaca_feed.py` | 🔲 |
| Source normalizer | `apex/data/normalizer.py` | 🔲 |
| Tests | `tests/test_historical_feed.py` | 🔲 |

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
| Indicator library | `apex/strategy/indicators.py` | 🔲 |
| Reference: SMA crossover | `apex/strategy/library/sma_crossover.py` | 🔲 |
| Indicator tests | `tests/test_indicators.py` | 🔲 |
| Strategy tests | `tests/test_sma_crossover.py` | 🔲 |

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
| Risk manager + config | `apex/risk/risk_manager.py` | ✅ (smoke-tested) |
| Portfolio tracker | `apex/risk/portfolio.py` | 🔲 |
| Position sizer | (currently inside risk_manager) | ✅ |
| Risk manager tests | `tests/test_risk_manager.py` | 🔲 |
| Portfolio tests | `tests/test_portfolio.py` | 🔲 |

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
| Simulated execution | `apex/execution/simulated.py` | 🔲 |
| Alpaca execution | `apex/execution/alpaca.py` | 🔲 |
| Execution factory | `apex/execution/factory.py` | 🔲 |
| Main engine loop | `apex/execution/engine.py` | 🔲 |
| Run-once entry point | `scripts/run_once.py` | 🔲 |
| Integration test | `tests/test_integration.py` | 🔲 |

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

Before the first real-money trade:
- [ ] 30+ days of paper trading with Sharpe ≥ 1.0
- [ ] Paper results within ~80% of backtest projection
- [ ] `APEX_MODE=live`, `APEX_BROKER=alpaca`, live keys in env (never committed)
- [ ] Kill switch tested (set a halt env var, confirm orders blocked)
- [ ] First live order = smallest possible size; verify fill in Alpaca dashboard
- [ ] Start with capital you can afford to lose entirely
