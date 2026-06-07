# Apex Quant — Task Backlog

A running list of concrete, actionable tasks for the project. Phases 1–5 are
code-complete (see `ROADMAP.md`); this backlog is the forward work — Phase 6
(live ops + strategy expansion) and beyond.

**Rules of the road (from `CLAUDE.md`):** events-only between modules · strategies
emit signals, never orders · risk fails closed · Decimal for money · determinism
is sacred (injected `Clock`, seeded RNG) · every module ships with tests ·
paper-trade 30+ days before live.

Status legend: 🔲 to do · 🚧 in progress · ✅ done

---

## A. Strategy R&D — the second uncorrelated edge (Phase 6's #1 unblocker)

1. 🔲 Re-run cross-asset value on the richer 10–13 ETF pool (vs current 7) and re-measure standalone Sharpe + correlation to `multi_asset_trend`.
2. 🔲 Build a combined per-asset value+momentum composite score and Gauntlet it as a single sleeve.
3. 🔲 Test long-horizon (3–5y) mean reversion on equity-index ETFs as a standalone driver; record corr to trend.
4. 🔲 Probe a carry/term-structure sleeve (bond curve steepness, VIX term structure proxy) for uncorrelated premium.
5. 🔲 Add a defensive/safe-haven sleeve (gold + long Treasuries) gated on a risk-off regime filter.
6. ✅ Implement a time-series momentum strategy across multiple lookbacks (1/3/6/12m) blended, distinct from the deployed inverse-vol trend. *(built+tested: `apex/strategy/library/ts_momentum_blend.py`, 23 tests; not yet registered/tuned — see F1, F8)*
7. 🔲 Research a seasonality/calendar-effect overlay (turn-of-month, sell-in-May) as a tilt, not a standalone.
8. ✅ Build a volatility-regime classifier (e.g. realized-vol percentile buckets) usable as a strategy gate. *(built+tested: `apex/strategy/regime.py`, 17 tests; not yet consumed by any strategy — see F2)*
9. 🔲 Test a breadth/dispersion signal from the ETF basket as a risk-on/off switch.
10. ✅ Add a min-variance / risk-parity weighting variant of the trend sleeves and compare to inverse-vol. *(built+tested: `apex/strategy/weighting.py` — equal/inverse-vol/risk-parity + corr down-weight, 26 tests; feeds the allocator, see B/F2)*
11. 🔲 Implement a correlation-aware sleeve weighting that down-weights sleeves as their pairwise corr rises.
12. 🔲 Research a trend-following on FX/crypto ETFs (if Alpaca-tradeable) for a genuinely different asset class.
13. 🔲 Build a "crisis alpha" tail-hedge sleeve and measure its drag in calm regimes vs payoff in drawdowns.
14. 🔲 Add a strategy-level turnover/cost report so each candidate shows net-of-cost edge before deploy.
15. 🔲 Create a reusable research notebook/template under `research/` that runs any candidate through corr-to-deployed + Gauntlet in one shot.
16. 🔲 Implement an ensemble meta-strategy that votes across the library and emits a single blended signal.
17. 🔲 Document a "kill criteria" doc: when to retire a deployed strategy (drift floor, corr breach, regime change).
18. 🔲 Stand up a strategy-decay tracker that compares rolling live Sharpe of each library strategy to its OOS baseline.

## B. Multi-strategy capital-allocation engine (deferred until a 2nd edge clears)

19. 🔲 Define the `Allocator` interface: takes per-strategy signals + portfolio snapshot, returns capital weights.
20. 🔲 Implement equal-weight and inverse-vol allocators as the two reference policies.
21. 🔲 Add a risk-budget allocator that caps each strategy's contribution to total portfolio vol.
22. 🔲 Wire the allocator between strategies and the risk manager without violating the events-only rule.
23. 🔲 Enforce a hard per-strategy capital ceiling so one strategy can never consume the whole book.
24. 🔲 Handle conflicting signals (two strategies long/short the same symbol) with a deterministic netting rule.
25. 🔲 Add rebalance scheduling (calendar vs threshold-drift) with backtest parity.
26. 🔲 Persist per-strategy attribution (P&L, Sharpe, weight history) to state for the dashboard.
27. 🔲 Write allocator tests: weights sum to ≤ 1, ceilings respected, fail-closed on malformed strategy output.
28. 🔲 Backtest the allocator with the deployed trend + one synthetic uncorrelated edge to validate the split logic.

## C. Risk manager enhancements

29. 🔲 Add per-sector / per-asset-class exposure caps on top of the existing per-symbol cap.
30. 🔲 Implement a correlation-cluster cap (limit aggregate exposure to highly-correlated positions).
31. 🔲 Add a configurable max-positions limit and verify it fails closed when exceeded.
32. 🔲 Implement a trailing-stop escalation path the risk manager can apply on winners.
33. 🔲 Add a "time stop" — auto-flatten positions held beyond a max horizon.
34. 🔲 Add a gross + net leverage cap distinct from the per-position cap.
35. 🔲 Implement a daily-loss soft halt (reduce-only) separate from the hard drawdown halt.
36. 🔲 Add a liquidity guard: reject orders whose size exceeds a % of recent average volume.
37. 🔲 Add a stale-data guard: reject signals derived from bars older than a freshness threshold.
38. 🔲 Implement a post-halt recovery protocol (how the system resumes after a HaltEvent clears).
39. 🔲 Add slippage-budget tracking that throttles trading when realized slippage exceeds modeled.
40. 🔲 Add a config-validation layer that rejects internally-inconsistent RiskConfig at startup.
41. 🔲 Expand risk tests for the new caps: each new limit must have a "breach halts/rejects" case.

## D. Data layer

42. 🔲 Add a corporate-actions adjuster (splits/dividends) to the normalizer with tests.
43. ✅ Implement a data-quality report (gaps, duplicates, zero-volume bars) run before any backtest. *(built+tested: `apex/data/quality.py`, 14 tests; not yet called in the backtest/feed pipeline — see F3)*
44. 🔲 Add a Parquet caching layer for Alpaca historical pulls to avoid refetching.
45. 🔲 Add a second data source adapter (e.g. yfinance/Stooq) behind `base_feed` for cross-validation.
46. ✅ Implement multi-timeframe bar aggregation (resample 1m → 5m/1h/1d) deterministically. *(built+tested: `apex/data/resample.py`, 16 tests; not exported from `apex/data/__init__.py` — see F3)*
47. 🔲 Add a survivorship-bias check / delisted-symbol handling note to the historical feed docs.
48. 🔲 Add timezone + market-calendar awareness (half-days, holidays) to the historical feed.
49. 🔲 Implement a live-vs-historical bar reconciliation check at warmup→stream handoff.
50. ✅ Add a bad-tick / outlier filter (price spikes beyond N ATR) to the normalizer. *(built+tested: `apex/data/outlier_filter.py` — ATR & MAD methods, 21 tests; not yet wired into the feed — see F3)*
51. 🔲 Add a feed health-check command that verifies Alpaca connectivity + data freshness.
52. 🔲 Cache and version the ETF universe definition so backtests are reproducible across universe changes.
53. 🔲 Add tests for corporate actions, gap handling, and multi-timeframe aggregation.

## E. Validation Gauntlet

54. 🔲 Add a regime-segmented backtest report (bull/bear/sideways/high-vol breakdown).
55. 🔲 Add a parameter-sensitivity heatmap output to the param-sweep gate.
56. ✅ Implement a deflated Sharpe ratio metric to penalize multiple-testing. *(built+tested: `apex/validation/deflated_sharpe.py` — PSR + DSR, 25 tests; not yet a Gauntlet gate — see F4)*
57. 🔲 Add a PBO (probability of backtest overfitting) estimator as an optional gate.
58. 🔲 Add transaction-cost sensitivity curves (Sharpe vs cost multiplier) to the cost-stress gate.
59. ✅ Add a benchmark-relative report (alpha/beta/information ratio vs SPY). *(built+tested: `apex/validation/benchmark.py` — alpha/beta/IR/TE/capture, 22 tests; not yet a Gauntlet gate — see F4)*
60. ✅ Implement block-bootstrap (preserve autocorrelation) as a Monte Carlo option. *(built+tested: `apex/validation/block_bootstrap.py` — seeded circular blocks, 14 tests; not yet a Gauntlet gate — see F4)*
61. 🔲 Add a "minimum track-record length" estimate to the Gauntlet output.
62. 🔲 Generate a one-page HTML/Markdown Gauntlet report artifact per strategy run.
63. 🔲 Add a Gauntlet regression test: a known-good strategy must keep its grade across refactors.
64. 🔲 Add a Gauntlet CLI (`scripts/gauntlet.py`) that runs any library strategy by name end-to-end.

## F. Execution & live-trading readiness

65. 🔲 Verify the Alpaca data feed against real paper keys (the standing Phase 6 blocker).
66. 🔲 Verify the Alpaca execution adapter against real paper keys (submit/cancel/reconcile round-trip).
67. 🔲 Wire and smoke-test the GitHub Actions cron (`.github/workflows/trade.yml`) end-to-end.
68. 🔲 Add execution idempotency-key persistence so a retried cron run can't double-submit.
69. 🔲 Implement startup broker-reconciliation: compare local state to Alpaca truth, halt on mismatch.
70. 🔲 Add partial-fill handling tests and a partial-fill simulation mode in `simulated.py`.
71. 🔲 Implement order-type support beyond market (limit/stop-limit) with risk-manager validation.
72. 🔲 Add a "smallest viable order" first-live-trade mode (rule from the going-live checklist).
73. 🔲 Add a pre-market / market-hours guard so the cron only trades when the market is open.
74. 🔲 Implement a graceful disconnect → safe-mode (cancel-all + halt) path with tests.
75. 🔲 Add a dry-run / `--no-submit` flag to `run_once` that logs intended orders without sending.

## G. Observability, ops & alerting

76. 🔲 Extend `scripts/report.py` with a rolling 30-day paper-gate progress bar against the rule-17 target.
77. 🔲 Add structured JSON logging with a run-id so each cron cycle is traceable.
78. 🔲 Persist a daily equity/P&L snapshot to state for time-series charting.
79. 🔲 Add ntfy alerts for data-feed failures and missed cron runs (heartbeat).
80. 🔲 Add a weekly summary digest (P&L, Sharpe, drift, top movers) pushed via ntfy.
81. ✅ Add a "system health" command summarizing config, mode, halt state, last run, open positions. *(built+tested: `scripts/health.py`, 15 tests; exit-code aware — see F5)*
82. 🔲 Add log rotation / retention for the `logs/` directory.
83. 🔲 Add a halt-event audit trail (what tripped it, when, what cleared it).
84. 🔲 Add metrics export (Prometheus-style text or CSV) for external dashboarding.
85. 🔲 Document the full ops runbook: how to halt, resume, diagnose a failed cron, rotate keys.

## H. Backtest engine & performance

86. 🔲 Add a backtest result cache keyed on (strategy, params, data hash) for fast re-runs.
87. 🔲 Profile the event loop and remove hot-path allocations without breaking determinism.
88. ✅ Add an equity-curve + drawdown plot output (matplotlib, saved to file, no GUI). *(built: `scripts/plot_equity.py`, Agg backend, lazy import; needs `pip install matplotlib` — see F6)*
89. 🔲 Add a multi-strategy backtest mode that drives the allocation engine.
90. 🔲 Add a backtest-vs-live parity test harness (replay live bars, assert identical decisions).
91. 🔲 Add a deterministic seed-audit test proving identical results across two runs.
92. 🔲 Add a fast smoke-backtest fixture (tiny dataset) for CI to catch regressions cheaply.

## I. Testing & code quality

93. 🔲 Add a property-based test suite (hypothesis) for indicators against insufficient/edge windows.
94. 🔲 Add a mutation-testing pass on the risk manager to prove the tests actually guard it.
95. 🔲 Raise and enforce a coverage floor in CI for `apex/risk/` and `apex/validation/`.
96. 🔲 Add a CI job running ruff + mypy strict on the whole package.
97. 🔲 Add an architectural-fitness test that fails if any strategy imports broker/execution internals.

## J. apex-trader integration (the control surface)

98. 🔲 Define the read-only status export contract (JSON schema) apex-trader consumes — mirror it in both repos.
99. ✅ Implement a `scripts/export_status.py` that writes the current snapshot (equity, positions, halt, per-strategy P&L) for the dashboard. *(built+tested: `scripts/export_status.py`, 11 tests, Decimal→str JSON; not yet wired into cron — see F5, F7)*
100. 🔲 Document the API boundary: what apex-quant exposes vs what stays internal, and the auth/transport plan.

---

## Follow-ups from the 2026-06-06 parallel build (wiring & integration)

The fan-out built 13 new modules (12 backlog items) as **standalone, fully-tested
files** — full suite now **605 tests green** (was 336). The agents were deliberately
forbidden from touching shared files (`__init__.py`, RiskManager, Gauntlet
orchestrator, configs, requirements) to avoid parallel-write conflicts. That
wiring is the next pass:

- **F1 — Register `TimeSeriesMomentumBlend`.** Add it to the strategy factory /
  `apex/strategy/library/__init__.py` and any config YAML so it's selectable at
  runtime. Then run it through the Gauntlet and tune defaults
  (lookbacks/scale/buy_threshold/atr_mult are unvalidated priors).
- **F2 — Wire the gates/weighting into strategies + the allocator.** Have a
  vol-filtered strategy consume `regime.VolatilityRegimeClassifier.is_risk_on()`;
  make the (future) allocation engine (backlog §B) use `strategy/weighting.py`
  instead of inline inverse-vol. Re-export `regime`/`weighting` symbols from
  `apex/strategy/__init__.py`.
- **F3 — Plug the data-hygiene modules into the pipeline.** Call
  `data/quality.data_quality_report` as a pre-backtest gate; insert
  `data/outlier_filter.filter_outliers` into the feed/normalizer path; expose
  `resample.resample_bars` for multi-timeframe strategies. Re-export all three
  from `apex/data/__init__.py`.
- **F4 — Turn the new validation modules into Gauntlet gates.** Integrate
  `deflated_sharpe` (multiple-testing penalty), `benchmark` (alpha/beta/IR vs SPY),
  and `block_bootstrap` (autocorrelation-aware MC) into
  `apex/validation/gauntlet.py` and the grading; export from
  `apex/validation/__init__.py`. Decide trial-count + benchmark series sourcing.
- **F5 — Reconcile ops scripts with production config.** `health.py` and
  `export_status.py` read `AppConfig.from_env()` defaults, not run_once's
  `PRODUCTION_RISK` override — unify so they reflect what the live cron actually runs.
- **F6 — Decide matplotlib.** `plot_equity.py` degrades gracefully without it; if
  charts are wanted, add `matplotlib` to `requirements-dev.txt` (dev-only).
- **F7 — Wire `export_status.py` into the cron** after `run_once` so
  `state/status.json` refreshes each cycle; supply a real per-strategy P&L map
  (Portfolio doesn't track P&L by strategy id today).
- **F8 — Validate, don't just build.** None of the new strategies/edges have
  cleared the Gauntlet on real data. Building them is necessary but not
  sufficient — the Phase-6 unblock is still a genuinely *uncorrelated, cost-clearing*
  edge (backlog §A). These modules are the toolkit, not the result.
- **F9 — Pre-existing lint debt (untouched).** `scripts/fetch_yahoo.py` (E741 ×2)
  had ruff issues before this build; left alone to keep this change additive. Clean
  up in a separate `style:` pass.

---

## 2026-06-06 — Library expansion (100 modules, 20-agent fan-out)

A self-directed 100-agent fan-out added **100 new self-contained modules + 111 test
files**, all disjoint (no shared-file edits). Full suite went **605 → 2325 tests, all
green**; lint clean (only the 2 pre-existing `fetch_yahoo.py` E741 remain). What landed:

- **25 indicators** (`apex/strategy/ind_*.py`): stochastic, adx, cci, williams_r, obv,
  keltner_channels, donchian_channels, aroon, roc, mfi, chaikin_money_flow, trix,
  ultimate_oscillator, supertrend, hull_moving_average, kama, zlema, dema_tema, vortex,
  parabolic_sar, choppiness_index, fisher_transform, coppock_curve, elder_ray,
  standard_error_bands.
- **20 validation/stat tools** (`apex/validation/*.py`): omega_ratio, tail_ratio,
  var_cvar, ulcer_index, drawdown_analysis, rolling_sharpe, cagr_mar, kelly_criterion,
  hit_rate_stats, autocorrelation, hurst_exponent, skew_kurtosis, t_test_returns,
  bootstrap_ci, information_coefficient, variance_ratio_test, turnover_metrics,
  cost_model, capacity_estimate, regime_split_metrics.
- **12 risk analytics** (`apex/risk/*.py`, never the RiskManager): position_sizing,
  correlation_matrix, exposure_report, erc_weights, liquidity_caps, stop_levels,
  drawdown_throttle, leverage_metrics, concentration_metrics, beta_hedge,
  var_limit_check, trade_risk_reward.
- **12 data tools** (`apex/data/*.py`): returns_builder, corporate_actions,
  anchored_vwap, dollar_volume_bars, rolling_zscore, winsorize, synthetic_bars,
  trading_calendar, spread_estimator, ohlc_consistency, frequency_inference,
  returns_aggregator.
- **12 performance analytics** (new `apex/analytics/` package): equity_curve,
  monthly_returns_table, rolling_correlation, performance_summary, trade_analyzer,
  contribution_analysis, benchmark_comparison, drawdown_table, return_distribution,
  underwater_curve, rolling_beta, win_loss_analysis.
- **10 strategy research candidates** (`apex/strategy/library/*.py`, UNVALIDATED):
  bollinger_breakout, macd_trend, donchian_breakout, mean_reversion_zscore,
  volatility_breakout, keltner_trend, roc_momentum, atr_channel_breakout,
  stochastic_reversal, connors_rsi_strategy.
- **9 ops/reporting scripts** (`scripts/*.py`, read-only + pure tested cores):
  perf_report, trade_log_export, compare_strategies, risk_dashboard, returns_csv,
  config_audit, backtest_grid, monthly_report, alerts_preview.

### Follow-ups from this build (still my job — agents could not touch shared files)

- **F10 — Export the new public surface.** None are re-exported from package
  `__init__.py` files (callers must use full paths today). Curate `__all__` for
  `apex/strategy`, `apex/validation`, `apex/data`, `apex/risk`, `apex/analytics`,
  `apex/strategy/library` — decide what is public API vs internal.
- **F11 — Consolidate the indicators.** 25 `ind_*.py` files sit beside the original
  `indicators.py`. Decide: fold them into `indicators.py` / an `indicators/` package,
  or keep flat and re-export. Pick ONE convention before more accrue.
- **F12 — Register & Gauntlet the 10 research strategies.** Wire selected candidates
  into the strategy factory + config, then run each through the Gauntlet. Per the
  project ethos (overfitting is the #1 killer) NONE should be deployed until they
  clear all 7 gates on real data and prove uncorrelated to `multi_asset_trend`.
- **F13 — Wire validation tools into the Gauntlet & reports.** Fold deflated_sharpe,
  benchmark, block_bootstrap (F4) plus the new omega/tail/VaR/ulcer/IC/variance-ratio
  metrics into `gauntlet.py` grading and into `performance_summary`/report output.
- **F14 — Adopt analytics in `scripts/report.py`.** Replace ad-hoc report math with
  the new `apex/analytics/*` helpers so live + backtest reporting share one code path.
- **F15 — Integrate data hygiene into the pipeline.** `corporate_actions`,
  `ohlc_consistency`, `returns_builder`, `trading_calendar`, `synthetic_bars` should be
  used by the feed/backtester/test fixtures rather than sitting idle.
- **F16 — Reconcile risk analytics with the RiskManager.** The new advisory
  calculators (position_sizing, stop_levels, drawdown_throttle, var_limit_check) must
  stay advisory — the RiskManager remains the sole, immutable sizer/gatekeeper. If any
  logic should bind, port it *into* the RiskManager deliberately with its own tests.
- **F17 — Honest status.** This is a tested, reusable *toolkit*, not validated alpha.
  The Phase-6 unblock is still a single genuinely-uncorrelated, cost-clearing edge
  (backlog §A) run through the live paper gate — not 35 more indicators/strategies.
