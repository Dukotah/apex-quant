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
│   │   ├── event_bus.py       🔲 Central event queue (Phase 1)
│   │   └── clock.py           🔲 Time abstraction: real vs simulated (Phase 1)
│   ├── data/
│   │   ├── base_feed.py       ✅ BaseDataFeed (ABC)
│   │   ├── historical_feed.py 🔲 CSV/Parquet replay for backtest (Phase 2)
│   │   ├── alpaca_feed.py     🔲 Live/paper data via Alpaca (Phase 2)
│   │   └── normalizer.py      🔲 Raw → Bar/Tick conversion (Phase 2)
│   ├── strategy/
│   │   ├── base_strategy.py   ✅ BaseStrategy (ABC) + StrategyContext
│   │   ├── indicators.py      🔲 SMA/EMA/RSI/MACD/BB/ATR, tested (Phase 3)
│   │   └── library/           🔲 Concrete strategies (specs written, impl pending)
│   │       ├── dual_momentum.py        📋 spec'd — THE ANCHOR (build first)
│   │       ├── rsi2_mean_reversion.py  📋 spec'd — tactical complement
│   │       ├── rsi2_vol_filtered.py    📋 spec'd — the improvement
│   │       └── etf_rotation.py         📋 spec'd — the diversifier
│   ├── risk/
│   │   ├── risk_manager.py    ✅ RiskManager + RiskConfig (the gatekeeper)
│   │   └── portfolio.py       🔲 Position/cash/equity/drawdown tracker (Phase 4)
│   └── execution/
│       ├── base_execution.py  ✅ BaseExecutionEngine (ABC)
│       ├── simulated.py       🔲 Paper fills w/ slippage + commission (Phase 5)
│       ├── alpaca.py          🔲 Live Alpaca execution (Phase 5)
│       ├── factory.py         🔲 MODE flag → right engine (Phase 5)
│       └── engine.py          🔲 Main orchestration loop (Phase 5)
├── tests/                     🔲 pytest suite, mirrors apex/ structure
├── config/                    ← YAML strategy/risk configs
├── docs/                      ← Phase specs and reference
├── scripts/
│   └── run_once.py            🔲 Entry point the cron runner calls (Phase 5)
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

See `ROADMAP.md` for the full plan. Quick status:

- **Phase 1 (Core):** Models, events, config ✅. Event bus + clock 🔲.
- **Phase 2 (Data):** Base class ✅. Concrete feeds 🔲.
- **Phase 3 (Strategy):** Base class ✅. Indicators + first strategy 🔲.
- **Phase 4 (Risk):** RiskManager ✅ (tested). Portfolio tracker 🔲.
- **Phase 5 (Execution):** Base class ✅. Simulated + Alpaca + engine loop 🔲.

---

## When Starting a Session

1. Read this file.
2. Read `DECISIONS.md` (what was decided previously — your external memory).
3. Read `ROADMAP.md` to find the next 🔲 item.
4. Read `SESSION_PLAYBOOK.md` for the exact prompt pattern for that item type.
5. Build **one module**, test it, update `DECISIONS.md` and `ROADMAP.md`, commit.

**Do not build more than one module per session.** Context drift causes
regressions. One module, tested, committed. Stop.

---

## Commit Convention
```
feat(phase-N): <module> — <short description>
fix(risk): <description>
test(strategy): <description>
docs: <description>
```
Never commit secrets. The pre-commit hook scans for them.
