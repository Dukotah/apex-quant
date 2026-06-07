# Apex Quant — Task Board

Live board. `todo` / `doing` / `done`. See `ROADMAP.md` for phases, `PROGRESS.md` for the log.

## Phase F1 — Validate & de-risk the value edge

| # | Task | Status |
|---|------|--------|
| F1.1 | Survivorship stress tool + auto-verdict + tests. | done |
| F1.2 | Temporal robustness tool (`scripts/temporal_robustness.py`) + tests. | done |
| F1.3 | Universe robustness tool (`scripts/universe_robustness.py`) + tests. | done |
| F1.4 | Verdict in DECISIONS.md (Session 28): real-enough-to-pursue; live gate on W8. | done |

## Phase F2 — Operator experience & observability

| # | Task | Status |
|---|------|--------|
| F2.1 | Status CLI (`scripts/status.py`) + tests — reads live state DB. | done |
| F2.2 | Preflight health check (`scripts/preflight.py`) + tests. | done |
| F2.3 | Tighten alerts (actionable only + daily heartbeat). | todo |

## Path A — Go-live hardening *(docs/ROADMAP-STRATEGIC.md NOW phase)*

| # | Task | Status |
|---|------|--------|
| NOW-1 | Complete & verify the 30-day paper gate (`scripts/report.py`). | doing (time-gated) |
| NOW-2 | Broker-reachability preflight (`check_broker_reachable` → `get_account`). | done |
| NOW-3 | Programmatic paper→live gate in `trade.yml` (report+preflight, fail non-zero). | done |
| NOW-4 | Live-risk config: `target_volatility` enabled (throttle kept at 0.12). | done |
| NOW-5 | Clean daily-loss baseline in `run_once` each cycle (`daily_open` table). | done |
| NOW-6 | HaltEvent cancels open broker orders (`cancel_open_orders` in engine contract). | done |
| NOW-7 | Broker-truth reconciliation diff + alert (block entries, allow exits). | done |
| NEXT-2 | Move StateStore OFF the public repo (hard blocker before live $). | todo |

## Improvements

| Task | Status |
|------|--------|
| Local CI parity (`make check` / `scripts/check.{sh,ps1}`) + README quickstart/operating. | done |
| `Bar.__post_init__` OHLC invariant (low ≤ open/close ≤ high). | done |
| W2: walk-forward efficiency = OOS/IS Sharpe (was an exploding return ratio). | done |

## Phase F3 — Second edge → allocation engine *(research proven; live gate on W8)*

| # | Task | Status |
|---|------|--------|
| F3.1 | Hysteresis on value+momentum; **chose pure value** (lower turnover, less trend-corr). | done |
| F3.2 | Allocation backtest: **20% value lifts blend Sharpe 0.82→0.99 at corr 0.24, DD flat**. | done |
| F3.3 | Build the live multi-strategy allocation engine (~20% value / 80% trend). | todo (gated on W8) |
| W8 | Survivorship-free validation — ⛔ needs PAID delisted data (owner spend decision, S30). | blocked |
