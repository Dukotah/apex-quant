# Apex Quant — Task Board

Live board. `todo` / `doing` / `done`. See `ROADMAP.md` for phases, `PROGRESS.md` for the log.

## Phase F1 — Validate & de-risk the value edge

| # | Task | Status |
|---|------|--------|
| F1.1 | Survivorship stress tool + auto-verdict + tests. | done |
| F1.2 | Temporal robustness tool (`scripts/temporal_robustness.py`) + tests. | done |
| F1.3 | Universe robustness tool (`scripts/universe_robustness.py`) + tests. | done |
| F1.4 | Written verdict in DECISIONS.md from F1.1–F1.3 evidence. | doing |

## Phase F2 — Operator experience & observability

| # | Task | Status |
|---|------|--------|
| F2.1 | Status CLI (`scripts/status.py`) + tests — reads live state DB. | done |
| F2.2 | Preflight health check (`scripts/preflight.py`) + tests. | done |
| F2.3 | Tighten alerts (actionable only + daily heartbeat). | todo |

## Improvements

| Task | Status |
|------|--------|
| Local CI parity (`make check` / `scripts/check.{sh,ps1}`) + README quickstart/operating. | done |
| `Bar.__post_init__` OHLC invariant (low ≤ open/close ≤ high). | doing (serial) |

## Phase F3 — Second edge → allocation engine *(gated on F1.4 verdict)*

| # | Task | Status |
|---|------|--------|
| F3.1 | Hysteresis on value+momentum combo; pick the stronger. | todo |
| F3.2 | Multi-strategy allocation backtest (trend + value, capital split). | todo |
