# Apex Quant — Full Workload

My self-assigned plan from here to "winning" (see `VISION.md`). Ordered by execution
priority. Each item: **what · why · definition of done**. Status: `todo` / `doing` / `done` /
`blocked`. `TASKS.md` is the live phase board; `PROGRESS.md` is the running log.

> North star: ≥2 uncorrelated, survivorship-honest edges live with bulletproof risk, at $0
> infra. The framework is already functional — this is the climb from "works" to "wins."

---

## NOW — finish & ship Phase F1/F2 (one clean PR)

- **W1 · F1.4 verdict** — ✅ DONE (DECISIONS S28): real-enough-to-pursue; survivorship
  0.67@2%, temporal 16/17 yrs, universe 0.70/100%. Live gate stays on W8.
- **W2 · Gate-3 efficiency metric** — ✅ DONE: rewrote to OOS-Sharpe/IS-Sharpe (was an
  exploding return ratio); both strategies still grade A; +2 tests.
- **W3 · Ship Phase F1/F2 PR** — *doing.* branch `phase-1-edge-validation` → `main`. **DoD:**
  PR open with full summary (shipped / decided / next); CI green; no failing checks.

## NEXT — Phase F3: second edge → allocation *(gated on W1 verdict being positive)*

- **W4 · F3.1 hysteresis on value+momentum** — add `exit_rank_buffer` to ValueMomentumStrategy
  (mirror CrossAssetValue); re-run the single-name Gauntlet; pick the stronger of pure-value vs
  combo as the second-edge candidate. **DoD:** both measured, winner chosen + logged, tests.
- **W5 · F3.2 allocation backtest** — `scripts/allocate.py`: backtest a capital split between
  the deployed trend edge (smart-7) and the value edge (single-names), align daily returns,
  report combined Sharpe / correlation / drawdown vs each alone; sweep the split. Research
  tool, NOT wired live. **DoD:** combined vs standalone documented; pure split-math unit-tested.
- **W6 · F3.3 allocation engine (gated on W5 showing real lift)** — a deterministic, risk-aware
  multi-strategy allocator the engine can run; full Gauntlet on the blend. **DoD:** blend
  clears the Gauntlet; runs through a backtest with a clean split; correlation documented.

## THEN — Phase F2.3 + robustness hardening

- **W7 · F2.3 alerts** — make ntfy alerts actionable-only + a once-daily heartbeat so silence
  is meaningful. **DoD:** alert policy documented + tested with a fake notifier; no spam.
- **W8 · Survivorship-honest data path** — document (and if a free source exists, wire) a
  point-in-time constituents universe so single-name research isn't survivorship-blind. The
  decisive test the W1 verdict will flag as the real gate before deploying the value edge.
  **DoD:** either a working survivorship-free fetch or a crisp documented plan + the paid option.
- **W9 · Coverage uplift** — thin modules (`backtester` 62%, `base_strategy` 78%, `config`
  79%, `metrics` 81%). **DoD:** each ≥ 85%; suite green.

## ONGOING — operate

- **W10 · Watch the paper gate** — the deployed trend strategy is mid 30-day gate; monitor via
  `python -m scripts.status`. **DoD (owner call):** gate completes with live Sharpe tracking the
  ~0.82 backtest → then (and only then) the live-capital decision. Not autonomous.

---

## Operating rules I hold myself to
- Build must pass + tests green before any item is `done`. No broken commits.
- One clean PR per phase; never open a PR with failing checks.
- Verify agent output serially (the F1.2 temporal bug is why); heavy backtests stay with me.
- Log meaningful decisions in DECISIONS.md; keep this file, TASKS.md, PROGRESS.md honest.
- Don't build on an unproven edge — W4+ stay gated on the W1 verdict.
