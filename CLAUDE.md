# APEX QUANT — Master Instructions for Claude

> **Read this file first, every session, before writing any code.**
> It is the single source of truth for how this project is built.

---

## What This Is

Apex Quant is a **strictly decoupled, event-driven, asset-agnostic algorithmic
trading framework in Python**. It is designed so that strategies can be
generated, plugged in, backtested, and executed — with **deterministic risk
guardrails that are structurally impossible to bypass.**

The defining architectural principle: **strategies cannot place orders.** They
can only express intent (a `SignalEvent`). The `RiskManager` is the *only*
producer of `OrderEvent`s, and it sits physically in the path between intent
and action. A buggy or reckless strategy literally cannot do damage because it
has no pathway to the broker.

This runs entirely on **free tiers** — the only money spent is trading capital.

---

## The Two Sibling Projects (don't confuse them)

| Repo | What it is | Stack |
|------|-----------|-------|
| **apex-quant** (this) | The Python *engine* — strategies, backtest, risk, execution | Python |
| **apex-trader** | The Next.js *platform* — UI, auth, dashboards, multi-user | TypeScript / Vercel |

They connect later via API. This repo is the brain; that one is the control surface.
**This file governs apex-quant only.** If you're asked to do UI/auth/dashboard
work, that belongs in apex-trader.

---

## THE GOLDEN RULES (never violate)

### Architecture
1. **Modules communicate ONLY through events.** No module imports another's
   internals. Data flows: `MarketEvent → SignalEvent → [RiskManager] → OrderEvent → FillEvent`.
2. **Strategies emit signals, never orders.** A strategy has no broker reference
   and no way to size or place a trade. It returns `SignalEvent`s. Period.
3. **The RiskManager is immutable and non-subclassable by strategies.** There is
   exactly one risk policy. It cannot be weakened, overridden, or skipped.
4. **All data models are frozen (immutable).** A `Bar`, once created, is a fact.
5. **The Live/Paper switch is config-only.** Going from paper to live is changing
   `APEX_MODE`. No strategy, risk, or data code changes — ever.

### Risk (the whole point of the system)
6. **Risk checks FAIL CLOSED.** If a check errors, the signal is REJECTED. The
   default outcome of any uncertainty is "no trade," never "trade."
7. **Every order carries a mandatory stop-loss.** No stop = no order.
8. **Max drawdown breach halts the ENTIRE system** via a `HaltEvent`.
9. **Risk parameters load once at startup from a frozen config.** Nothing mutates
   limits at runtime.

### Code quality
10. **Determinism is sacred.** Same inputs → same outputs, every time. This is
    what makes backtest results trustworthy and backtest/live parity possible.
    No `datetime.now()` in logic — use the injected `Clock`. No randomness without
    a seeded RNG.
11. **No I/O inside strategy logic.** No network, no file reads, no `print()`.
    Use the logger. Strategies are pure functions of their inputs.
12. **Every module ships with tests.** Indicator math is tested against
    known-correct values. A module isn't done until its test is green.
13. **Type hints everywhere.** Use `from __future__ import annotations`.
14. **Decimal for money, never float.** Floating-point rounding errors in P&L
    are unacceptable. All prices/quantities/cash are `decimal.Decimal`.

### Safety
15. **Live mode requires explicit opt-in.** `APEX_MODE=live` must be paired with a
    real broker; the config raises if you try to run live against the simulator.
16. **Secrets come from environment variables only.** Never hardcode an API key.
    Never commit `.env`. The `.gitignore` enforces this.
17. **Paper-trade a strategy 30+ days before considering live.** Backtest success
    is necessary but not sufficient — overfitting is the #1 killer.

---

## Repository Layout

```
apex-quant/
├── CLAUDE.md                  ← You are here. Read first.
├── DECISIONS.md               ← Running log of design decisions. Update every session.
├── SESSION_PLAYBOOK.md        ← How to run each build session (paste prompts).
├── ROADMAP.md                 ← The 5-phase build plan with status.
├── README.md                  ← Human-facing project overview.
├── requirements.txt           ← Python dependencies (all free).
├── pyproject.toml             ← Package config + tooling (ruff, pytest, mypy).
├── .env.example               ← Env var template. Copy to .env (gitignored).
├── .gitignore
├── apex/
│   ├── core/
│   │   ├── models.py          ✅ Bar, Tick, Symbol, Position (asset-agnostic, frozen)
│   │   ├── events.py          ✅ Market/Signal/Order/Fill/Halt events (frozen)
│   │   ├── config.py          ✅ AppConfig + the MODE switch
│   │   ├── event_bus.py       ✅ Central event queue (Phase 1)
│   │   └── clock.py           ✅ Time abstraction: real vs simulated (Phase 1)
│   ├── data/
│   │   ├── base_feed.py       ✅ BaseDataFeed (ABC)
│   │   ├── historical_feed.py ✅ CSV/Parquet replay for backtest (Phase 2)
│   │   ├── alpaca_feed.py     ✅ Live/paper data via Alpaca (injectable fetcher)
│   │   └── normalizer.py      ✅ Raw → Bar/Tick conversion (tested)
│   ├── strategy/
│   │   ├── base_strategy.py   ✅ BaseStrategy (ABC) + StrategyContext
│   │   ├── indicators.py      ✅ SMA/EMA/RSI/MACD/BB/ATR, tested (Phase 3)
│   │   └── library/           ✅ Strategies (one deployed + research references)
│   │       ├── multi_asset_trend.py        ✅ DEPLOYED — 7-sleeve inverse-vol trend (grade A)
│   │       ├── dual_momentum.py            ✅ built, tested (research reference)
│   │       ├── rsi2_mean_reversion.py      ✅ built, tested (research reference)
│   │       ├── rsi2_vol_filtered.py        ✅ built, tested (research reference)
│   │       ├── etf_rotation.py             ✅ built, tested (research reference)
│   │       ├── trend_bond.py               ✅ built, tested (research reference)
│   │       ├── cross_sectional_momentum.py ✅ research — correlated to trend
│   │       ├── cross_asset_value.py        ✅ research — uncorrelated but too weak
│   │       ├── short_term_reversal.py      ✅ research — fails long-only
│   │       └── value_momentum.py           ✅ research — value+momentum combo (turnover-killed)
│   ├── risk/
│   │   ├── risk_manager.py    ✅ RiskManager + RiskConfig (the gatekeeper)
│   │   └── portfolio.py       ✅ Position/cash/equity/drawdown tracker (Phase 4)
│   ├── validation/           ✅ THE GAUNTLET — see docs/VALIDATION_GAUNTLET.md
│   │   ├── metrics.py         ✅ Sharpe/Sortino/DD/PF/correlation (tested)
│   │   ├── monte_carlo.py     ✅ Gate 4: edge-vs-luck test (tested)
│   │   ├── walk_forward.py    ✅ Gate 3: rolling OOS validation
│   │   └── gauntlet.py        ✅ 7-gate orchestrator + grading (tested)
│   └── execution/
│       ├── base_execution.py  ✅ BaseExecutionEngine (ABC)
│       ├── simulated.py       ✅ Paper fills w/ slippage + commission
│       ├── alpaca.py          ✅ Live Alpaca execution (injectable BrokerClient)
│       ├── factory.py         ✅ MODE flag → right engine (paper+live wired)
│       └── engine.py          ✅ Main orchestration loop
├── tests/                     ✅ pytest suite, mirrors apex/ structure (414 tests passing)
├── config/                    ← YAML strategy/risk configs
├── docs/                      ← Phase specs and reference
├── scripts/
│   └── run_once.py            ✅ Entry point the cron runner calls (tested)
└── .github/workflows/
    └── trade.yml              ✅ Free 24/7 scheduled runner (GitHub Actions cron)
```

Legend: ✅ built · 🔲 to build

---

## The Free Stack (verified June 2026)

| Layer | Tool | Cost | Notes |
|-------|------|------|-------|
| Broker + data | **Alpaca** | $0 | Free paper trading, commission-free live, $0 min deposit. Paper uses free IEX data feed. PDT rules dropped June 4 2026. |
| Runtime (scheduled) | **GitHub Actions** | $0 | Unlimited minutes on public repos. Cron-triggered runs. NOT for infinite loops. |
| Runtime (always-on) | **Oracle Cloud Always Free** | $0 | True 24/7 ARM VM if you need a persistent process. |
| State/DB | **SQLite** (or Supabase free) | $0 | SQLite file in repo is simplest; Supabase if you want the dashboard. |
| Strategy authoring | **Claude Pro chat** | included | Generate strategies in chat, paste into repo. Avoids paid API. |
| Backtesting | **vectorbt** / built-in engine | $0 | Open source. |

**Total recurring cost: $0.** Only capital at risk is what you fund.

---

## How Data Flows (memorize this)

```
DataFeed.stream() ──emits──> MarketEvent
                                  │
                                  ▼  (engine routes to strategies)
Strategy.on_bar() ──returns──> [SignalEvent, ...]
                                  │
                                  ▼  (engine hands each to risk manager)
RiskManager.evaluate(signal, portfolio)
        ├── any check fails ──> None  (signal discarded, logged)
        └── all checks pass ──> OrderEvent (approved + sized + stop attached)
                                  │
                                  ▼
ExecutionEngine.submit_order() ──emits──> FillEvent
                                  │
                                  ▼
Portfolio.on_fill() ──updates──> positions, cash, equity, drawdown
                                  │
                                  └──feeds equity/drawdown back to──> RiskManager
```

The loop is closed: the portfolio's equity feeds the risk manager's drawdown
check, so the system self-regulates.

---

## Building New Strategies (the safe, repeatable pattern)

**Before building any strategy, read `docs/STRATEGY_PLAYBOOK.md`.** It contains
the researched, documented starter strategies (Dual Momentum, RSI(2) mean
reversion, vol-filtered RSI(2), ETF rotation), what this architecture is good and
bad at, the alpha-decay lessons, and the backtest validation gates every strategy
must clear. The four starters already have spec stubs in `apex/strategy/library/`
with full rules in their docstrings — implement those before inventing new ones.

A strategy is the ONLY thing you'll add frequently. Every strategy:
1. Subclasses `BaseStrategy`.
2. Implements `on_bar(self, bar) -> List[SignalEvent]` (and/or `on_tick`).
3. Reads indicators from `apex.strategy.indicators` (never recomputes inline).
4. Reads state via `self.context` (read-only — look, don't touch).
5. Returns signals with a `strength` (0..1 conviction) and a `reason` string.
6. Suggests a stop-loss (`suggested_stop_loss`) — the risk manager validates it.
7. Has a matching test that feeds it known bars and asserts the expected signals.

Strategies NEVER: place orders, size positions, touch the broker, do I/O,
use wall-clock time, or use unseeded randomness.

---

## Current Build Status

See `ROADMAP.md` for the full plan. Quick status (**414 tests passing**):

- **Phase 1 (Core):** Models, events, config, event bus, clock ✅.
- **Phase 2 (Data):** Base, historical feed, Alpaca feed, normalizer ✅.
- **Phase 3 (Strategy):** Base, indicators, 10 library strategies + SMA ref ✅.
- **Phase 4 (Risk):** RiskManager (reduce-aware) + Portfolio ✅.
- **Phase 5 (Execution):** Simulated + Alpaca execution, factory (paper+live),
  engine loop, backtester, `run_once` cron cycle ✅.
- **Phase 6 (Live ops):** drift monitor, kill switch, ntfy alerts, paper-gate report ✅.

All five build phases are code-complete; the Alpaca adapters are verified against live
**paper keys** and the GitHub Actions cron is wired and running GREEN. The multi-asset
trend strategy (7-sleeve inverse-vol, Gauntlet grade A) is **LIVE on paper**. The sole
remaining gate before live capital is the mandatory 30-day paper period (rule 17), in
progress — watch it with `python -m scripts.report`. The long-only second-edge hunt has
concluded that trend is the sole deployable edge in this universe. See `DECISIONS.md`
(newest entries on top).

---

## When Starting a Session

1. Read this file.
2. Read `DECISIONS.md` (what was decided previously — your external memory).
3. Read `ROADMAP.md` to find the next 🔲 item.
4. Read `SESSION_PLAYBOOK.md` for the exact prompt pattern for that item type.
5. Build **one module**, get `make check` green (see **Definition of Done**), update `DECISIONS.md` and `ROADMAP.md`, commit.

**Do not build more than one module per session.** Context drift causes
regressions. One module, tested, committed. Stop.

---

## Definition of Done (close the loop — never skip)

A module is **not done** until the exact CI gate passes locally. Run:

```
make check          # ruff check + ruff format --check + pytest (the 3 CI gates, fail-fast)
```

- If formatting fails, run `make fmt` then re-run `make check`.
- `make test` alone runs just the suite; `make check` is what CI enforces, so it's the real bar.
- **Read before you guess:** if you're unsure how an existing module behaves, read it (and its test) — never assume an API, signal shape, or event field.
- **Fix the root cause, not the symptom.** A failing test means the design or the code is wrong — don't loosen the assertion to make it green.
- Don't report a module finished, or commit, until `make check` is green. If it fails, say so with the output.

> Worked example of one clean session:
> ```
> task: build the ATR-based position sizing helper
> you:  read indicators.py + its test to match style → implement → write its test
>       → run `make check` → 1 ruff error, fix it → `make check` green (417 passing)
>       → update DECISIONS.md + ROADMAP.md → commit. Done. Stop.
> ```

---

## Commit Convention
```
feat(phase-N): <module> — <short description>
fix(risk): <description>
test(strategy): <description>
docs: <description>
```
Never commit secrets. The pre-commit hook scans for them.
