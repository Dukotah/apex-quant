# Govcon Contract-Award Event Study — Findings (v1)

**Question:** does winning a large federal contract predict abnormal stock returns,
and is the effect concentrated in small-caps (the original hypothesis)?

**Method:** `event_study.py` — 35 curated public contractors, every award ≥ $20M
(2016–2024 H1), market-adjusted returns vs SPY, split by cap tier, with a bootstrap
significance test (random-date null per stock) so a stock's own drift can't fake a
result. 474 events with full price coverage.

## Results (market-adjusted vs SPY)

| tier | n | pre[-10,0] | fwd[0,+1] | fwd[0,+5] | bootstrap p (fwd[0,+1] / +5) |
|------|---|-----------|-----------|-----------|------------------------------|
| **small** | 48 | +1.85% (t1.69) | −0.26% | +0.23% | 0.81 / 0.51 — **no edge** |
| **mid** | 188 | +0.77% (t1.70) | **+0.74%** | **+0.82%** | **0.000 / 0.011 — significant** |
| **large** | 238 | −0.00% | +0.08% | +0.12% | 0.22 / 0.35 — none (priced in) |

## Read

1. **Method is sound — the control behaves.** Large-cap primes show ~zero abnormal
   return (awards are immaterial to a $50B prime, and priced in instantly). A study
   that found "edge" there would be broken; this one correctly finds none.
2. **The original hypothesis is REJECTED.** Small-caps show no tradeable post-award
   move (p=0.51). The +1.85% *pre-event* run-up says whatever happens lands BEFORE
   the contract date — leakage / date-lag — so it isn't capturable on these dates.
3. **Unexpected real lead: mid-caps.** +0.74% next-day / +0.82% over 5 days, both
   bootstrap-significant (p≈0.000 / 0.011). The bootstrap null is random dates on the
   SAME (surviving) stocks, so this is NOT merely survivorship drift. This is a
   genuine flicker worth chasing — but it was not the hypothesis, and it's caveated.

## Why this is NOT yet tradeable (the honest caveats)

- **Dates are contract/PoP dates, not press-release timestamps.** The pre-event
  run-up shows part of the move precedes our event day, so real entry timing is
  unclear. Need true announcement times (FPDS `date_signed` / news) to know if
  +0.74% is actually capturable or already gone.
- **Survivorship bias.** MANT (bought out 2022) and TGI failed price fetch — delisted/
  renamed names are missing, biasing survivors up. The bootstrap controls for each
  survivor's drift but not for the absent dead names.
- **Multiple comparisons.** 3 tiers × 4 windows tested; some significance expected
  by chance (though p=0.000 on mid fwd[0,+1] is strong).
- **Low frequency + entity resolution.** ~23 mid-cap events/yr; some 'med'-confidence
  names may misattribute subsidiary awards.

## v2 — corrected event dates (contract `date_signed`, not PoP start)

Re-ran with the actual contract **signing date** (USAspending award-detail
`date_signed`) as the event, the cleaner announcement proxy. The decisive test.

| tier | n | pre[-10,0] | fwd[0,+1] (p) | fwd[0,+5] (p) |
|------|---|-----------|---------------|---------------|
| small | 48 | +1.42% | +0.86% (**0.024**) | +0.67% (0.32) |
| **mid** | 189 | +0.43% | **+0.77% (0.000)** | **+0.89% (0.004)** |
| large (control) | 238 | −0.21% | +0.01% (0.45) | +0.15% (0.30) |

**The lead survived and got cleaner.** Versus v1 (PoP dates): the mid-cap pre-event
run-up **shrank** (0.77% → 0.43%) while the post-event edge **held/strengthened**
(+0.89%, p=0.004). That is the signature of a REAL effect with better-timed dates —
the move now lands after the corrected event, not before. A date artifact would do
the reverse. Large-cap control stayed null throughout (method still honest). Small-cap
flipped to a significant +1-day pop but fades by day 5 (p=0.32) — suggestive but
fragile (n=48).

## v3 — CAPTURABILITY (the make-or-break test): NOT tradeable

`capturability.py`. Same validated event set; the only change is realistic entry —
DoD announces awards the evening of signing (after close), so a public trader can
first act at the NEXT OPEN. Entry at open[T+1], same exit close[T+5], net of
slippage (mid 30bps), bootstrap-tested. Large-cap stays the control.

| tier | ideal close[T]->[T+5] | overnight gap (uncapturable) | real open[T+1] NET | bootstrap p |
|------|----|----|----|----|
| small | +0.67% | +0.23% | −0.03% | 0.37 |
| **mid** | +0.89% | +0.33% | **+0.22%** | **0.066** |
| large (control) | +0.15% | −0.05% | +0.04% | 0.22 — null ✓ |

**Verdict: the mid-cap edge is REAL but NOT TRADEABLE.** Of the +0.89% ideal edge,
~0.33% is the overnight announcement gap (gone before you can act), ~0.30% is
first-day continuation you miss entering at the next open, and ~0.30% is costs —
leaving +0.22% net at a 51% win rate, p=0.066 (not significant), and NEGATIVE by
T+2. The alpha lives entirely in the hours between the government signing and the
public being able to react; once you trade on public info at the next open net of
slippage, it's noise. (A cautionary note: an earlier buggy version of this test —
looser event set + naive t-stats — reported +2.75% NET, p=0.000. The large-cap
control lighting up exposed the bug. Trust controls and bootstraps, not raw means.)

**Decision: PARK IT.** Documented, validated, and correctly killed at the
capturability gate — exactly the bar that should stop capital. Not worth building
into a strategy. Possible (low-priority) revisits: true press-wire timestamps + an
intraday reaction (a latency game retail tends to lose), or other public-but-neglected
catalysts (FDA, 8-K, hiring) run through this same honest pipeline.

---

## (superseded) earlier next-steps after v2

The easy version (buy small-caps after awards) is **dead as originally framed**. But
there is a **robust, bootstrap-significant mid-cap effect** (~+0.8% over 1–5 days,
p≤0.004) that *strengthened* under date correction — a credible alpha lead, not yet a
proven strategy. The make-or-break question has now shifted:

1. **CAPTURABILITY (the new decisive test):** `date_signed` ≈ when DoD signs, which may
   be at or before *public* dissemination (the DoD daily contract digest, press wires).
   Measure the edge entering at the **next open after the award is PUBLIC**, not at the
   signing-day close. If +0.77% survives realistic public-reaction timing + small/mid-cap
   slippage, it's tradeable; if it needs signing-day knowledge, it isn't.
2. Fix **survivorship**: add delisted/acquired names (Mantech, Vectrus pre-merge, …).
3. **Multiple-comparison** correct; segment by award size / cap and by agency.
4. If it still holds → wrap as an apex-quant strategy and run the full **Gauntlet** (same
   bar the trend strategy cleared). Only a Gauntlet pass earns paper capital.
