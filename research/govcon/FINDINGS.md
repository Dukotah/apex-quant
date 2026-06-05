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

## Verdict & next steps

The easy version (buy small-caps after awards) is **dead**. There is a real,
significant **mid-cap** effect that is a legitimate research lead — not a strategy
yet. To confirm or kill it:
1. Replace PoP dates with true **announcement timestamps** (FPDS `date_signed`; ideally
   news). Re-run; if the post-event edge survives precise dates, it's real.
2. Fix **survivorship**: add delisted contractors (Mantech, Vectrus pre-merge, etc.).
3. Segment by **award size relative to market cap** and by **agency**; multiple-comparison
   correct.
4. If it still holds, wrap it as an apex-quant strategy and run the full **Gauntlet**
   (the same bar the trend strategy cleared). Only a Gauntlet pass earns paper capital.
