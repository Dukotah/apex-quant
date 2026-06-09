# Apex Quant — The Perpetual R&D Engine

> **Read order:** `VISION.md` (the bar) → `docs/ROADMAP-STRATEGIC.md` (the 0–6 month plan of
> record, NOW/NEXT/LATER/HORIZON) → **this file** (the engine that never stops).
>
> The strategic roadmap is a *finite* slice: it ends at "two edges live." **This document is the
> layer above it** — the set of flywheels that keep turning *after* that, forever, because a quant
> tool is never "done." Alpha decays, regimes rotate, costs drift, data rots, and risk that was
> only unit-tested is risk that hasn't been exercised. The job is not to finish a checklist; it is
> to **keep the flywheels spinning and keep climbing the maturity ladder.**

---

## 0. What "actually works" means (the honest frame)

The engine is *built*. What is unproven is that it **works as a trading instrument**:

| Dimension | Built? | Proven in reality? |
|---|---|---|
| Event architecture, fail-closed risk, Decimal money, deterministic backtest | ✅ | ✅ (tests) |
| The Gauntlet (9 gates) kills overfits, passes a real edge | ✅ | ✅ (tests) |
| Edge #1 (trend) survives a **live** 30-day paper gate | ✅ | ❌ (~13% done; cron died Jun 5–8) |
| Edge #2 (value) survives **survivorship-honest** data | ✅ (on survivors) | ❌ (needs paid PIT data) |
| The live loop runs unattended without silent death | partly | ❌ (just had a 3-day silent outage) |
| State is not leaking live positions to the public | ❌ | ❌ (hard blocker before live $) |
| Risk has **halted cleanly in production** at least once | ✅ (tests) | ❌ (never fired for real) |
| A failing sleeve is caught before it drags the book | partly | ❌ (no per-sleeve attribution live yet) |

**"Actually works" = every ❌ above becomes ✅, and then stays ✅ as the world changes.** The first
column is one-time engineering. The second column is **perpetual** — that's why this roadmap never
ends.

---

## 1. The Flywheel

```
                 ┌─────────────────────────────────────────────┐
                 │                                             ▼
   ┌──────────┐    ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
   │ DISCOVER │ ─▶ │ VALIDATE │ ─▶│ ALLOCATE │─▶ │ OPERATE  │ ─▶│ MONITOR  │
   │  edges   │    │ (Gauntlet│   │ capital  │   │  live    │   │  decay   │
   │          │    │  + honest│   │ across   │   │ (cleanly,│   │ & regime │
   │          │    │  data)   │   │  edges   │   │ unattend)│   │  shift   │
   └──────────┘    └──────────┘   └──────────┘   └──────────┘   └────┬─────┘
        ▲                                                            │
        │              decay detected / regime change / new data     │
        └────────────────────────────────────────────────────────────┘
                       (retire the stale, hunt the next)
```

Every edge eventually decays. The MONITOR stage feeds DISCOVER: when a live edge drifts below its
floor, you retire it and the hunt resumes. **There is always a next edge to find and an old one to
bury.** That loop is the never-ending part.

---

## 2. The Maturity Ladder (how to measure progress forever)

A finite roadmap is "done / not done." A perpetual one needs **levels you keep climbing**. Each
track below is scored L0→L5. The product's overall level is the *minimum* across tracks (a chain is
as strong as its weakest link). Re-score quarterly in `DECISIONS.md`.

| Level | Meaning |
|---|---|
| **L0** | Doesn't exist / not wired |
| **L1** | Built and tested, but inert (not in the live path) |
| **L2** | Wired into the live/backtest path, runs, manually operated |
| **L3** | Automated + observable (you get told when it misbehaves) |
| **L4** | Self-healing / self-regulating (recovers without you) |
| **L5** | Adversarially hardened + exercised in production (it has actually saved you once) |

**Current honest read (June 2026):** Architecture L4 · Validation L3 · Risk L3 (untested in prod →
not L5) · Execution-live L2 · Data L2 · Operability L2 (just regressed to L1 by the silent outage) ·
Allocation L1 · Platform L0 · Capability(options/short/crypto) L1. **Overall product level: ~L1–L2.**
The first mission is to drag the *minimum* up to L3 across the board, then L4, then L5. You never
"finish" L5 — you defend it.

---

## 3. The Perpetual Tracks

Ten flywheels. Each has a **charter** (why it exists), a **cadence** (how often it turns), a
**"healthy" bar** (what good looks like), and a **standing backlog** (concrete candidate work that
refills itself — this is the never-ending part). The strategic roadmap's NOW/NEXT/LATER items are
the *current pull* from these backlogs; when those land, you pull the next.

> **Discipline gate (non-negotiable, applies to every track):** one module per session · `make
> check` green before "done" · a `DECISIONS.md` entry per meaningful choice · the RiskManager is
> extended only via reviewed, tested, dedicated changes, never weakened/bypassed · respect the
> Anti-Goals (§6). Nothing in this document overrides `CLAUDE.md`.

---

### Track 1 — Edge Discovery (the alpha factory)

**Charter.** Always have ≥1 *candidate* edge in the pipeline and a written verdict on the last one.
Alpha decays; an empty pipeline is a dying fund.

**Cadence.** Continuous. One hypothesis → Gauntlet → deploy-or-kill verdict per research session.

**Healthy bar.** At any moment: ≥1 live edge, ≥1 candidate in validation, ≥1 hypothesis queued, and
every dead candidate has a one-line epitaph in `DECISIONS.md` so it's never re-litigated.

**Standing backlog (pull in priority order; refill from the tail forever):**
- The **second edge endgame**: survivorship-honest single-name value (Path B). This is the current
  pull — everything else waits behind it.
- Cross-asset **carry** sleeve (div-yield × momentum filter; corr-to-trend ≈ +0.09; free data) —
  the best evidence-backed *third* edge per the research.
- **Defensive/quality overlay** on the value long-leg (ROE/debt screen) — natural growth-regime hedge.
- **Crisis-alpha / tail-hedge** sleeve — measure calm-regime drag vs drawdown payoff explicitly.
- **Ensemble meta-strategy** that votes across the validated library into one blended signal.
- **Decay tracker**: rolling live-Sharpe of each deployed sleeve vs its OOS baseline → auto-flag.
- **Kill-criteria doc**: the written rule for when a *deployed* edge gets retired (drift floor, corr
  breach, regime change). Discovery and retirement are the same flywheel.
- Refill: every quarter, run a literature/anomaly sweep and add 2–3 fresh hypotheses to the tail.

**Forbidden here (see §6):** more momentum-family sleeves; widening the ETF universe for value;
tick/intraday signals; naked short-vol; re-litigating the killed edges.

---

### Track 2 — Validation Science (the anti-lie moat)

**Charter.** Every way a backtest can lie should be a structural check, not a hope. This is the
project's actual moat — keep widening it.

**Cadence.** New gate or sharpening per research cycle; full re-validation of the live book quarterly.

**Healthy bar.** The Gauntlet resists overfit (✅ MCPT/PBO/DSR), look-ahead (✅ determinism/WF),
cost (✅ 2× stress), *and* survivorship (⚠️ partial) and regime-dependence (⚠️ partial). Quarterly
re-validation is scheduled, not remembered.

**Standing backlog:**
- **Wire the built-but-inert validators into the Gauntlet/grading**: `deflated_sharpe`, `benchmark`
  (α/β/IR vs SPY), `block_bootstrap` (autocorr-aware MC) — they exist and are tested (F4/F13) but
  aren't gates yet.
- **Regime-segmented Gauntlet** (bull/bear/sideways/high-vol slices) — a grade-A edge that only
  works in one vol regime is a regime *bet*, surface it as such (HOR-9).
- **CPCV (combinatorial purged CV)** as a soft Gate 3b — more OOS paths than walk-forward (HOR-10).
- **Minimum track-record length** estimate in every Gauntlet report.
- **One-page HTML/MD Gauntlet artifact** per run + a **Gauntlet regression test** (a known-good
  strategy must keep its grade across refactors).
- **`scripts/gauntlet.py` CLI** — run any library strategy by name end-to-end.
- **Survivorship as a first-class gate** (Track 5 supplies the data; this track scores it).
- Refill: as each new backtest-lie is discovered in the wild, add a gate that resists it.

---

### Track 3 — Risk Science (the whole point)

**Charter.** Risk that is only unit-tested is risk that has never fired. Drive it to L5 — *exercised
in production* — and keep adding fail-closed guards as new failure modes surface.

**Cadence.** Each new guard gets a dedicated ⚠️ session (RiskManager rule). Quarterly: a deliberate
fire-drill (force a halt in paper, confirm it flattens + cancels + alerts).

**Healthy bar.** Every limit has a "breach → reject/halt" test; the kill switch flattens *and*
cancels resting orders; a real drawdown has halted cleanly at least once; no stale daily baseline
survives a restart.

**Standing backlog (each ⚠️ = dedicated session + DECISIONS entry):**
- **Confirm HaltEvent cancels open broker orders** end-to-end (NOW-6 ✅ — now exercise it for real).
- **Per-sleeve drawdown circuit breaker** (zero a sleeve's weight on its P&L floor; needs Track 7
  attribution) (NEXT-5).
- **Daily-loss soft halt** (reduce-only) distinct from the hard drawdown halt.
- **Per-sector / per-asset-class** and **correlation-cluster** exposure caps (beyond per-symbol).
- **ADV / market-impact cap** — *hard prerequisite* before any single-name value order (LATER-4).
- **Gross + net leverage cap** distinct from per-position; **max-positions** cap; **time-stop**
  auto-flatten; **trailing-stop escalation** on winners; **slippage-budget throttle**.
- **Post-halt recovery protocol** — the documented, tested way the system resumes after a halt.
- **Config-validation layer** — reject an internally-inconsistent `RiskConfig` at startup.
- **Quarterly fire-drill**: the ritual that turns L3 risk into L5 risk.
- Refill: every production near-miss becomes a new guard + a new test.

---

### Track 4 — Execution & Microstructure (paper→live fidelity)

**Charter.** The gap between backtest fills and live fills is where retail quietly dies. Shrink and
measure it forever.

**Cadence.** Continuous while live; A/B any fill-model change in paper before adopting.

**Healthy bar.** Live slippage tracked vs model on every fill; shadow (paper) book runs in parallel
with live; idempotent submission; reconciliation diffs broker truth every cycle.

**Standing backlog:**
- **Shadow-trade logging** — book every live signal on paper too; alert on 2σ divergence (NEXT-3).
- **Pluggable `FillModel` ABC** for the simulated engine (default = current; swap in conservative
  bar-fill assumptions) (HOR-12).
- **Order types beyond market** (limit / stop-limit) with RiskManager validation.
- **Partial-fill handling** + a partial-fill simulation mode.
- **Idempotency-key persistence** so a retried cron can't double-submit.
- **Market-hours / pre-market guard**; **graceful disconnect → safe-mode** (cancel-all + halt).
- **`--no-submit` dry-run** for `run_once` (logs intended orders).
- **Capacity / market-impact study** per edge (when does size start moving the print?).
- Refill: each live execution surprise (reject reason, buying-power quirk) becomes a handler + test.

**Forbidden:** building tick/L2/intraday infrastructure — the stack is daily-bar by deliberate
design; retail loses to HFT at low timeframes.

---

### Track 5 — Data & Universe (garbage-in protection)

**Charter.** Every edge is downstream of data you trust. Expand coverage, harden hygiene, and close
the survivorship hole that gates the whole second-edge path.

**Cadence.** Hygiene modules wired opportunistically; universe definitions versioned on every change.

**Healthy bar.** Corporate-actions adjusted, outliers filtered, gaps detected, calendar-aware, and a
**survivorship-honest path exists** (paid, bounded) for single-name research.

**Standing backlog:**
- **Survivorship-honest data decision** (Path B): exhaust free synthetic-delisting stress (NEXT-8) →
  explicit pay-for-data verdict (LATER-1) → paid PIT delisted-inclusive Gauntlet (LATER-2). The one
  deliberate exception to $0-infra, and *only* on evidence.
- **Wire the built-but-inert hygiene modules** (F3/F15): `data_quality_report` as a pre-backtest
  gate, `outlier_filter` into the feed, `resample` exported, `corporate_actions` / `ohlc_consistency`
  / `trading_calendar` used by the pipeline (today they sit idle).
- **Parquet caching** for Alpaca historical pulls (stop refetching).
- **Second data source** (yfinance/Stooq) behind `base_feed` for cross-validation.
- **Universe versioning** — cache + version the ETF/name universe so backtests are reproducible.
- **Warmup→stream reconciliation** check at the live handoff.
- Refill: each new asset class or data feed brings its own hygiene + tests.

---

### Track 6 — Operability & Reliability (the SRE flywheel)

**Charter.** Running the live system should be *a glance plus trustworthy alerts*, never
log-spelunking — and it must **never silently die again**. (It just did, for 3 days.)

**Cadence.** Continuous; this track has *priority* because a dead cron makes every other track moot.

**Healthy bar.** Silence is meaningful (heartbeat + external dead-man's switch); one command shows
full state; every cron cycle is traceable; the operator gets only actionable alerts.

**Standing backlog:**
- **External dead-man's switch** (UptimeRobot/Cronitor) — internal watchdog (landed) is necessary
  but not sufficient; silent schedule-non-fire needs an *outside* observer (NEXT-7).
- **Unified status** at L4: `scripts/status.py` ✅ exists — drive it to self-healing + drift-aware.
- **Structured JSON logging with a run-id** per cycle; **log rotation/retention**.
- **Daily equity/P&L snapshot** to state for charting; **weekly digest** (P&L/Sharpe/drift/movers).
- **Halt-event audit trail** (what tripped, when, what cleared).
- **Full ops runbook**: halt, resume, diagnose a failed cron, rotate keys.
- **Drift monitor from returns, not equity gaps** (a skipped cycle must not inject a false outlier)
  (NEXT-6).
- Refill: every outage post-mortem adds a detector so that *class* of silence can't recur (the
  Jun 5–8 outage already added the self-healing preflight + watchdog — keep that reflex).

---

### Track 7 — Capital & Allocation Science (combining edges)

**Charter.** Once ≥2 honest edges exist, *how* you weight them is itself an edge. Keep the allocator
honest, attributable, and risk-budgeted.

**Cadence.** Activated when the second edge clears; then continuous re-weighting research.

**Healthy bar.** Per-sleeve P&L/Sharpe/weight attribution is live; weights are risk-budgeted (not
just inverse-vol); one sleeve can never consume the book; conflicting signals net deterministically.

**Standing backlog:**
- **Per-sleeve P&L attribution** in `report.py` (a 7-sleeve book hides a failing sleeve) (NEXT-4).
- **Per-strategy position tagging** in `StateStore` (`strategy_id` on every position) (LATER-5).
- **Flip value `funded=True` + activate the allocator** at ~80/20 — *after* W8 passes and the ADV
  cap is in force (LATER-3, the payoff of Path B).
- **Risk-budget allocator** (cap each strategy's contribution to total portfolio vol).
- **Correlation-aware down-weighting** as pairwise corr rises; **rebalance scheduling** (calendar vs
  threshold-drift) with backtest parity.
- **Deterministic netting** for conflicting signals; **hard per-strategy ceiling**.
- Refill: each new live edge is a new allocation problem (weight, corr, capacity, decay rate).

---

### Track 8 — Platform & Product (apex-trader, the control surface)

**Charter.** The engine is the brain; `apex-trader` (Next.js) is the eventual control surface. When
the engine is honest and operable, expose it cleanly. (Lives in the sibling repo — this track is the
*engine-side contract*.)

**Cadence.** Starts after Operability hits L3; then continuous as the product surface grows.

**Healthy bar.** A read-only, authenticated status API; a DB-based halt the UI can trigger; no
secrets ever crossing the boundary.

**Standing backlog:**
- **Read-only status API** (`apex/api/`) wrapping `StateStore` + Gauntlet report; shared-secret auth
  (HOR-8). The status-export contract already exists in both repos — formalize it.
- **DB-based halt signal** the UI can set and `run_once` reads (env-var `APEX_HALT` isn't
  UI-triggerable) (HOR-9).
- **quantstats tearsheet** off the equity curve (rolling Sharpe, underwater, monthly heatmap) (HOR-11).
- **Deterministic append-only event ledger** (Signal/Order/Fill/Halt as JSON-L with monotonic seq) —
  enables exact replay of a live session through the backtester to diagnose divergence (LATER-6).
- Refill: every operator question the dashboard can't answer becomes an endpoint.

**Forbidden:** managing other people's money — personal-account, no-advice architecture stays.

---

### Track 9 — Capability Activation (new instruments, only on evidence)

**Charter.** Crypto, options, shorting, (someday) futures expand the opportunity set — but each is
*off by default* and gated behind its own Gauntlet pass and risk plumbing. Activate deliberately,
never speculatively.

**Cadence.** One capability at a time, each a multi-session ⚠️/🛑 effort, only when an edge needs it.

**Healthy bar.** No capability reaches live capital until: its risk path routes through the
RiskManager, its Gauntlet passes on a fresh window, and its operational plumbing is tested.

**Standing backlog (none are NOW work — they wait for evidence):**
- 🛑 **Options engine-routing + Portfolio gap** (LATER-7): the *risk* side is built; `engine.py`
  still doesn't handle `OptionOrderEvent` and `Portfolio` tracks no Greeks/reserved-cash. This is the
  single most dangerous *remaining* capability gap — close it before any live option (then HOR-7).
- **Long/short activation** (HOR-6): `allow_short=False`; needs margin plumbing, ETB/locate checks,
  gross/net caps, a borrow-fee model, a margin-aware Gauntlet. Independent of options.
- **Crypto re-validation** (HOR-5): plumbing built, Gauntlet-failed (OOS −0.56); revisit only with a
  funding-rate carry overlay on a fresh window.
- **Regime detection** (Statistical Jump Model, HOR-2) as a prerequisite for any regime-gated edge.
- Refill: a new instrument is only added when an *edge* demands it — capability follows alpha, never
  leads it.

---

### Track 10 — Engineering Health & Meta (keep the machine maintainable)

**Charter.** A research engine you can't safely change is a dead engine. Keep the codebase honest,
the toolkit wired, and the docs true.

**Cadence.** A slice every few sessions; a "doc-recon + dead-code sweep" every month.

**Healthy bar.** Thin modules ≥85% coverage; the large built-but-inert toolkit is either wired or
deleted; docs match reality (test counts, gate counts, statuses); branches/PRs pruned.

**Standing backlog:**
- **Wire-or-delete the toolkit.** The 2026-06-06 fan-outs added ~100 tested-but-inert modules
  (indicators, validators, analytics, risk calculators) plus F1–F17 follow-ups. Each is either
  pulled into the live path (Tracks 2/3/5/7) or removed. *Tested ≠ used* — un-wired code is a
  liability, not an asset. This is a large, genuinely never-ending integration backlog on its own.
- **Coverage uplift** on the riskiest-thinnest modules (`backtester` 62%, `base_strategy` 78%,
  `config` 79%, `metrics` 81%) → ≥85% (NEXT-10).
- **Property-based tests** (hypothesis) for indicators on edge windows; **mutation testing** on the
  RiskManager (prove the tests actually guard it).
- **mypy strict** in CI; **architectural-fitness test** ✅ (keep extending it).
- **Indicator consolidation** — 25 `ind_*.py` beside `indicators.py`; pick ONE convention (F11).
- **Doc-recon ritual** — reconcile VISION/CLAUDE/ROADMAP/TASKS counts & statuses (they drift; e.g.
  "416 vs 1070 vs 3056 tests" has happened) (NEXT-11).
- Refill: every refactor leaves the next seam to tidy; every fan-out leaves a wiring tail.

---

## 4. Cadence Rituals (what makes it self-sustaining)

A perpetual roadmap dies without rhythm. These are the recurring obligations that keep the flywheels
turning whether or not anyone "feels like" doing roadmap work:

| Cadence | Ritual | Output |
|---|---|---|
| **Every cycle (cron)** | Watchdog + status export + reconciliation diff | `status.json`, alerts on anomaly |
| **Daily** | Glance at `scripts/status.py`; paper-gate day N/30 | A number, not a log dig |
| **Weekly** | Digest (P&L, Sharpe, drift, per-sleeve); prune branches/PRs | One push notification |
| **Monthly** | Doc-recon + dead-code sweep (Track 10); decay-tracker review (Track 1) | Honest docs; flagged decay |
| **Quarterly** | Full Gauntlet re-validation of the live book; **risk fire-drill** (Track 3); re-score the Maturity Ladder (§2) in DECISIONS | A graded book; an exercised halt; a level-up plan |
| **Per research session** | One hypothesis → Gauntlet → deploy-or-kill verdict + epitaph | A decision, never a half-finished probe |
| **Annually** | Re-read VISION; confirm the North Star still holds; sunset decayed edges | A pruned, current strategy roster |

If a ritual ever feels skippable, that's the signal it should be **automated** (push it up the
Maturity Ladder), not dropped.

---

## 5. How this connects to the strategic roadmap

`docs/ROADMAP-STRATEGIC.md` is **the current pull from these flywheels** — the specific NOW/NEXT/
LATER/HORIZON items in flight right now, with IDs, deps, and DoD. This document is the **refill
mechanism**: when a strategic item lands, you don't run out of work — you pull the next candidate
from the relevant track's standing backlog and (if it's a meaningful chunk) promote it into the
strategic roadmap with a fresh ID.

```
  PERPETUAL TRACKS (this file)  ──promote next item──▶  STRATEGIC ROADMAP (NOW/NEXT/LATER)
        ▲                                                        │
        └──────────────── item lands; refill the tail ──────────┘
```

So the two never conflict: the strategic roadmap is the *sprint board*, the perpetual tracks are the
*backlog that refills itself*, and the Maturity Ladder is the *scoreboard*.

---

## 6. Anti-Goals (carried forward — do not drift)

These are inherited verbatim from `docs/ROADMAP-STRATEGIC.md` and bind every track above:

- **NOT** building tick/L2/intraday infra — daily-bar by design; retail loses to HFT intraday.
- **NOT** chasing more momentum-family sleeves — "you can't diversify momentum with more momentum."
- **NOT** widening the ETF universe for value — cross-section too small (7 fails, 13 worse).
- **NOT** re-litigating killed edges (RSI2, dual-momentum, ETF rotation, short-term reversal,
  value+momentum combo, turn-of-month, breadth/VAA, credit-spread, bond-carry-standalone). Kept as
  tested references only; revisit only with a *new* backtest producing *fresh* evidence.
- **NOT** a `PositionSizer` ABC / pre-risk sizing event — sizing lives inside the RiskManager (sole
  order producer); a pre-risk sizing path is exactly what the architecture forbids.
- **NOT** promising a *free* survivorship-free backtest — it's a proven dead end; the honest test is
  paid, and only on evidence (NEXT-8 → LATER-1/2).
- **NOT** deploying naked short-vol, pure low-vol L/S, or standalone seasonality.
- **NOT** paying for data speculatively — spend happens only after the free stress verdict.
- **NOT** managing other people's money — personal account only; no-advice architecture stays.
- **NOT** breaking one-module-per-session or touching the immutable RiskManager casually.

---

## 7. The North Star (unchanged)

> **Two-plus uncorrelated, survivorship-honest, Gauntlet-validated edges running live on $0 infra,
> combined by the allocator, with risk that has halted cleanly in production at least once** — and
> then *kept* there as edges decay and get replaced, forever.

The strategic roadmap gets you to that sentence once. **This document is how it stays true.**
