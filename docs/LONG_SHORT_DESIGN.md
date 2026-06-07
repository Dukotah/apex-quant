# Long/Short Design Note (Phase 3)

> Status: **backtest/paper infrastructure only.** Short-selling is OFF by default
> (`allow_short=False`) and the deployed long-only bot is byte-identically
> unaffected. Do **not** enable live shorting until the LIVE TODOs below are done.

This note documents the additive, gated long/short support added to the risk +
portfolio core. It is the structural step the DECISIONS log (Sessions 20, 22, 24,
26) repeatedly flagged as the only way to harvest a real market-neutral edge.

---

## What changed (the diff under review)

### `apex/risk/risk_manager.py`

- **`RiskConfig` — three new fields (all default to the long-only behavior):**
  - `allow_short: bool = False` — master switch. When `False`, a SELL that is not
    reducing a long is **blocked** (fail closed). When `True`, such a SELL opens or
    adds a short.
  - `max_gross_exposure_pct: Optional[Decimal] = None` — cap on
    `sum(|position notional|) / equity` (total leverage across both legs). `None` =
    disabled = unchanged.
  - `max_net_exposure_pct: Optional[Decimal] = None` — cap on
    `|signed sum of position notional| / equity` (directional tilt). `None` =
    disabled = unchanged.

- **`evaluate()` — one new gate (step 1c) and one new check (step 6b):**
  - **1c. Short gate.** After the reduce-aware path (1b), if the signal is a SELL
    that is *not* reducing a long (i.e. flat or already short) and `allow_short` is
    `False`, the order is rejected with `"short selling disabled"`. This is placed
    *after* the reduce path so closing/covering is always still allowed.
  - **6b. Gross/net exposure caps** via the new `_check_gross_net_exposure()`,
    enforced right after the existing leverage check. Disabled (both `None`) by
    default → no effect on the existing path.

- **New private method `_check_gross_net_exposure(signal, quantity, portfolio)`:**
  computes the post-fill gross and net exposure of the **combined book** (existing
  positions' signed `market_value` + the proposed order's signed notional: `+` for
  BUY, `−` for SELL) and rejects if either configured cap would be breached. Fails
  closed on missing price / non-positive equity.

- The existing `_resolve_stop_loss` **already** required a SELL's stop to be ABOVE
  the reference price and at least `min_stop_distance_pct` away, and
  `require_stop_loss` already forced a stop's presence — so a short inherits the
  **mandatory above-entry stop** with no new code. The existing `_size_position`
  mirrors long sizing for the short. The existing `_check_leverage` already counts
  the short's (absolute) notional into total exposure.

### `apex/risk/portfolio.py`

- **Two new read-only properties the RiskManager's caps rely on:**
  - `gross_exposure` = `sum(abs(market_value))` (identical to the legacy `exposure`;
    added as an explicitly-named helper).
  - `net_exposure` = `sum(market_value)` (signed; shorts subtract).
- **No change to fill booking.** The portfolio *already* handled shorts correctly
  (open-from-flat via SELL, add-to-short weighted-average entry, partial/exact
  cover with realized P&L, and the long→short / short→long flips). That partial
  logic from earlier sessions (see DECISIONS Session 25) was verified and is now
  covered by explicit tests; it was extended only by the two exposure helpers.

### Tests

- `tests/test_risk_manager.py`: new classes `TestShortGateDisabledByDefault`,
  `TestShortOpening`, `TestShortExposureCaps`. Two pre-existing tests in
  `TestStopWrongSide` (`test_sell_stop_below_price_rejected`,
  `test_sell_stop_above_price_accepted`) were updated to pass `allow_short=True`,
  because they explicitly exercise the short-open path that is now gated — see the
  back-compat note below.
- `tests/test_portfolio.py`: new functions for open/mark/cover/partial-cover/
  add-to-short and gross/net/market-neutral exposure.

---

## The guardrails (why shorting is safe-by-construction here)

A short has **unbounded loss** — price can rise without limit, so an unmanaged
short can bury the whole account, unlike a long which can only go to zero. The
protection is therefore *structural*, not advisory, and every piece fails closed:

1. **Master switch off by default** — nothing can short unless an operator
   explicitly sets `allow_short=True` in a frozen config at startup.
2. **Mandatory stop ABOVE entry** — a short with no valid above-entry stop is
   rejected (reuses the existing `require_stop_loss` + side/distance validation).
   This bounds the per-trade loss.
3. **Gross-exposure cap** — bounds total leverage across both legs.
4. **Net-exposure cap** — bounds how directionally one-sided the book may get.
5. **Leverage cap on the combined book** — the pre-existing `max_leverage` already
   counts short notional.
6. **Single producer unchanged** — shorts are still only ever created by the
   RiskManager's `evaluate()`; no other code path can emit a short OrderEvent.

---

## Backward-compatibility (the deployed bot is unaffected)

- All previously-passing tests stay green **except** the two short-stop tests noted
  above, which were updated to opt into `allow_short=True` because they
  *specifically* test short-opening (the wrong-side-stop and the correct-stop
  cases). Every other existing test is byte-identical.
- **Important nuance:** *before* this change, the RiskManager had no short gate at
  all — a SELL-from-flat with a valid above-entry stop already produced a short
  OrderEvent (the old `test_sell_stop_above_price_accepted` proved this). That
  latent capability was never reachable in production because the deployed
  `MultiAssetTrendStrategy` is long/flat and only ever emits SELL-to-close (the
  reduce path). This change makes the default **stricter** (shorts are now blocked
  unless explicitly enabled), which is *more* conservative for the live bot, not
  less. `PRODUCTION_RISK` in `scripts/run_once.py` does not set `allow_short`, so
  it inherits `False` — the live bot cannot short.

---

## Residual risks

- **Borrow/locate not modeled.** Backtest/paper assumes a short is always
  available to borrow with zero borrow fee. Real shorts can be hard-to-borrow,
  recalled, or carry a borrow rate that erodes the edge. Not represented in P&L.
- **Stops are not slippage-proof.** A protective stop bounds *intended* loss but a
  gap-up through the stop (overnight news) can fill far worse. The gross/net caps
  are the backstop for that tail.
- **No margin-interest / financing cost** on the short proceeds or on leverage.
- **Dividends on borrowed shares** (the short pays the dividend) are not modeled.
- **Pattern-day-trading / margin maintenance** mechanics are broker-side and not
  simulated.

---

## LIVE shorting TODOs (do these before enabling real-money shorts)

1. **Margin account.** Live shorts require a Reg-T margin account at Alpaca (the
   paper account can simulate it, a cash account cannot short). Config/keys gate.
2. **Alpaca short-sell routing in execution** (`apex/execution/alpaca.py`): confirm
   a SELL with no/short inventory routes as a short-sell (Alpaca infers this from a
   negative target, but verify the order-intent mapping and `client_order_id`
   idempotency for the short leg), and that `reconcile_positions` round-trips
   negative quantities (it already maps `qty < 0 → SELL` on seed — verify the
   broker actually reports negatives the way `_reconcile` expects).
3. **Borrow-availability / locate check.** Before approving a short, query Alpaca's
   `shortable` / `easy_to_borrow` flags and reject (fail closed) if not shortable.
   This belongs as an additional pre-trade check feeding the RiskManager (e.g. via
   the symbol metadata or a new injected availability check).
4. **Borrow-fee + margin-interest modeling** in the portfolio/backtest so the
   backtested short edge is net of real carrying costs.
5. **Validate a short-using strategy through the Gauntlet** on a margin-aware
   backtest before any capital — the whole point of the discipline.
6. **Set explicit `max_gross_exposure_pct` / `max_net_exposure_pct` /
   `max_leverage`** in the live `RiskConfig`; they are `None`/`1.0` today.
