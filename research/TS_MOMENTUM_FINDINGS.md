# TimeSeriesMomentumBlend — Gauntlet verdict (BACKLOG F1, A6)

**Date:** 2026-06-07 · **Universe:** smart-7 (SPY EFA TLT GLD DBC UUP DBA), real
Yahoo adjusted-close history ~2006–2026 · **Status: FAIL → ARCHIVED.**

Reproduce:
```
python -m scripts.fetch_yahoo SPY EFA TLT GLD DBC UUP DBA --start 2005-01-01 \
    --out data/real/multiasset_smart7.csv
python -m scripts.validate_real ts_momentum      # full 7-gate Gauntlet
python -m research.ts_momentum_study             # corr-to-trend + tuning scan
```

## 1. Gauntlet on real data → grade **FAIL** (hard gate)

| Gate | Result |
|------|--------|
| 1 In-Sample Sanity | ✗ **FAIL** — IS Sharpe 0.47 < 0.50 (hard gate) |
| 2 Out-of-Sample | ✓ PASS — OOS Sharpe 0.92 (195% of IS) |
| 3 Walk-Forward | ✓ PASS — WF Sharpe 0.72, eff 1.47 |
| 4 Monte Carlo | ✓ PASS — p=0.004, realistic DD 68% |
| 5 Cost Stress | ✓ PASS — Sharpe 0.59 at 2× cost |
| 6 Param Sensitivity | ✓ PASS — robust plateau (neighbors 0.57 vs 0.63) |
| 7 Benchmark/Correlation | ✓ PASS — corr 0.27 to SPY |

`trades=217 · IS Sharpe 0.47 · OOS 0.92 · full 0.63 · @2×-cost 0.59 · benchmark 0.64`

A hard-gate fail is conclusive: **archive, do not deploy.** Note the *inverted*
split (OOS 0.92 ≫ IS 0.47) — this is not overfitting; in-sample momentum is just
genuinely weak across 2006–2016 (2008 + the 2011–2015 chop), strong 2018–2026.

## 2. The decisive metric: correlation to the DEPLOYED edge

Gate 7 measures corr to SPY (0.27). The real second-edge bar (F8/F17) is
correlation to the deployed `multi_asset_trend` sleeve, backtested on identical
events:

> **corr(ts_momentum_blend, multi_asset_trend) = +0.88**

Momentum is trend-family, and the number confirms it: this is the **same edge
re-expressed**, not a distinct one. Even a Gauntlet pass would add turnover and
concentration, not diversification. This alone disqualifies it for §A.

## 3. "Tune the defaults" → declined as gate-chasing

Scan over the entry/sizing priors (buy_threshold × scale, 9 cells). IS Sharpe
wobbles ~0.43–0.52 with **only 3/9 cells clearing 0.50** and no coherent
gradient — noise straddling the threshold, not a miscalibrated prior. Picking the
one cell that crosses (0.05/0.30 → IS 0.52) would be fitting to the split. The
academic lookbacks (21/63/126/252) already pass Gate 6's plateau, so they aren't
the issue either. Declined.

## Bottom line

`ts_momentum_blend` is **registered for tooling (F1 done), tested on real data,
and archived**: it fails a hard Gauntlet gate AND is 0.88-correlated to the
deployed trend. Per F17, building it was necessary but not sufficient — the
Phase-6 unblocker remains a genuinely *uncorrelated*, cost-clearing edge (the
deployed **value** sleeve is that edge; momentum is not a second one).
