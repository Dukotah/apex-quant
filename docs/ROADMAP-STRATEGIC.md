<!-- Generated 2026-06-07 by a 13-agent research+synthesis workflow (codebase survey + web best-practice research + adversarial code-grounded critique). Plan of record; reconcile against DECISIONS.md as work lands. -->

# Apex Quant — Forward Strategic Roadmap

*Lead Architect plan of record. Synthesizes a fleet survey of the codebase, production-trading best-practice research, and an adversarial code-grounded review. This is an execution plan, not a survey. Every claim below has been reconciled against `DECISIONS.md` and the actual source tree as of June 2026.*

---

## Where We Are / What Winning Looks Like

**Where we are (reality, June 2026).**

- The **framework bar is met.** Event-driven, asset-agnostic, structurally fail-closed. Strategies emit `SignalEvent`s; the immutable `RiskManager` is the SOLE producer of `OrderEvent`s. Decimal money, frozen models, deterministic backtest, config-only paper→live switch. ~1070 tests, 92% coverage. $0 infra.
- **Edge #1 (Trend) is LIVE ON PAPER.** 7-sleeve inverse-vol multi-asset trend, Gauntlet grade A (full Sharpe 0.82, OOS 1.34, 7/7 gates), MCPT-confirmed. Deployed via GitHub Actions cron on Alpaca paper. **In the mandatory 30-day paper gate.** This is the critical path to the first live dollar.
- **Edge #2 (Value) is grade A but GATED.** Cross-asset single-name value (42 names + hysteresis), genuinely uncorrelated to trend (+0.24), lifts book Sharpe 0.82 → 0.99 at a 20/80 blend with flat drawdown. **Blocked on W8 survivorship-free validation** — Yahoo serves survivors only, which is maximally dangerous for a buy-the-laggard strategy. Per Session 30, a *true* survivorship-free backtest requires a **paid** delisted-securities dataset; the free path is already documented as a dead end. This is the critical path to the second edge.
- **A rich built-but-gated capability surface exists:** live capital allocator (gated `allocation=None`), Alpaca crypto (failed Gauntlet), long/short (`allow_short=False`, no margin plumbing), defined-risk options. **Options now route through the RiskManager on the risk side** — `evaluate_option` + `OptionOrderEvent` were built in Session 31 — but the **engine-routing + Portfolio-tracking half is still open** (`OptionOrderEvent` is not handled in `engine.py`; `Portfolio` has no options/Greeks tracking). EWMA-vol/barbell trend craft (grade A, awaiting A/B swap).
- **The Gauntlet is the moat.** A 7-gate validation harness (Sharpe/Sortino/DD, walk-forward, Monte Carlo, MCPT permutation, cost-stress at 2×, beats-SPY) + soft Gate 8 (Deflated Sharpe). The product's differentiator is research discipline and bulletproof risk, not spend.

**What winning looks like.**

1. **≥2 genuinely uncorrelated edges live**, each independently Gauntlet-validated and survivorship-honest, combined by the capital allocator at a clean split.
2. **Risk that has been *exercised* in production**, not just unit-tested — a real drawdown halts cleanly, kill switch flattens, reconciliation never desyncs.
3. **A fast, honest research loop** that structurally resists every known backtest lie (overfit, look-ahead, survivorship, cost).
4. **$0 infra, compounding capital.** The edge is discipline and risk, not money spent — with one deliberate, bounded exception: paying for survivorship-honest data if (and only if) W8 evidence justifies funding the value sleeve.

**The two critical paths.** Everything below sequences around these:
- **Path A → Live capital:** finish paper gate → harden the paper→live switch & live-risk config → move state off the public repo → pilot ramp at 10–25%.
- **Path B → Second edge:** exhaust the *free* survivorship-stress evidence → make the explicit pay-for-data decision → clear paid W8 → flip value `funded=True` → activate the allocator at ~80/20.

---

## Golden-Rule & Care Flags

Items touching the **immutable RiskManager** or **real capital** carry a ⚠️ and require a dedicated session, an explicit DECISIONS.md entry, and no shortcuts to the one-module-per-session discipline. The single most dangerous *remaining* issue is the **options engine-routing gap** — flagged 🛑 wherever it appears. Note: the options *risk-approval* path is already built; what is open is engine handling of `OptionOrderEvent` and Portfolio Greeks/position tracking. Do not re-build the risk side.

---

## PHASE: NOW (0–1 month) — Close the paper gate, harden the live switch

> Theme focus: **Go-Live** and **Operability**. Nothing here requires spend. This phase is the critical path to the first live dollar.

| ID | Item | Rationale | Deps | Effort | Impact | Gating | Definition of Done |
|---|---|---|---|---|---|---|---|
| **NOW-1** | Complete & verify the 30-day paper gate | The mandatory Rule-17 gate; first live dollar is blocked on it | none | — | High | **time** (30 cycles) | `scripts/report.py` verdict reads "GATE PASSED"; live Sharpe tracking ~0.82 floor; logged in DECISIONS.md |
| **NOW-2** ⚠️ | Broker-reachability preflight (`check_broker_reachable`) | `preflight.py` has **no** `get_account()` call today; a stale/revoked key fails mid-cycle *after* reconciliation, persisting a half-state. This must exist before NOW-3 can gate on it | none | S | High | none | `preflight.py` gains `check_broker_reachable()` calling `get_account()`; mandatory workflow step blocks trade on FAIL |
| **NOW-3** ⚠️ | Programmatic paper→live gate in `trade.yml` | Today the live guard is **only** a credential-presence check (`-z $ALPACA_API_KEY`) that prints a `::warning::` and continues — a misclick on `APEX_MODE=live` routes live orders with no gate check | NOW-1, NOW-2 | S | High | none | Workflow runs `report.py` + `preflight.py` (incl. broker reachability) and **fails non-zero** if `APEX_MODE=live` and gate not passed |
| **NOW-4** ⚠️ | Set live-risk config: `drawdown_throttle_start`, `target_volatility` | Both controls are BUILT but disabled — system runs full-size at 9% DD then hard-halts at 10% | none | S | High | none | Live `RiskConfig`: throttle_start≈0.05, target_vol≈0.12; tests assert non-None in live profile |
| **NOW-5** ⚠️ | Establish a clean daily-loss baseline in `run_once` each cycle | `reset_daily()` already exists in the backtester engine; the real risk is in the **cron path**: `run_once` seeds `day_start_equity` from broker equity at reconciliation, so a mid-session cron fire after a loss can inherit a **stale, already-down baseline** persisted in SQLite, silently shrinking room before halt | none | M | High | none | Test proves `run_once` sets `day_start_equity` from today's *opening* equity, not yesterday's close or an intra-day post-loss value; invariant holds across a simulated mid-session restart |
| **NOW-6** ⚠️🛑 | Confirm HaltEvent cancels open broker orders | A halted system with resting orders still has live exposure | none | S | High | none | On halt, engine calls `cancel_open_orders()`; integration test asserts zero open orders post-halt |
| **NOW-7** | Broker-truth reconciliation diff + alert | Reconcile seeds positions but never *diffs* vs broker; a dropped FillEvent silently oversizes | NOW-2 | M | High | none | `run_once` diffs `Portfolio` vs Alpaca positions; discrepancy > $1 alerts + blocks new entries |
| **NOW-8** | Document intraday-stop & capital-ramp policy | Daily cron submits no live stops; the ramp schedule is the #1 discipline step retail skips | none | S | Med | none | DECISIONS.md entry: stop = next cycle + 40% catastrophe halt accepted; Pilot/Scale-1/Full ramp formalized |

**Critical-path note:** NOW-1 → NOW-2 → NOW-3, plus NOW-4..NOW-7, are the hard gate between paper and the first pilot dollar. None may be skipped. NOW-9 (MCPT wiring) from the draft is **removed — already complete** (`gauntlet_runner.run_mcpt`, `tests/test_mcpt_wiring.py`); see Anti-Goals. The public-repo state-leak (formerly NOW-8) is **promoted to a blocker on NEXT-1** and reframed below — it is owner-action with a real tradeoff, not a NOW build session.

---

## PHASE: NEXT (1–3 months) — Pilot live capital + exhaust the free W8 evidence

> Theme focus: **Go-Live (pilot ramp)**, **Data/Validation (free W8)**, **Operability**.

| ID | Item | Rationale | Deps | Effort | Impact | Gating | Definition of Done |
|---|---|---|---|---|---|---|---|
| **NEXT-1** ⚠️ | Pilot live at 10–25% of intended book | Discover live-only failure modes (rejects, buying-power, fills) before full capital | NOW-1..NOW-8, **NEXT-2 (state off public repo)** | M | High | **owner-action** (fund account) | 30 days live pilot; DD < 5%; live slippage within 150% of model; zero halt events; logged. **Blocker: no live capital until state is off the public repo** |
| **NEXT-2** ⚠️ | Move state off the public repo (with tradeoff decision) | The git commit-back step (`git add -A state/ logs/`) pushes exact positions/equity/orders to a **public** repo every cron cycle — a live front-running/information-leak. But making the repo private burns paid Actions minutes (breaks $0-infra). This is a strategic tradeoff, not a slot-in build | none | M | High | **owner-action** (private store) | DECISIONS.md logs the tradeoff (private repo → paid Actions minutes **vs.** redirect state to private Gist/Supabase free tier); state store is provably **not** in the public repo before any live capital |
| **NEXT-3** | Shadow-trade logging (live + paper in parallel) | Catches slippage creep & divergence early; trivial given paper engine is wired | NOW-1 | S | Med | none | Every live signal also booked on paper; daily live-vs-shadow delta logged; 2σ divergence alerts |
| **NEXT-4** | Per-sleeve P&L attribution in `report.py` | A 7-sleeve book can hide a failing sleeve behind an OK aggregate | none | M | High | none | Report shows per-sleeve realized P&L, trades, win-rate (grouped by `strategy_id`+symbol) |
| **NEXT-5** | Per-sleeve drawdown circuit breaker | A single bad sleeve can bleed below the global halt while masked in aggregate | NEXT-4 | M | Med | none | CapitalAllocator zeroes a sleeve's weight if its realized P&L floor is breached; tested |
| **NEXT-6** | Drift monitor from returns, not equity gaps | A skipped cron cycle injects a spurious outlier return → false quarantine | none | S | Med | none | Drift fed by per-cycle returns with gap-detection; missed-day does not corrupt rolling Sharpe |
| **NEXT-7** | Dead-man's switch (UptimeRobot/Cronitor heartbeat) | Silent cron death is invisible; need external liveness, not just internal heartbeat | none | S | Med | none | Cron pings external monitor on success; missed window → email; documented |
| **NEXT-8** | **W8 Phase 1 (free): synthetic-delisting stress verdict** | Honestly framed: a true survivorship-free backtest needs **paid** data (Session 30). The *free* move is to push the existing `survivorship_stress.py` synthetic-delisting injection to **heavier haircuts** and document whether the edge survives — not to "replicate in QuantConnect/LEAN," which Session 30 already ruled out (free tier data is non-portable; `fja05680/sp500` gives membership dates only, no delisted OHLCV) | none | M | High | **data** (free, in-repo) | Run F1.1 stress at progressively heavier delisting/haircut levels; DECISIONS.md verdict on whether free evidence is sufficient to justify (or not) paying for survivorship-honest data |
| **NEXT-9** | EWMA-vol / barbell trend A/B in paper | Grade-A additive craft (full 0.83 / OOS 1.33); barbell 20/500d is the diversification engine | NOW-1 | M | Med | **time** (A/B window) | A/B vs deployed trend over a paper window; swap in only if it dominates on the gate metrics |
| **NEXT-10** | W9 coverage uplift on thin modules | Backtester 62%, base_strategy 78%, config 79%, metrics 81% — the riskiest paths are thinnest | none | M | Med | none | Each listed module ≥ 85%; suite green |
| **NEXT-11** | Reconcile stale docs (VISION/CLAUDE/ROADMAP/TASKS) | Test counts (416 vs 1070), F3.3 status, MCPT status, and the *partial* options wiring are stale/contradictory | none | S | Low | none | Docs reflect 1070 tests/92%; F3.3 built+gated; MCPT marked wired; options marked "risk side done, engine-routing open" |

**Critical-path note:** NEXT-1 advances Path A and is **blocked by NEXT-2**. NEXT-8 is the *honest* free first step of Path B — its DECISIONS.md verdict is the explicit input to the LATER-1 spend decision; it does not promise a free survivorship-free backtest the project already proved impossible.

---

## PHASE: LATER (3–6 months) — Fund the second edge, activate allocation

> Theme focus: **Second-Edge & Allocation**, **Data/Validation (paid W8)**, early **Capability Activation**.

| ID | Item | Rationale | Deps | Effort | Impact | Gating | Definition of Done |
|---|---|---|---|---|---|---|---|
| **LATER-1** | **W8 decision gate: pay-for-data or accept-haircut** | The single most consequential decision for the "two live edges" win; undecided in DECISIONS.md | NEXT-8 | S | High | **owner-action** (spend) | DECISIONS.md logs Path-1 (pay ~$20–50/mo: EODHD/Sharadar/Norgate) or Path-2 (small sleeve at stressed numbers) |
| **LATER-2** | **W8 Phase 2 paid survivorship-free Gauntlet** | The definitive value validation on delisted-inclusive data | LATER-1 (pay path) | M | High | **data** (~$20–50/mo) | Full Gauntlet on PIT delisted-inclusive universe; grade A/B clears; corr-to-trend re-measured < 0.5 |
| **LATER-3** ⚠️ | Flip value `funded=True` + activate allocator **(ADV cap required)** | The 80/20 blend lifts book Sharpe 0.82→0.99 at flat DD; config-only. But single names (unlike ETFs) have real market impact — an order at 1% of a $200M-ADV name moves the market. The ADV cap is a **prerequisite of going live, not an afterthought** | LATER-2, **LATER-4** | S | High | **data** (W8 pass) | `AllocationConfig.value.funded=True`; `AppConfig.allocation` set live; value sized at ~20% risk; **ADV cap (LATER-4) is in force before the first live value order** |
| **LATER-4** | ADV / market-impact cap for single-name value | Hard prerequisite of LATER-3 — value cannot go live uncapped | none | S | Med | none | RiskConfig caps order at ≤1% ADV; value pre-live checklist enforces it; LATER-3 cannot complete without it |
| **LATER-5** | Per-strategy position tagging in `StateStore` | Multi-strategy P&L attribution & future UI need a strategy tag per position | NEXT-4 | M | Med | none | `StateStore` positions carry `strategy_id`; report/allocator read per-sleeve cleanly |
| **LATER-6** | Deterministic append-only event ledger | Enables exact replay of a live session through the backtester to diagnose divergence. **Diagnostic tooling — independent of options routing** | none | M | Med | none | Every Signal/Order/Fill/Halt written JSON-L with monotonic seq + bar-time; replay test passes |
| **LATER-7** ⚠️🛑 | **Close the options engine-routing + Portfolio-tracking gap** | The risk side is already done (`evaluate_option` + `OptionOrderEvent` route through RiskManager, Session 31). What remains: `engine.py` does **not** handle `OptionOrderEvent`, and `Portfolio` has no options position/Greeks/reserved-cash tracking. This is the *remaining* scope — do not re-build the risk approval | none | M | High | none | `engine.py` handles `OptionOrderEvent` end-to-end; `Portfolio` tracks options positions, reserved cash, and Greeks; tests prove no bypass path and correct cash/exposure accounting |
| **LATER-8** | Pilot → Scale-1 (50%) capital ramp | Per the documented ramp; advance only on live evidence | NEXT-1 | M | High | **owner-action** + time | DD < 8%; live Sharpe within 30% of paper over 30–60 days; logged advancement |
| **LATER-9** | Regime-segmented Gauntlet slices | A grade-A strategy that only works in one vol regime is a regime bet, not an edge | none | M | Med | none | Gauntlet runs separately on high-vol/low-vol slices; report surfaces per-regime grade |

**Critical-path note:** LATER-1 → LATER-2 → LATER-3 (with LATER-4 in force) is the entirety of Path B's payoff, gated on an **owner spend decision** made the moment NEXT-8's free verdict lands. LATER-7 (options engine gap) is **no longer dependent on the event ledger** — it can ship independently. The draft's `PositionSizer` item (former LATER-9) is **cut to Anti-Goals** as architecture-violating scope bloat.

---

## PHASE: HORIZON (6+ months) — Capability activation, research, platform

> Theme focus: **Capability Activation (crypto/options/shorting)**, **Research (next edges)**, **Platform (apex-trader)**. All optional; pursue only on evidence.

| ID | Item | Rationale | Deps | Effort | Impact | Gating | Definition of Done |
|---|---|---|---|---|---|---|---|
| **HOR-1** | **Research: cross-asset carry sleeve** | Best evidence-backed next edge — corr to trend ≈ +0.09, free data (dividend yield, FRED yield-curve), ETF-only, low survivorship risk | LATER-3 | L | High | none | `carry = div_yield × momentum_filter` on ETFs; full Gauntlet; deploy-or-kill verdict |
| **HOR-2** | Research: regime detection (Statistical Jump Model) | JM beats HMM (14 vs 115 regime shifts/33y); modulates sleeve weights to cut DD toward 12–15%. **Prerequisite for any regime-gated edge** | LATER-3 | L | Med | none | 2-state JM on EWM-DD features; backtested weight modulation; deploy-or-kill |
| **HOR-3** | Research: defensive/quality overlay on value | Long-leg low-vol/quality survives costs (~2.7% net); natural hedge to growth/momentum regimes | LATER-3 | M | Med | **data** (fundamentals) | Quality screen (ROE/debt) as a value-sleeve overlay; Gauntlet; verdict |
| **HOR-4** | Research: regime-gated bond carry **(fresh backtest required)** | Bond carry **already FAILED** the Gauntlet standalone (full 0.34, OOS 0.07, mostly long IEF — Session 31). NOT viable as-is. Only revisit with a **new** regime-filtered backtest that produces fresh evidence; absent that, the "not re-litigating killed edges" anti-goal applies | HOR-2 | M | Low | none | A *new* IEF/SHY/TLT-on-slope + regime-filter backtest clears the Gauntlet, **or** the edge is formally re-killed in DECISIONS.md. No deploy on prior numbers |
| **HOR-5** ⚠️ | Crypto sleeve re-validation + carry overlay | Plumbing built but Gauntlet-failed (OOS −0.56); long-only crypto carry (funding-rate basis) may add signal quality | HOR-1 | M | Low | **data** + Gauntlet | Crypto trend+carry clears full Gauntlet on a fresh window, or stays gated with a logged verdict |
| **HOR-6** ⚠️ | Long/short activation (margin plumbing) | `allow_short=False`; needs a margin account (owner), ETB/locate checks per cycle, gross/net caps, a borrow-fee model, and a margin-aware Gauntlet. PDT retired (Jun 2026) removes the $25k floor. **Options routing is unrelated and is NOT a dependency** | none | L | Low | **owner-action** + Gauntlet | Margin-aware backtest grades A; gross/net caps set; ETB `shortable` checked each session; only then `allow_short=True` |
| **HOR-7** 🛑 | Options live (defined-risk, L2/L3) | Only after LATER-7 closes the engine/Portfolio gap; CC/CSP map to Alpaca L2, bull-put to L3 | LATER-7 | L | Med | **owner-action** (Alpaca L2/L3 approval) | Options fully routed (risk + engine + Portfolio); DTE/assignment/Greeks monitoring live; small defined-risk allocation |
| **HOR-8** | Platform: read-only API for apex-trader | apex-trader (Next.js) is the future control surface; SQLite `StateStore` is the natural integration point | LATER-5, LATER-6 | L | Med | none | `apex/api/` read endpoint wraps StateStore + Gauntlet report; shared-secret auth; no secrets exposed |
| **HOR-9** | Platform: DB-based halt signal | apex-trader "halt" button needs a DB flag `run_once` reads — env-var `APEX_HALT` isn't UI-triggerable | HOR-8 | S | Med | none | `halt` flag in SQLite read on startup; UI can set it; status reflects it |
| **HOR-10** | Validation: CPCV (Gate 3b) + quarterly re-validation cadence | CPCV gives more OOS paths than walk-forward; quarterly Gauntlet re-run is currently manual/undocumented | none | M | Low | none | Soft Gate 3b WARNs if CPCV OOS lower-bound < 0; a scheduled quarterly re-validation job exists |
| **HOR-11** | Reporting: quantstats tearsheet | Professional-grade reporting (rolling Sharpe, underwater, monthly heatmap) at near-zero cost | LATER-5 | S | Low | none | `quantstats.reports.html(returns, benchmark='SPY')` wired off the equity curve |
| **HOR-12** | Pluggable fill model for simulated engine | Fixed slippage today; a `FillModel` ABC enables more realistic/conservative bar-fill assumptions | none | M | Low | none | `FillModel` ABC with `get_fill_price(order, bar)`; default = current behavior; swappable |

---

## Critical Path (honest sequencing)

```
PATH A — LIVE CAPITAL (Edge #1)
  NOW-1 paper gate ─▶ NOW-2 broker preflight ─▶ NOW-3 live-switch gate ─▶ NOW-4..7 risk hardening
       ─▶ NEXT-2 state off PUBLIC repo (HARD BLOCKER) ─▶ NEXT-1 pilot 10–25%
       ─▶ LATER-8 scale 50% ─▶ Full 100%

PATH B — SECOND EDGE (Edge #2)               [the W8 data gate is THE blocker]
  NEXT-8 free synthetic-delisting stress verdict ─▶ LATER-1 spend decision (owner)
       ─▶ LATER-2 paid W8 Gauntlet ─▶ {LATER-4 ADV cap} ─▶ LATER-3 flip funded=True + allocator ON (80/20)

PATH C — CAPABILITY (only after risk is honest)
  LATER-7 options engine/Portfolio gap 🛑  ─▶ HOR-7 options live
  (independent) HOR-6 shorting  ◀── NOT gated on options
  (independent) LATER-6 ledger  ◀── NOT a prerequisite of LATER-7
```

**The three hard gates that govern everything:** (1) **time** — the 30-day paper gate (NOW-1) before any live dollar; (2) **leakage** — state must be off the public repo (NEXT-2) before any live dollar; (3) **data + owner spend** — W8 (NEXT-8 → LATER-1/2) before the second edge gets funded. Path C is deliberately last: no live options until the options engine-routing gap (LATER-7) closes. Shorting (HOR-6) and the event ledger (LATER-6) are intentionally **decoupled** from the options path — neither blocks nor is blocked by it.

---

## Anti-Goals / Explicitly NOT Doing

- **NOT** re-building the options *risk-approval* path — it already exists (`evaluate_option` + `OptionOrderEvent` through RiskManager, Session 31). Only the engine-routing + Portfolio-tracking half (LATER-7) remains.
- **NOT** scheduling MCPT wiring — it is already built, wired, and tested (`gauntlet_runner.run_mcpt`, `tests/test_mcpt_wiring.py`).
- **NOT** introducing a `PositionSizer` ABC / `SizingProposal` event. Sizing lives inside the RiskManager (sole order producer) and is already tested via `RiskConfig` + `SignalEvent.strength`. A pre-risk sizing event risks a path where sizing precedes the risk gate — exactly what the architecture forbids. The "byte-identical behavior" goal confirms it is pure refactor with no edge. Cut.
- **NOT** promising a *free* survivorship-free backtest. Session 30 settled this: free data is a dead end (`fja05680/sp500` is membership-only; QuantConnect data is non-portable). The free move is heavier synthetic-delisting stress (NEXT-8); the honest survivorship-free test is **paid** (LATER-2).
- **NOT** deploying bond carry on its prior numbers — it failed the Gauntlet standalone (OOS 0.07). Only a *new* regime-gated backtest with fresh evidence may revisit it (HOR-4); otherwise it stays killed.
- **NOT** building tick/L2/intraday infrastructure. The whole stack is daily-bar; GitHub Actions cron is structurally unsuited to sub-daily. Retail loses to HFT at low timeframes.
- **NOT** chasing more momentum-family sleeves. "You cannot diversify momentum with more momentum" — proven (cross-sectional momentum corr +0.76 to trend).
- **NOT** expanding the ETF universe for value. Cross-section too small (7 ETFs FAIL, 13 worse). Value needs large single-name cross-sections.
- **NOT** deploying naked short-vol, pure low-vol L/S, or standalone seasonality. Short-vol blows up without defined-risk structures; low-vol short leg is cost-destroyed; seasonality OOS evidence too weak.
- **NOT** re-litigating killed edges (RSI2, dual-momentum, ETF rotation, short-term reversal, value+momentum combo, turn-of-month, breadth/VAA, credit-spread, bond-carry-standalone). Kept as tested references only.
- **NOT** gating shorting (HOR-6) on options routing — they are architecturally unrelated.
- **NOT** paying for data speculatively. W8 spend happens only **after** the free stress verdict (NEXT-8).
- **NOT** managing other people's money. Personal account only — no-advice architecture stays.
- **NOT** breaking the one-module-per-session discipline or touching the immutable RiskManager casually. Every ⚠️ item gets its own session + DECISIONS.md entry.

---

## Success Metrics

**Go-Live (Path A)**
- Paper gate passed with live Sharpe tracking ≥ 70% of the 0.82 validated floor.
- State store provably off the public repo before the first live dollar — zero positions/equity in public git.
- Pilot: 30 days live, DD < 5%, realized slippage within 150% of model, zero spurious halts.
- A real (or deliberately forced) halt cleanly stops trading **and** cancels open orders — risk *exercised*, not just tested.
- `run_once` establishes a clean daily-loss baseline every cycle — no stale post-loss baseline survives a mid-session restart.
- Live-vs-shadow divergence stays within 2σ on a rolling 20-day window.

**Second Edge (Path B)**
- NEXT-8 synthetic-delisting stress verdict logged in DECISIONS.md.
- W8 spend decision (pay vs. accept-haircut) explicitly logged.
- Value clears the Gauntlet grade A/B on **paid** survivorship-honest data with corr-to-trend < 0.5.
- ADV cap in force before the first live value order.
- Allocator live at ~80/20; book Sharpe ≥ 0.95 with max DD not worse than trend-alone.

**Discipline & Operability**
- Options never reach live capital while the engine-routing/Portfolio gap (LATER-7) is open.
- Per-sleeve attribution surfaces any failing sleeve before it drags the book.
- Dead-man's switch + reconciliation diff: zero undetected silent cron deaths or position desyncs.
- Suite stays green; thin modules ≥ 85%; quarterly Gauntlet re-validation cadence honored.

**The North Star:** two uncorrelated, survivorship-honest, Gauntlet-validated edges running live on $0 infra, combined by the allocator, with risk that has halted cleanly in production at least once. Everything in this roadmap sequences toward that and nothing dilutes it.
