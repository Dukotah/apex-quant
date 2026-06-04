# Apex Quant — Decisions Log

> Running record of design decisions. **Paste this at the start of every session**
> so Claude has continuity. **Append to it at the end of every session.**
> Newest entries at the top.

---

## Session 0 — Framework Foundation (initial scaffold)

**What was built:**
- `apex/core/models.py` — asset-agnostic frozen data models: `Bar`, `Tick`,
  `Symbol` (with `AssetClass` enum + contract multiplier for futures/crypto),
  `Position`, and order enums. Bars self-validate in `__post_init__` (reject
  negative prices, high<low, naive timestamps).
- `apex/core/events.py` — the event taxonomy: `MarketEvent`, `SignalEvent`,
  `OrderEvent`, `FillEvent`, `HaltEvent`. All frozen. Each event carries a UUID
  and links back to its parent (signal_id, order_id) for full traceability.
- `apex/core/config.py` — `AppConfig` with the `ExecutionMode` switch
  (backtest/paper/live) and `Broker` enum. `from_env()` raises if LIVE is paired
  with the simulated broker (safety).
- `apex/data/base_feed.py` — `BaseDataFeed` ABC. Context-manager support.
- `apex/strategy/base_strategy.py` — `BaseStrategy` ABC with `on_bar`/`on_tick`
  hooks + `StrategyContext` (read-only state view).
- `apex/risk/risk_manager.py` — `RiskManager` (the gatekeeper) + frozen
  `RiskConfig`. Smoke-tested and working.
- `apex/execution/base_execution.py` — `BaseExecutionEngine` ABC with the
  paper/live abstraction and fill-callback wiring.

**Key design decisions (the "why"):**
- **Signals carry conviction (`strength` 0..1), not quantity.** Strategies express
  *how confident*, the risk manager translates that into *how many shares* within
  hard caps. This keeps sizing logic in exactly one place.
- **Position sizing lives inside the RiskManager**, not a separate sizer module
  (for now). It's tightly coupled to the exposure/leverage checks, so co-locating
  prevents a signal being "approved" then separately "sized too big."
- **Risk checks fail closed** — the `evaluate()` method wraps everything in
  try/except and REJECTS on any exception. Safety beats availability.
- **Drawdown breach sets a persistent `_halted` flag** on the risk manager and
  emits a `HaltEvent`. Daily-loss halts can be reset via `reset_daily()`;
  drawdown halts are sticky until manual review.
- **Decimal everywhere for money.** No floats in price/quantity/cash math.
- **All timestamps UTC, timezone-aware**, enforced at model construction.

**Verified:**
- Smoke test passed: compliant signal → 25 shares of AAPL ($100k equity × 5% ÷
  $200); missing stop → rejected; 15% drawdown → system halt + rejection.

**Free-stack decisions:**
- Broker: **Alpaca** (free paper, commission-free live, IEX free data feed).
- Runtime: **GitHub Actions cron** for scheduled runs (free, public repo);
  **Oracle Cloud Always Free** VM if a persistent process is ever needed.
- State: **SQLite** in-repo to start (zero setup); Supabase free tier if the
  dashboard sibling project is wired in.
- AI: strategies authored in **Claude Pro chat** and pasted in → $0 API cost.
  Runtime is fully deterministic and calls no LLM.

**Next:** Phase 1 finish — `event_bus.py` + `clock.py` + model/event tests.

---

<!-- TEMPLATE for future sessions — copy this block:

## Session N — <title>

**What was built:**
- 

**Key design decisions:**
- 

**Verified:**
- 

**Next:**
- 

-->
