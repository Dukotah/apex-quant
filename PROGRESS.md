# Apex Quant — Progress Log

Running log so the owner can glance in anytime. Newest first.

## 2026-06-07 (cont.) — research buildout: trend craft, MCPT, 3 candidate sleeves

- Parallel agent fleet → integrated serially. **Full suite 820 green**, ruff clean, 90% cov.
  Branch `feat/research-buildout` (7 commits; allocator held for item 3).
- **Trend craft** (deployed strategy, additive/OFF by default): EWMA vol sizing + multi-speed
  barbell trend vote. Live behavior unchanged until A/B-validated.
- **MCPT** price-path-permutation test (`apex/validation/permutation.py`) — completes Gauntlet
  hardening; gauntlet wiring still to do.
- **3 candidate low-correlation sleeves drafted + tested (NOT validated):** turn-of-month,
  breadth-momentum (VAA), credit-spread regime. Plus bond-carry from earlier.
- **NEXT (heavy):** validate the new sleeves on real data through the Gauntlet + measure
  correlation to trend; verify ^TNX/^IRX & HYG/LQD feed plumbing first. Only uncorrelated
  Gauntlet-passers earn deployment; rest stay documented references.

## 2026-06-07 — research sweep + Gauntlet hardening (Gate 8: Deflated Sharpe)

- **6-agent research sweep, ~35 papers**, scored against our hard constraints. Verdict: trend +
  value are well-founded; biggest wins are rigor/craft, not new strategies. Owner greenlit four
  builds (Gauntlet hardening · trend craft · finish allocator · bond-carry edge) and a clear
  "don't build" list (BAB, dual-momentum/Faber=trend, low-vol, sell-in-May, MV optimization).
- **Shipped item 1: `apex/validation/overfitting.py` + Gate 8 (soft).** PSR / Deflated Sharpe /
  expected-max-Sharpe / Min Track Record Length (Bailey & López de Prado), wired as a WARN-only
  8th gate that reuses the Gate-6 sweep as its trial population. Deployed strategy stays grade A
  (DSR≈1). **710 tests green**, lint/format clean, coverage 89%. (MCPT price-shuffle = next.)
- **Parked (green where tested), to land in order:** allocator.py (F3.3 draft), bond_carry.py
  (+25 tests; verify ^TNX/^IRX feed scaling first), ops/alerts.py (+34 tests; needs run_once
  wiring), W9 coverage uplift (backtester/config/base_strategy/metrics → 100%).

## 2026-06-06 (cont.) — local dashboard + W8 finding

- **Local dashboard** (`scripts/dashboard.py`, stdlib only): `python -m scripts.dashboard` →
  http://127.0.0.1:8787 — live status + shipped phases + key results + recent log, auto-refresh.
  Smoke-tested end-to-end (HTTP 200, all sections render); pure `build_page` unit-tested.
- **W8 finding:** survivorship-free data isn't available free (Yahoo has no delisted tickers) →
  true W8 needs a paid source (owner spend decision). Free-tier stress evidence is the substitute.

## 2026-06-06 (cont.) — Phase F1/F2 MERGED + Phase F3: second edge proven

- **Phase F1/F2 shipped & merged** (PR #6, CI green): edge-validation suite, ops CLIs
  (status/preflight), local-CI parity, Bar invariant, walk-forward efficiency fix.
- **Phase F3 (branch `phase-3-second-edge`):**
  - **F3.1** added hysteresis to the value+momentum combo (also grade A) but **chose pure
    value** as the pairing — ¼ the turnover, and the combo's momentum reintroduces correlation
    to the deployed trend (defeats the purpose).
  - **F3.2 allocation backtest — DIVERSIFICATION WIN:** trend 0.82 + value 0.74 at corr
    **+0.24**; **20% value / 80% trend → Sharpe 0.99 (+0.17, +21%) with drawdown flat at 7%.**
    The value edge is a real, book-improving second edge.
- **Decision:** greenlight F3.3 (build the live ~20/80 allocation engine) but keep it gated
  behind W8 (survivorship-free validation) before any live value capital.
- Next: open the Phase F3 PR; then F3.3 build (engine) / W8 (the live gate).

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
