# Apex Quant — Roadmap

> **Forward (founder) view first; the original Phase 1–6 build roadmap is preserved below as
> "Build history."** See `VISION.md` for the bar and what winning looks like, `TASKS.md` for
> the live board, `PROGRESS.md` for the running log.

Status legend: ✅ done · 🔲 to build · 🚧 in progress

## Where we are

The framework is **functionally complete and live on paper**. The forward work is no longer
"build the engine" — it is **prove an edge is real, make a second one, and operate cleanly.**

## Phase F1 — Validate & de-risk the value edge ✅ *(verdict: real-enough-to-pursue; live gate on W8)*

The S26 single-name value edge is grade A but on a **survivorship-biased** universe. Decide
whether it is likely real before spending another hour building on it.

- **F1.1** Survivorship stress tool: model the bias (delisted laggards Yahoo omits) as a
  delisting-hazard haircut on held names; sweep the hazard rate and report how the edge
  degrades. *Done when:* a tool + test exist and produce a hazard→Sharpe@2x curve.
- **F1.2** Temporal robustness: does the edge hold across sub-periods, or is it one regime?
- **F1.3** Universe robustness: re-run on a larger / shuffled / decile-shifted universe.
- **F1.4** Written verdict in `DECISIONS.md`: real-enough-to-pursue or survivorship mirage.
- **Definition of done:** a defensible, evidence-backed verdict; tooling reusable for any
  future single-name strategy; tests green.

## Phase F2 — Operator experience & observability

Make running the live system a glance, not a log dig.

- **F2.1** Unified status CLI (`python -m apex.status` or `scripts/status.py`): one command →
  mode, halt state, positions, equity, drawdown, paper-gate day N/30, drift vs backtest.
- **F2.2** Preflight health check (config, keys present, data fresh, broker reachable seam).
- **F2.3** Tighten alerts (only actionable ones; daily heartbeat).
- **Definition of done:** `status` gives a trustworthy one-screen read; tests green.

## Phase F3 — Second edge → allocation engine *(research PROVEN; live gate on W8)*

F1 verdict was positive, so:

- **F3.1** ✅ chose **pure value** as the second-edge candidate (hysteresis combo also grade A
  but ¼ the turnover loses, and the combo's momentum reintroduces trend-correlation).
- **F3.2** ✅ allocation backtest (`scripts/allocate.py`): **20% value / 80% trend → blend
  Sharpe 0.82 → 0.99 (+0.17) at correlation +0.24, drawdown flat at 7% — a diversification
  WIN.** The value edge is a real, book-improving second edge.
- **F3.3** 🔲 build the live, risk-aware multi-strategy allocator (~20/80). Gated on W8: do
  NOT fund the value sleeve live until survivorship-free validation clears.
- **Definition of done:** the blend clears the Gauntlet and runs through a backtest with a
  clean split; live wiring is config-gated and off until W8.

---

## IMPROVEMENTS *(found while operating — build the ones worth building)*

- [ ] **Survivorship-honest data path** — a free-ish point-in-time constituents source, or a
  documented paid one, so single-name research isn't survivorship-blind by default.
- [ ] **`Bar.__post_init__` invariant**: assert `low <= open/close <= high` (would have caught
  the Session-8 data-corruption bug at the source). Verify synthetic generators comply first.
- [ ] **Coverage uplift** on the thinnest modules (`backtester` 62%, `base_strategy` 78%,
  `config` 79%, `metrics` 81%).
- [ ] **Gate-3 walk-forward "efficiency" metric** reports anomalous values (e.g. 66, 397) —
  investigate the ratio (likely divide-by-near-zero in a window) and recalibrate or relabel.
- [ ] **Local dev parity**: a one-shot `make check` / `tox`-style command that runs the exact
  CI gates (ruff check, ruff format --check, pytest+cov) so CI never surprises us again.
- [ ] **Decimal `_ANN`/vol path**: the realized-vol path is intentionally float; document the
  Decimal/float boundary so it isn't "fixed" into a bug later.
- [ ] **README quickstart** for a cold-start operator (clone → install → run a Gauntlet →
  read status).

---

# Build history — original Phase 1–6 roadmap (complete)

The system was built in sequential phases. **All complete.** Kept for provenance.

---

## Phase 1 — Core Event System & Data Models
*The skeleton everything depends on. Get this perfect.*

| Module | File | Status |
|--------|------|--------|
| Data models | `apex/core/models.py` | ✅ |
| Event taxonomy | `apex/core/events.py` | ✅ |
| App config + MODE switch | `apex/core/config.py` | ✅ |
| Event bus | `apex/core/event_bus.py` | ✅ (pub/sub + queue; tested in test_core) |
| Clock (real + simulated) | `apex/core/clock.py` | ✅ (tested in test_core) |
| Tests for models/events/clock/bus | `tests/test_models_events.py`, `tests/test_core.py` | ✅ |

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
| Library: dual_momentum, rsi2_mean_reversion, rsi2_vol_filtered, etf_rotation, trend_bond | `apex/strategy/library/` | ✅ |
| Cross-sectional momentum (research; fails 2× cost, +0.76 corr to trend) | `apex/strategy/library/cross_sectional_momentum.py` | ✅ built, not deployed |
| Cross-asset value (research; UNCORRELATED corr +0.29 but too weak — Sharpe 0.30, edge<costs) | `apex/strategy/library/cross_asset_value.py` | ✅ built, not deployed |
| Value+momentum combo (research; S24 — WORSE than value, Sharpe 0.17, turnover-killed) | `apex/strategy/library/value_momentum.py` | ✅ built, not deployed |
| **DEPLOYED: multi_asset_trend** — 7-sleeve inverse-vol, grade A 7/7, OOS 1.34 | `apex/strategy/library/multi_asset_trend.py` | ✅ LIVE on paper |

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
| Risk manager + config | `apex/risk/risk_manager.py` | ✅ reduce-aware; drawdown throttle + vol-target overlays |
| Portfolio tracker | `apex/risk/portfolio.py` | ✅ + rolling realized-volatility |
| Position sizer | (inside risk_manager: cap × strength × throttle × vol-target) | ✅ |
| Risk manager tests | `tests/test_risk_manager.py` | ✅ (49 cases) |
| Portfolio tests | `tests/test_portfolio.py` | ✅ (23 cases) |

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

## Phase 6 — Live Operations & Strategy Expansion (current)
*Phases 1–5 are complete and the multi_asset_trend bot is LIVE on paper. This is the
real forward work, distilled from the DECISIONS log.*

| Item | Status | Notes |
|------|--------|-------|
| Drift monitor wired into run_once (auto-quarantine) | ✅ | blocks new entries below floor; Session 17 |
| ntfy push notifications (trade/halt/quarantine/kill) | ✅ | Session 17/21 |
| Manual kill switch (`APEX_HALT` env) | ✅ | emergency stop — blocks ALL orders; Session 21 |
| Paper-gate monitor (`scripts/report.py`) | ✅ | live P&L / rolling Sharpe / drift / gate progress; Session 21 |
| Drawdown sizing throttle | ✅ | Session 13 (dormant on this strategy — DD too shallow) |
| Volatility-target overlay | ✅ built, off | Session 18 (redundant — trend self-regulates) |
| Sleeve-screening tool | ✅ | Session 16 (`scripts/sleeve_screen.py`) |
| **30-day paper gate (Rule 17)** | 🚧 in progress | the real test — judge live, not on more backtests |
| **Second uncorrelated edge** | 🚧 mechanism FOUND, premium too weak | Session 22: cross-asset VALUE (long-horizon reversal) is the FIRST long-only driver that comes back UNCORRELATED to trend (corr +0.29) — the value/momentum thesis holds here. BUT standalone Sharpe 0.30 (edge<costs), so a blend puts 0% on it. Right mechanism, weak premium in 7 ETFs. **Probe (1) CLOSED ✗ (Session 23): value on the richer 13-ETF `sleeve_pool` DILUTES the premium (full Sharpe +0.30→−0.21, MC p 0.82 = noise) — currency/commodity-subsector/credit sleeves carry no value premium; the limit is asset KIND, not count. `validate_real.py value_pool`.** **Probe (2) CLOSED ✗ (Session 24): combined per-asset value+momentum (AQR combo) on smart-7 is WORSE than pure value — full Sharpe +0.30→+0.17, cost-stress −0.26, because the combined rank churns (908 trades vs 265, ~3.4×) and turnover eats the edge; Gate 6 robust + corr 0.18 but can't rescue a sub-cost sleeve. `apex/strategy/library/value_momentum.py`, `validate_real.py value_momentum`.** Probes (1)+(2) settle it: NO long-only 2nd edge exists in this 7-ETF universe (the cross-section is too small to harvest value net of costs). **Probe (3) — BREAKTHROUGH but UNPROVEN (Session 26): a large single-name cross-section (42 large-caps, 2005-2026) makes the value premium REAL — pure value clears ALL 7 Gauntlet gates at grade A (Sharpe@2x 0.70, OOS 0.68, MC p=0.000, beats SPY) ONCE hysteresis (new `exit_rank_buffer`, hold top-10 entries until they drop out of top-20) cuts the cost-killing turnover (981→147 trades). The combined value+momentum version gets the same real edge but still fails cost-stress (0.36@2x). `validate_real.py value_singlenames`. ⚠️ CRITICAL: the universe is SURVIVORSHIP-BIASED (Yahoo serves only survivors), so this is a STRONG CANDIDATE, NOT a deployable edge — must be re-validated on point-in-time (survivorship-free) constituents before any capital.** This confirms the diagnosis chain (cross-section was too small). The shorting route remains untried. |
| Multi-strategy capital-allocation engine | 🔲 deferred | the vehicle for a 2nd strategy — build ONLY once the Session-26 value edge is re-validated survivorship-free (still pending). |
| Govcon alt-data event-study pipeline | ✅ research, parked | `research/govcon/` — edge real but not capturable (Session 11) |

**Done when:** the paper gate completes with live Sharpe tracking the ~0.82 backtest;
a second genuinely-uncorrelated edge clears the Gauntlet and the allocation engine runs
both with a clean capital split.

---

## Post-Phase-5: Going Live Checklist
- [ ] 30+ days of paper trading with Sharpe ≥ 1.0  *(🚧 running — watch with `python -m scripts.report`)*
- [ ] Paper results within ~80% of backtest projection  *(tracked by the drift monitor)*
- [ ] `APEX_MODE=live`, `APEX_BROKER=alpaca`, live keys in env (never committed)
- [x] **Kill switch tested** — `APEX_HALT=1` blocks ALL orders (test_run_once); Session 21
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

The statistical core is built and tested (34 tests across metrics/Monte-Carlo/gauntlet,
plus drift_monitor and gauntlet_runner). All 7 gates are live: the Phase 5 backtester +
`gauntlet_runner` feed real per-window equity curves to Gates 3, 5, and 6. Verified
working: the Gauntlet correctly passes a real edge (grade A) and kills an overfit mirage
at Gate 2.

**Done when:** a strategy can be run through all 7 gates against the real
backtester and produce a graded report; the drift monitor auto-quarantines a
live strategy whose Sharpe decays below the floor.

---

## Post-Phase-5: Going Live Checklist

Before the first real-money trade:
