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
| 1 | Event bus, clock | done |
| 2 | Data feeds (base, historical, Alpaca, normalizer) | done |
| 3 | Indicators + strategies | done |
| 4 | Risk manager / portfolio | done |
| 5 | Execution / engine loop / backtester / run_once | done |
| 6 | Live ops (drift monitor, kill switch, paper-gate report, preflight, interactive web app) | done |

All six build phases are code-complete with **2,500+ tests passing**. The multi-asset
trend strategy (7-sleeve inverse-vol, Gauntlet grade A 7/7) is **live on paper** via a
GitHub Actions cron against an Alpaca paper account. The mandatory 30-day paper gate is
in progress before any live capital.

---

## Quickstart

```bash
# 1. Clone and create a virtual environment
git clone https://github.com/<you>/apex-quant.git
cd apex-quant
python -m venv .venv

# 2. Activate (Linux/macOS/WSL)
source .venv/bin/activate
# Activate (Windows PowerShell)
.venv\Scripts\Activate.ps1

# 3. Install runtime + dev dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# 4. Copy the env template and fill in your Alpaca paper keys
cp .env.example .env

# 5. Run the full Gauntlet on real data (downloads data first time)
#    The "smart7" preset is the deployed 7-sleeve inverse-vol trend strategy.
python -m scripts.validate_real smart7

# 6. Run all CI gates locally (lint, format-check, test)
make check
# or without make:
bash scripts/check.sh    # Linux / macOS / WSL
pwsh scripts/check.ps1   # Windows PowerShell
```

---

## Operating

Once the system is running in paper mode, these scripts let you watch it without
touching the broker:

| Script | What it does |
|--------|-------------|
| `python -m scripts.report` | Paper-gate monitor — rolling Sharpe, drawdown, and 30-day gate progress vs. the validated backtest baseline. Run this daily. |
| `python -m scripts.status` | Quick health check — prints the last run timestamp, current equity, and any active halt state from the SQLite state DB. |
| `python -m scripts.preflight` | Pre-flight check — validates env vars, broker connectivity, and config before the cron fires (run this once after setup). |
| `python scripts/webapp.py` | Interactive web app — explore strategies, run the Gauntlet on any strategy from the browser, view a live system overview. Visit http://localhost:8000 after starting. |

**Environment variables that control runtime behaviour:**

| Variable | Values | Default | Effect |
|----------|--------|---------|--------|
| `APEX_MODE` | `backtest` / `paper` / `live` | `backtest` | Selects the execution engine. `paper` = live data + simulated fills, no real money. `live` = real broker, real money (requires 30+ paper days first). |
| `APEX_BROKER` | `simulated` / `alpaca` / `ibkr` | `simulated` | Broker adapter. Must match `APEX_MODE` (live mode rejects `simulated`). |
| `APEX_HALT` | `1` / `true` / `yes` / `on` | *(unset)* | Emergency kill switch. When set, the next `run_once` cycle blocks ALL new orders — a human override with zero code changes. |
| `APEX_CAPITAL` | any decimal | `100000` | Starting capital in USD for backtest / paper modes. |

The typical paper-trading cron invocation (what `.github/workflows/trade.yml` runs):

```bash
APEX_MODE=paper APEX_BROKER=alpaca python -m scripts.run_once
```

To halt immediately without touching the scheduler, set `APEX_HALT=1` in your
environment or GitHub Actions secret — the next cycle exits without submitting
any orders. Unset it to resume.

---

## Disclaimer

Educational/personal use. Not investment advice. Trading carries substantial risk
of loss. Paper-trade for 30+ days before risking real capital, and only risk what
you can afford to lose.
