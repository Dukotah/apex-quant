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
| W8 | Survivorship-free (point-in-time) validation — the live-capital gate. | todo |
