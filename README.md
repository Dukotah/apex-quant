# ⚡ Apex Quant

> Event-driven, asset-agnostic algorithmic trading framework with immutable risk
> guardrails. Built to run entirely on free infrastructure — the only money spent
> is the capital you trade.

---

## The Core Idea

Every module communicates only through events. **Strategies cannot place orders** —
they only express intent. The Risk Manager is the sole producer of orders and
sits structurally in the path between intent and action, so it cannot be bypassed.

```
MarketEvent → SignalEvent → [RISK MANAGER] → OrderEvent → FillEvent
```

A reckless strategy literally cannot do damage, because it has no pathway to the
broker. That's the whole design.

---

## Start Here (for Claude in future sessions)

Read these four files, in order, before writing any code:

1. **`CLAUDE.md`** — master instructions, golden rules, architecture
2. **`DECISIONS.md`** — what's been decided so far (external memory)
3. **`ROADMAP.md`** — the 5-phase build plan with live status
4. **`SESSION_PLAYBOOK.md`** — exact prompts for building one module at a time

---

## The Free Stack (verified June 2026)

| Layer | Tool | Cost |
|-------|------|------|
| Broker + data | Alpaca (free paper, commission-free live, IEX data) | $0 |
| Scheduled runtime | GitHub Actions cron (unlimited on public repos) | $0 |
| Always-on runtime | Oracle Cloud Always-Free VM | $0 |
| State | SQLite (in-repo) or Supabase free tier | $0 |
| Strategy authoring | Claude Pro chat → paste into repo | included |

See `docs/HOSTING.md` for setup of each runtime option.

---

## The Live/Paper Switch

One environment variable controls everything:

```bash
APEX_MODE=backtest   # historical replay, no broker
APEX_MODE=paper      # live data + simulated fills (no real money) ← default
APEX_MODE=live       # real broker, REAL MONEY (after 30+ days proven paper)
```

No strategy, risk, or data code changes when you switch. Ever.

---

## Build Status

| Phase | Module | Status |
|-------|--------|--------|
| 1 | Core models, events, config | done |
| 1 | Event bus, clock | todo |
| 2 | Data feeds (base done) | todo |
| 3 | Indicators + strategies (base done) | todo |
| 4 | Risk manager (done, tested) / portfolio | in progress |
| 5 | Execution (base done) / engine loop | todo |

The four base classes are complete and the Risk Manager is smoke-tested:
compliant signals get sized correctly, missing stops get rejected, and a
drawdown breach halts the whole system.

---

## Running

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in Alpaca paper keys
pytest tests/ -v            # run the suite (grows each session)
python -m scripts.run_once  # one trading cycle (stub until Phase 5)
```

---

## Disclaimer

Educational/personal use. Not investment advice. Trading carries substantial risk
of loss. Paper-trade for 30+ days before risking real capital, and only risk what
you can afford to lose.
