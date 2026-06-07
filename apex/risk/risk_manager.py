"""
apex.risk.risk_manager
======================
The RiskManager is the single most important block in this system.

It sits structurally between INTENT (SignalEvent) and ACTION (OrderEvent).
Every signal produced by every strategy MUST pass through it. There is no
code path that lets a strategy reach the execution engine directly — by
design, the only producer of OrderEvents is this class.

Key properties:
  - NOT abstract. There is exactly one risk policy, and strategies cannot
    subclass, override, or weaken it.
  - Risk parameters are loaded once at startup from an immutable config and
    cannot be changed at runtime by any strategy.
  - Checks FAIL CLOSED: if a check errors, the signal is REJECTED. The default
    outcome of any uncertainty is "no trade", never "trade".
  - A max-drawdown breach emits a HaltEvent that stops ALL new orders.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from apex.core.events import HaltEvent, OptionOrderEvent, OrderEvent, SignalEvent
from apex.core.models import OrderSide, OrderType, Symbol, utc_now
from apex.core.option import OptionOrder

logger = logging.getLogger("apex.risk")


@dataclass(frozen=True)
class RiskConfig:
    """
    Immutable risk parameters. Frozen so nothing can mutate limits at runtime.
    Loaded once at startup. These are HARD limits, not suggestions.
    """

    max_position_size_pct: Decimal = Decimal("0.05")  # 5% of equity per position
    max_total_exposure_pct: Decimal = Decimal("0.50")  # 50% of equity deployed at once
    max_leverage: Decimal = Decimal("1.0")  # 1.0 = no leverage
    max_drawdown_pct: Decimal = Decimal("0.10")  # 10% from peak halts trading
    max_daily_loss_pct: Decimal = Decimal("0.02")  # 2% daily loss halts for the day
    max_open_positions: int = 10
    require_stop_loss: bool = True  # every order needs a protective stop
    min_stop_distance_pct: Decimal = Decimal("0.005")  # stop must be >= 0.5% away
    symbol_whitelist: Optional[frozenset] = None  # None = all allowed

    # --- Drawdown sizing throttle (de-risk as the equity bleeds) ---
    # As drawdown-from-peak grows past `start`, NEW entries are sized down linearly
    # to `floor`x by the time drawdown reaches `full`, then held at `floor`x. This
    # protects the live equity PATH (you bet smaller while losing, so a bad run is
    # survivable) — it is the standard managed-futures answer to a strategy whose
    # realistic drawdown is large. Independent of the hard `max_drawdown_pct` halt,
    # which remains the catastrophe backstop. `start=None` disables it (the default,
    # so existing configs are unchanged).
    drawdown_throttle_start: Optional[Decimal] = None  # DD where down-sizing begins
    drawdown_throttle_full: Decimal = Decimal("0.30")  # DD where `floor`x is reached
    drawdown_throttle_floor: Decimal = Decimal("0.30")  # smallest size multiplier (>0)

    # --- Volatility targeting (scale exposure toward a target realized vol) ---
    # When set, new entries are sized by target_volatility / portfolio_realized_vol,
    # clamped to [vol_scale_min, vol_scale_max]. The book de-risks when realized vol
    # runs hot and re-risks when it cools — the standard managed-futures overlay. With
    # max_scale 1.0 (and no leverage) it acts purely as a turbulence de-risker.
    # None disables it (default, so existing configs are unchanged).
    target_volatility: Optional[Decimal] = None  # annualized, e.g. 0.10 = 10%
    vol_scale_min: Decimal = Decimal("0.4")  # floor on the exposure multiplier
    vol_scale_max: Decimal = Decimal("1.0")  # cap (1.0 = never lever past full)

    # --- Short-selling (Phase 3, long/short) ---------------------------------
    # OFF by default. When False, a SELL only ever REDUCES/closes an existing long;
    # a SELL on a flat (or already-short) symbol is BLOCKED — the system stays
    # strictly long-only, exactly as the deployed bot requires. When True, a SELL
    # on a flat/short symbol OPENS or ADDS a short: it is sized like a long, but
    # FAILS CLOSED unless it carries a mandatory protective stop ABOVE entry
    # (a short loses money when price rises), and it must clear the gross/net
    # exposure caps and the leverage cap on the COMBINED book.
    #
    # WHY THESE GUARDRAILS ARE STRUCTURAL, NOT OPTIONAL: a short has UNBOUNDED loss
    # (price can rise without limit). A long can only go to zero; a short can bury
    # the account. The mandatory above-entry stop bounds the per-trade loss; the
    # gross-exposure cap bounds total leverage across both legs; the net-exposure
    # cap bounds directional risk. Together they are the only thing standing between
    # this code and an unlimited-loss event, so they fail closed.
    allow_short: bool = False

    # Gross exposure = sum(|position notional|) / equity — total capital at risk
    # across BOTH long and short legs (a market-neutral 50/50 book is 100% gross).
    # Bounds total leverage when both sides are open. None = disabled (unchanged).
    max_gross_exposure_pct: Optional[Decimal] = None

    # Net exposure = |signed sum of position notional| / equity — the directional
    # tilt (longs minus shorts). Bounds how one-sided the book may get. None =
    # disabled (unchanged).
    max_net_exposure_pct: Optional[Decimal] = None


class TradingHaltError(Exception):
    """Raised when trading is globally halted (drawdown/daily-loss breach)."""


class RiskManager:
    """
    Intercepts every SignalEvent. Produces an OrderEvent only if ALL checks pass.

    Usage in the engine loop:
        order = risk_manager.evaluate(signal, portfolio_snapshot)
        if order is not None:
            execution_engine.submit(order)
        # else: signal was rejected, nothing happens (logged internally)
    """

    def __init__(self, config: RiskConfig) -> None:
        self._config = config  # private + frozen = tamper-resistant
        self._halted: bool = False
        self._halt_reason: str = ""
        self._halt_triggered_by: str = ""  # structured cause, not the human-readable reason

    # ---- public API -------------------------------------------------------

    @property
    def is_halted(self) -> bool:
        return self._halted

    def evaluate(self, signal: SignalEvent, portfolio) -> Optional[OrderEvent]:
        """
        The gate. Returns a sized OrderEvent if the signal passes every check,
        otherwise None. NEVER raises for a normal rejection — rejection is a
        valid, expected outcome that simply produces no order.

        `portfolio` is a read-only snapshot exposing: equity, peak_equity,
        day_start_equity, open_positions (dict), exposure.
        """
        try:
            # 0. Global halt state — if halted, nothing trades.
            if self._halted:
                self._reject(signal, f"system halted: {self._halt_reason}")
                return None

            # 1. Drawdown / daily-loss circuit breakers (may trigger global halt).
            halt = self._check_circuit_breakers(portfolio, signal.timestamp)
            if halt is not None:
                self._trigger_halt(halt.reason, halt.triggered_by)
                self._reject(signal, halt.reason)
                return None

            # 1b. Reduce-aware path. Closing or trimming an existing position is
            # ALWAYS risk-reducing, so it is sized to flatten (never exceeding the
            # held quantity) and is exempt from the entry-side caps (exposure,
            # leverage) and the mandatory-stop requirement — you must always be
            # able to de-risk. Entry behaviour below is unchanged.
            if self._is_reducing(signal, portfolio):
                return self._evaluate_reduce(signal, portfolio)

            # 1c. Short gate. A SELL that is NOT reducing a long is an OPEN/ADD
            # SHORT (flat or already short). When shorting is disabled this is
            # blocked outright — the system stays strictly long-only and the
            # deployed bot is unaffected. FAIL CLOSED: the default is no-short.
            if signal.side == OrderSide.SELL and not self._config.allow_short:
                self._reject(signal, "short selling disabled (allow_short=False)")
                return None

            # 2. Symbol whitelist.
            if not self._check_whitelist(signal.symbol):
                self._reject(signal, f"{signal.symbol} not in whitelist")
                return None

            # 3. Max open positions.
            if not self._check_position_count(signal, portfolio):
                self._reject(signal, "max open positions reached")
                return None

            # 4. Mandatory stop-loss.
            stop = self._resolve_stop_loss(signal, portfolio)
            if self._config.require_stop_loss and stop is None:
                self._reject(signal, "missing/invalid mandatory stop-loss")
                return None

            # 5. Position sizing (returns 0 if no room within exposure limits).
            quantity = self._size_position(signal, portfolio)
            if quantity <= 0:
                self._reject(signal, "sizing produced zero quantity (exposure cap)")
                return None

            # 6. Leverage check on the resulting portfolio.
            if not self._check_leverage(signal, quantity, portfolio):
                self._reject(signal, "would exceed max leverage")
                return None

            # 6b. Gross / net exposure caps on the COMBINED long+short book. These
            # are the structural protection for shorting (a short's loss is
            # unbounded), so they fail closed: if configured and the resulting book
            # would breach either cap, the order is rejected. Disabled (None) by
            # default → no effect on the existing long-only path.
            if not self._check_gross_net_exposure(signal, quantity, portfolio):
                self._reject(signal, "would exceed gross/net exposure cap")
                return None

            # ALL CHECKS PASSED → build the approved, sized order.
            take_profit = signal.suggested_take_profit
            order = OrderEvent(
                symbol=signal.symbol,
                side=signal.side,
                quantity=quantity,
                order_type=OrderType.MARKET,
                stop_loss=stop,
                take_profit=take_profit,
                strategy_id=signal.strategy_id,
                signal_id=signal.event_id,
                timestamp=signal.timestamp or utc_now(),
            )
            logger.info(
                "APPROVED %s %s qty=%s stop=%s (strategy=%s)",
                signal.side.value,
                signal.symbol,
                quantity,
                stop,
                signal.strategy_id,
            )
            return order

        except Exception as exc:  # FAIL CLOSED — any error = reject.
            logger.error("Risk check errored, rejecting signal: %s", exc, exc_info=True)
            self._reject(signal, f"risk-check exception: {exc}")
            return None

    # ---- defined-risk options gate (the options golden rule) --------------

    def evaluate_option(
        self,
        *,
        order: OptionOrder,
        max_loss: Decimal,
        strategy_id: str,
        reason: str,
        portfolio,
    ) -> Optional[OptionOrderEvent]:
        """
        The options gate — the analogue of ``evaluate`` for the multi-leg options
        path. An ``OptionOrder`` is INTENT; it can only become an executable
        ``OptionOrderEvent`` by passing every check here. This method is the SOLE
        producer of ``OptionOrderEvent``, so options honor the golden rule
        ("strategies cannot place orders") structurally — the execution engine must
        never submit a raw ``OptionOrder``.

        Inputs are PRIMITIVE core types only (``OptionOrder`` + ``max_loss``) — risk
        must not import from apex.strategy.* (layering). FAILS CLOSED: returns an
        ``OptionOrderEvent`` only if ALL hold, else logs a rejection and returns None
        (never raises for a normal rejection):
          1. system is not halted (a drawdown/daily-loss halt blocks options too);
          2. ``max_loss`` is finite and > 0 — DEFINED-RISK only (inf / NaN / <= 0 fail);
          3. ``max_loss`` <= available cash/buying power (worst case reserved);
          4. the order has >= 1 leg AND a positive integer ``quantity``.
        """
        try:
            if self._halted:
                self._reject_option(strategy_id, order, f"system halted: {self._halt_reason}")
                return None

            try:
                loss = Decimal(str(max_loss))
            except (InvalidOperation, ValueError, TypeError):
                self._reject_option(strategy_id, order, f"max_loss not a number: {max_loss!r}")
                return None
            if not loss.is_finite():
                self._reject_option(strategy_id, order, f"max_loss not finite: {loss}")
                return None
            if loss <= 0:
                self._reject_option(
                    strategy_id, order, f"max_loss must be > 0 (defined-risk), got {loss}"
                )
                return None

            legs = getattr(order, "legs", None)
            if not legs or len(legs) < 1:
                self._reject_option(strategy_id, order, "order has no legs")
                return None
            quantity = getattr(order, "quantity", 0)
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
                self._reject_option(
                    strategy_id, order, f"order quantity must be > 0, got {quantity}"
                )
                return None

            available = self._available_funds(portfolio)
            if loss > available:
                self._reject_option(
                    strategy_id, order, f"max_loss {loss} exceeds available funds {available}"
                )
                return None

            # ALL CHECKS PASSED → build the approved, defined-risk option order.
            ts = getattr(portfolio, "timestamp", None) or utc_now()
            event = OptionOrderEvent(
                order=order,
                strategy_id=strategy_id,
                reason=reason,
                max_loss=loss,
                timestamp=ts,
            )
            logger.info(
                "APPROVED OPTION %s legs=%d qty=%s max_loss=%s (strategy=%s)",
                order.legs[0].contract.underlying.ticker,
                len(order.legs),
                order.quantity,
                loss,
                strategy_id,
            )
            return event
        except Exception as exc:  # FAIL CLOSED — any error = reject.
            logger.error("Option risk check errored, rejecting order: %s", exc, exc_info=True)
            self._reject_option(strategy_id, order, f"option risk-check exception: {exc}")
            return None

    def _available_funds(self, portfolio) -> Decimal:
        """Funds the option's max_loss is reserved against: buying_power, else cash,
        else equity; 0 (fail-closed) if none readable."""
        for attr in ("buying_power", "cash", "equity"):
            val = getattr(portfolio, attr, None)
            if val is not None:
                return Decimal(str(val))
        return Decimal("0")

    def _reject_option(self, strategy_id: str, order: OptionOrder, reason: str) -> None:
        underlying = "?"
        try:
            if order is not None and getattr(order, "legs", None):
                underlying = order.legs[0].contract.underlying.ticker
        except Exception:  # never let logging mask the real rejection
            pass
        logger.warning("REJECTED OPTION %s (strategy=%s): %s", underlying, strategy_id, reason)

    # ---- reduce-aware exit path (always allowed to de-risk) ---------------

    def _is_reducing(self, signal: SignalEvent, portfolio) -> bool:
        """
        True if this signal would REDUCE/close an existing position:
        a SELL while net long, or a BUY while net short. Adding to a position
        (BUY while long / SELL while short) is NOT reducing — it takes the
        normal entry path with full exposure/leverage/stop checks.
        """
        held = portfolio.open_positions.get(signal.symbol.ticker)
        if held is None:
            return False
        qty = held.quantity
        return (qty > 0 and signal.side == OrderSide.SELL) or (
            qty < 0 and signal.side == OrderSide.BUY
        )

    def _evaluate_reduce(self, signal: SignalEvent, portfolio) -> Optional[OrderEvent]:
        """
        Build a flatten/trim OrderEvent for a reducing signal. Sized to the held
        quantity scaled by conviction (strength 1.0 = full exit), never more than
        is held — so a reduce can never overshoot into an opposite-side position.
        No exposure/leverage/stop checks: de-risking is unconditionally permitted.
        """
        held = portfolio.open_positions.get(signal.symbol.ticker)
        held_qty = abs(held.quantity)
        strength = max(Decimal("0"), min(Decimal("1"), Decimal(str(signal.strength))))

        if signal.symbol.fractionable:
            qty = (held_qty * strength).quantize(Decimal("0.0001"))
        else:
            qty = Decimal(int(held_qty * strength))
        qty = min(qty, held_qty)

        if qty <= 0:
            self._reject(signal, "reduce sizing produced zero quantity")
            return None

        order = OrderEvent(
            symbol=signal.symbol,
            side=signal.side,
            quantity=qty,
            order_type=OrderType.MARKET,
            stop_loss=signal.suggested_stop_loss,  # optional on an exit
            take_profit=signal.suggested_take_profit,
            strategy_id=signal.strategy_id,
            signal_id=signal.event_id,
            timestamp=utc_now(),
        )
        logger.info(
            "APPROVED (reduce) %s %s qty=%s (strategy=%s)",
            signal.side.value,
            signal.symbol,
            qty,
            signal.strategy_id,
        )
        return order

    # ---- individual checks (private) --------------------------------------

    def _check_circuit_breakers(
        self, portfolio, when: Optional[datetime] = None
    ) -> Optional[HaltEvent]:
        """Max drawdown and max daily loss. These halt the WHOLE system."""
        ts = when or utc_now()  # bar-time when available → deterministic audit trail
        equity = Decimal(str(portfolio.equity))
        peak = Decimal(str(portfolio.peak_equity))
        day_start = Decimal(str(portfolio.day_start_equity))

        if peak > 0:
            drawdown = (peak - equity) / peak
            if drawdown >= self._config.max_drawdown_pct:
                return HaltEvent(
                    reason=f"max drawdown breached: {drawdown:.2%} >= "
                    f"{self._config.max_drawdown_pct:.2%}",
                    triggered_by="max_drawdown",
                    timestamp=ts,
                )

        if day_start > 0:
            daily_loss = (day_start - equity) / day_start
            if daily_loss >= self._config.max_daily_loss_pct:
                return HaltEvent(
                    reason=f"max daily loss breached: {daily_loss:.2%} >= "
                    f"{self._config.max_daily_loss_pct:.2%}",
                    triggered_by="max_daily_loss",
                    timestamp=ts,
                )
        return None

    def _check_whitelist(self, symbol: Symbol) -> bool:
        if self._config.symbol_whitelist is None:
            return True
        return symbol.ticker in self._config.symbol_whitelist

    def _check_position_count(self, signal: SignalEvent, portfolio) -> bool:
        # Adding to / closing an existing position is fine; only opening NEW
        # positions counts against the limit.
        existing = portfolio.open_positions.get(signal.symbol.ticker)
        if existing is not None:
            return True
        return len(portfolio.open_positions) < self._config.max_open_positions

    def _resolve_stop_loss(self, signal: SignalEvent, portfolio) -> Optional[Decimal]:
        """
        Use the strategy's suggested stop if valid; otherwise None.
        Validates the stop is on the correct side and far enough away.
        """
        stop = signal.suggested_stop_loss
        if stop is None:
            return None
        price = self._reference_price(signal.symbol, portfolio)
        if price is None or price <= 0:
            return None
        distance = abs(price - stop) / price
        if distance < self._config.min_stop_distance_pct:
            return None
        # Stop must be below entry for longs, above for shorts.
        if signal.side == OrderSide.BUY and stop >= price:
            return None
        if signal.side == OrderSide.SELL and stop <= price:
            return None
        return stop

    def _size_position(self, signal: SignalEvent, portfolio) -> Decimal:
        """
        Convert intent + conviction into a concrete quantity, capped by both
        per-position and total-exposure limits. This is where 'strength'
        from the signal scales the size (within hard caps).
        """
        equity = Decimal(str(portfolio.equity))
        price = self._reference_price(signal.symbol, portfolio)
        if price is None or price <= 0 or equity <= 0:
            return Decimal("0")

        # Per-position cap, scaled by conviction (strength 0..1), the drawdown
        # throttle (de-risk in a slump), and the volatility-target multiplier
        # (de-risk when realized vol runs hot). All default to 1.0 when disabled.
        strength = max(Decimal("0"), min(Decimal("1"), Decimal(str(signal.strength))))
        throttle = self._drawdown_throttle(portfolio)
        vol_mult = self._vol_target_multiplier(portfolio)
        target_dollars = (
            equity * self._config.max_position_size_pct * strength * throttle * vol_mult
        )

        # Respect remaining room under total exposure cap.
        max_exposure = equity * self._config.max_total_exposure_pct
        current_exposure = Decimal(str(portfolio.exposure))
        remaining = max_exposure - current_exposure
        if remaining <= 0:
            return Decimal("0")
        target_dollars = min(target_dollars, remaining)

        multiplier = signal.symbol.contract_multiplier
        raw_qty = target_dollars / (price * multiplier)

        # Whole units unless the instrument is fractionable (e.g. crypto).
        if signal.symbol.fractionable:
            return raw_qty.quantize(Decimal("0.0001"))
        return Decimal(int(raw_qty))

    def _drawdown_throttle(self, portfolio) -> Decimal:
        """
        Size multiplier in [floor, 1] based on current drawdown from peak. Full size
        until drawdown exceeds `start`, then ramps linearly down to `floor` by `full`,
        and stays at `floor` beyond. Returns 1 (no effect) when disabled or when there
        is no peak yet. Fails OPEN to 1 only structurally — any bad config collapses
        to the safe full-throttle path, and the hard drawdown halt still backs it up.
        """
        start = self._config.drawdown_throttle_start
        if start is None:
            return Decimal("1")
        peak = Decimal(str(portfolio.peak_equity))
        equity = Decimal(str(portfolio.equity))
        if peak <= 0:
            return Decimal("1")
        drawdown = (peak - equity) / peak
        if drawdown <= start:
            return Decimal("1")
        floor = self._config.drawdown_throttle_floor
        full = self._config.drawdown_throttle_full
        if drawdown >= full or full <= start:
            return floor
        frac = (drawdown - start) / (full - start)  # 0..1 across the ramp
        return Decimal("1") - frac * (Decimal("1") - floor)

    def _vol_target_multiplier(self, portfolio) -> Decimal:
        """
        Exposure multiplier in [vol_scale_min, vol_scale_max] = target_vol /
        realized_vol. De-risks when the portfolio's realized volatility exceeds the
        target. Returns 1 when disabled or while realized vol is still warming up.
        """
        target = self._config.target_volatility
        if target is None:
            return Decimal("1")
        rv = getattr(portfolio, "realized_volatility", None)
        if rv is None or rv <= 0:
            return Decimal("1")
        mult = target / Decimal(str(rv))
        return max(self._config.vol_scale_min, min(self._config.vol_scale_max, mult))

    def _check_leverage(self, signal: SignalEvent, quantity: Decimal, portfolio) -> bool:
        equity = Decimal(str(portfolio.equity))
        if equity <= 0:
            return False
        price = self._reference_price(signal.symbol, portfolio)
        if price is None:
            return False
        new_notional = quantity * price * signal.symbol.contract_multiplier
        total_notional = Decimal(str(portfolio.exposure)) + new_notional
        leverage = total_notional / equity
        return leverage <= self._config.max_leverage

    def _check_gross_net_exposure(self, signal: SignalEvent, quantity: Decimal, portfolio) -> bool:
        """
        Enforce the gross- and net-exposure caps on the book AFTER this order fills.

          gross = sum(|signed notional|) / equity   (total leverage across both legs)
          net   = |sum(signed notional)| / equity   (directional tilt, longs-shorts)

        Returns True when neither cap is configured (the default → unchanged) or
        when both are satisfied. FAILS CLOSED: a missing price or non-positive
        equity returns False, so an order is rejected rather than waved through on
        incomplete data. The new order adds to a same-side position (reduces/covers
        are handled earlier), so its signed notional is +notional for a BUY and
        −notional for a SELL (short).
        """
        gross_cap = self._config.max_gross_exposure_pct
        net_cap = self._config.max_net_exposure_pct
        if gross_cap is None and net_cap is None:
            return True

        equity = Decimal(str(portfolio.equity))
        if equity <= 0:
            return False
        price = self._reference_price(signal.symbol, portfolio)
        if price is None or price <= 0:
            return False

        # Signed notional of every currently-held position.
        signed_long_short: Decimal = Decimal("0")  # net (signed) sum
        gross: Decimal = Decimal("0")  # sum of absolutes
        for pos in portfolio.open_positions.values():
            mv = Decimal(str(pos.market_value))  # already signed (qty can be < 0)
            signed_long_short += mv
            gross += abs(mv)

        # Fold in the proposed order's signed notional.
        new_notional = quantity * price * signal.symbol.contract_multiplier
        signed_new = new_notional if signal.side == OrderSide.BUY else -new_notional

        # The proposed symbol may already be held (an ADD on the same side); its
        # contribution simply increases the magnitude of that leg, so adding the
        # new signed notional to both the running net and gross is correct because
        # same-side adds never cross zero (opposite-side moves are the reduce path).
        new_net = abs(signed_long_short + signed_new) / equity
        new_gross = (gross + abs(new_notional)) / equity

        if gross_cap is not None and new_gross > gross_cap:
            return False
        if net_cap is not None and new_net > net_cap:
            return False
        return True

    # ---- helpers ----------------------------------------------------------

    def _reference_price(self, symbol: Symbol, portfolio) -> Optional[Decimal]:
        """Latest known price for sizing. Provided by the portfolio snapshot."""
        return portfolio.last_price.get(symbol.ticker)

    def _trigger_halt(self, reason: str, triggered_by: str) -> None:
        if not self._halted:
            self._halted = True
            self._halt_reason = reason
            self._halt_triggered_by = triggered_by
            logger.critical("TRADING HALTED [%s]: %s", triggered_by, reason)

    def _reject(self, signal: SignalEvent, reason: str) -> None:
        logger.warning(
            "REJECTED %s %s (strategy=%s): %s",
            signal.side.value if signal.side else "?",
            signal.symbol,
            signal.strategy_id,
            reason,
        )

    def reset_daily(self) -> None:
        """Call at the start of each trading day to clear the daily-loss halt."""
        if self._halt_triggered_by == "max_daily_loss":
            self._halted = False
            self._halt_reason = ""
            self._halt_triggered_by = ""
            logger.info("Daily risk state reset.")
