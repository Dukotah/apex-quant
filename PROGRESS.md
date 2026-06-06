# Apex Quant — Progress Log

Running log so the owner can glance in anytime. Newest first.

## 2026-06-06 (cont.) — F1.4 VERDICT + Phase F1/F2 ready to ship

- **VERDICT (F1.4): the value edge is real-enough-to-pursue, not a survivorship mirage.**
  Survivorship 0.67@2%/yr haircut · temporal 16/17 yrs positive (all regimes) · universe
  median 0.70 across 8 subsets, 100% pass · both strategies still grade A under the fixed
  efficiency metric. **GREENLIGHT F3 in research/backtest mode; live-capital gate stays
  BLOCKED on survivorship-free data (W8).** Build the vehicle, don't fund it yet.
- **W2 done** (efficiency fix verified: deployed eff 1.08, value eff 3.46, both grade A).
- Shipping the Phase F1/F2 PR next: full local gate → push → open PR with summary.

## 2026-06-06 (cont.) — full workload + evidence landing

- Drafted **WORKLOAD.md** (W1–W10, current state → "winning") and started working it.
- **W2 (Gate-3 efficiency fix):** the walk-forward "efficiency" was a return ratio
  (stitched cumulative OOS return / single in-sample window return) → it exploded to 66–397.
  Rewrote it as OOS-Sharpe / in-sample-Sharpe (bounded, scale-free; ~1 = edge holds, <0.5 =
  decay) + 2 unit tests. Pending a gauntlet regression check (deployed strategy must stay A).
- **F1.2 temporal (rewritten) result:** 2005–09 correctly flagged warmup; **16/17 active
  years (2010–2026) positive across GFC-recovery / 2018 / COVID / 2022 bear → VERDICT
  "consistent, not one-regime."** Strong evidence the value edge isn't a single-regime fluke.
- **F1.3 universe:** in flight (draw 0 → Sharpe@2× 0.69 on a 30-of-42 subset; robust so far).
- Next: regression gauntlets → F1.4 verdict → Phase F1/F2 PR.

## 2026-06-06 (cont.) — F1.1 result + parallel buildout

- **F1.1 survivorship stress tool shipped** (`scripts/survivorship_stress.py` + injection
  tests + auto-verdict). First read of the sweep: 0%/yr hazard → Sharpe@2× **0.70** (matches
  the grade-A baseline, sanity ✓); **2%/yr hazard (~12 of 42 names delisted at −80%, a heavy
  haircut) → median Sharpe@2× 0.67, still clearing the 0.50 cost bar** (min 0.37, pass 80%).
  Strong early "edge is probably real, not survivorship sugar" signal. Realistic-hazard
  re-run (0–3%/yr) in flight to pin the curve + verdict.
- **Parallelized the rest of the board** with a 5-agent workflow (F1.2 temporal robustness,
  F1.3 universe robustness, F2.1 status CLI, F2.2 preflight, local-CI+README), each on
  distinct new files with fast offline tests. Heavy backtests stay serial with me.
- Next: integrate agent output → full suite/lint/format green → F1.4 verdict → Phase F1 PR.

## 2026-06-06

- **Took ownership.** Wrote `VISION.md` (the read: framework functionally complete + live on
  paper; the decisive open question is whether the grade-A value edge is real or a
  survivorship mirage). Restructured `ROADMAP.md` into a forward founder view (Phases F1–F3 +
  IMPROVEMENTS) over the preserved Phase 1–6 build history. Created this log and `TASKS.md`.
- **Decision:** do NOT build the allocation engine next — it's the vehicle for an unproven
  edge. Attack survivorship risk first (Phase F1).
- **Working** on F1.1 — survivorship stress tool (delisting-hazard haircut on held names).
- Branch: `phase-1-edge-validation`. PR opens when F1 milestones land with green CI.

### Baseline carried in (prior sessions)
- 416 tests, 91% coverage, CI green, lint+format clean.
- Deployed multi-asset trend strategy: grade A, live on paper, ~mid 30-day gate.
- S26 value edge: grade A on a survivorship-biased single-name universe → must validate.
