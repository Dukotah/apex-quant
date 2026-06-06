# Apex Quant — Vision

> My read of what this is, the bar for "fully functional," and what winning looks like.
> Written by the operator. Trust it and move.

## What this is

Apex Quant is an **event-driven, asset-agnostic algorithmic trading framework in Python**
whose defining property is **structural safety**: strategies can only express *intent*
(`SignalEvent`); the `RiskManager` is the *sole* producer of orders and sits physically in
the path between intent and action. A reckless or buggy strategy literally has no pathway to
the broker. Money is `Decimal`, data models are frozen, the backtest is deterministic, and
the paper→live switch is config-only. It runs entirely on free tiers (Alpaca paper/live,
GitHub Actions cron, SQLite) — the only money at risk is trading capital.

It is the **engine**. A sibling project, `apex-trader` (Next.js), is the eventual control
surface; they connect later via API. This repo is the brain.

## Who it's for

The operator (solo quant-founder) compounding their own capital, with the discipline of an
institutional risk desk and the cost base of a hobby project. Secondarily, it's the backend
a small platform could expose to others — but correctness for *one* serious user first.

## State (honest, 2026-06-06)

- **Framework: functionally complete.** Phases 1–6 done. 416 tests, 91% coverage, CI green,
  lint+format clean, and a recent 11-agent correctness audit applied (idempotency,
  determinism, short-side, missed-exit, walk-forward, and more).
- **Live:** the multi-asset trend strategy (7-sleeve inverse-vol, Gauntlet grade A) is
  **live on paper** via the GitHub Actions cron against an Alpaca paper account, ~mid-way
  through the mandatory 30-day paper gate.
- **Research frontier:** one **grade-A second-edge candidate** — single-name value (long-
  horizon reversal) with hysteresis — clears all 7 Gauntlet gates, **BUT on a survivorship-
  biased universe**, so it is *promising, not proven*. This is the single most important open
  question in the project.

## The bar for "fully functional"

The framework bar is **met**: a strategy goes idea → Gauntlet (7 gates) → paper → live by
changing config only; risk fails closed and cannot be bypassed; backtests are deterministic
and have no look-ahead; the live loop self-regulates (drawdown/daily-loss/kill-switch halts,
drift monitor, alerts). What remains to be "fully functional as a *business*," not just as
software:

1. **An edge proven live**, not just in backtest — the trend strategy clears its 30-day
   paper gate with live Sharpe tracking the ~0.82 backtest.
2. **Honest research** — every candidate edge is stress-tested against the ways backtests
   lie (overfitting → handled by the Gauntlet; **survivorship bias → not yet handled**, and
   it is the dominant risk for a buy-the-laggard value strategy).
3. **Operability** — running the live system is a glance at a status view plus trustworthy
   alerts, not log-spelunking.

## What winning looks like (beyond functional)

- **≥2 genuinely uncorrelated edges** running live, each independently Gauntlet-validated
  *and* survivorship-honest, combined by a capital-allocation engine with a clean split.
- **Bulletproof risk** that has been *exercised*, not just unit-tested — a real drawdown
  event halts cleanly in production.
- **A fast, honest research loop**: hypothesis → real-data Gauntlet → deploy-or-kill, with
  every known backtest lie (overfit, look-ahead, survivorship, cost) structurally resisted.
- **$0 infra, compounding capital.** The whole edge is the research and the risk discipline,
  not spend.

The next decisive move is therefore **not** to build the allocation engine (that's the
vehicle for an unproven edge). It is to **de-risk the value edge against survivorship bias**
— turn "grade A on survivors" into a defensible verdict. Everything downstream waits on that.
