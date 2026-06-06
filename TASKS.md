# Apex Quant — Task Board

Live board. `todo` / `doing` / `done`. See `ROADMAP.md` for phases, `PROGRESS.md` for the log.

## Phase F1 — Validate & de-risk the value edge

| # | Task | Status |
|---|------|--------|
| F1.1 | Survivorship stress tool (`scripts/survivorship_stress.py`): delisting-hazard haircut, sweep + auto-verdict, +tests. | done |
| F1.2 | Temporal robustness tool (`scripts/temporal_robustness.py`): edge across sub-periods. | doing (agent) |
| F1.3 | Universe robustness tool (`scripts/universe_robustness.py`): edge across random subsets. | doing (agent) |
| F1.4 | Written verdict in DECISIONS.md from F1.1–F1.3 evidence. | todo |

## Phase F2 — Operator experience & observability

| # | Task | Status |
|---|------|--------|
| F2.1 | Status CLI (`scripts/status.py`): one-screen mode/halt/positions/equity/drawdown/gate. | doing (agent) |
| F2.2 | Preflight health check (`scripts/preflight.py`): go/no-go before a live session. | doing (agent) |
| F2.3 | Tighten alerts (actionable only + daily heartbeat). | todo |

## Improvements (in flight)

| Task | Status |
|------|--------|
| Local CI parity (`make check` / `scripts/check.{sh,ps1}`) + README quickstart/operating. | doing (agent) |
| `Bar.__post_init__` OHLC invariant (low ≤ open/close ≤ high). | todo (serial — high blast radius) |

## Phase F3 — Second edge → allocation engine *(gated on F1.4 verdict)*

| # | Task | Status |
|---|------|--------|
| F3.1 | Hysteresis on value+momentum combo; pick the stronger. | todo |
| F3.2 | Multi-strategy allocation backtest (trend + value, capital split). | todo |
