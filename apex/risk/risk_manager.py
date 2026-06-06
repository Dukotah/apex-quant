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
from datetime import timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional

from apex.core.clock import Clock
from apex.core.events import HaltEvent, OrderEvent, SignalEvent
from apex.core.models import OrderSide, OrderType, Symbol, utc_now

logger = logging.getLogger("apex.risk")


@dataclass(frozen=True)
class RiskConfig:
    """
    Immutable risk parameters. Frozen so nothing can mutate limits at runtime.
    Loaded once at startup. These are HARD limits, not suggestions.
    """
    max_position_size_pct: Decimal = Decimal("0.05")   # 5% of equity per position
    max_total_exposure_pct: Decimal = Decimal("0.50")  # 50% of equity deployed at once
    max_leverage: Decimal = Decimal("1.0")             # 1.0 = no leverage
    max_drawdown_pct: Decimal = Decimal("0.10")        # 10% from peak halts trading
    max_daily_loss_pct: Decimal = Decimal("0.02")      # 2% daily loss halts for the day
    max_open_positions: int = 10
    require_stop_loss: bool = True                      # every order needs a protective stop
    min_stop_distance_pct: Decimal = Decimal("0.005")  # stop must be >= 0.5% away
    symbol_whitelist: Optional[frozenset] = None       # None = all allowed

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
    drawdown_throttle_floor: Decimal = Decimal("0.30") # smallest size multiplier (>0)

    # --- Volatility targeting (scale exposure toward a target realized vol) ---
    # When set, new entries are sized by target_volatility / portfolio_realized_vol,
    # clamped to [vol_scale_min, vol_scale_max]. The book de-risks when realized vol
    # runs hot and re-risks when it cools — the standard managed-futures overlay. With
    # max_scale 1.0 (and no leverage) it acts purely as a turbulence de-risker.
    # None disables it (default, so existing configs are unchanged).
    target_volatility: Optional[Decimal] = None        # annualized, e.g. 0.10 = 10%
    vol_scale_min: Decimal = Decimal("0.4")            # floor on the exposure multiplier
    vol_scale_max: Decimal = Decimal("1.0")            # cap (1.0 = never lever past full)

    # --- Stale-data guard (reject signals built on stale bars) -----------------
    # If set, a signal whose bar timestamp is older than this many seconds (per the
    # injected Clock — NEVER datetime.now()) is rejected: trading on stale data is a
    # silent way to lose money when a feed stalls. None disables it (default off, so
    # existing configs are unchanged). FAIL CLOSED: when enabled but the signal has no
    # timestamp, or no Clock was injected, the signal is rejected.
    max_bar_age_seconds: Optional[int] = None

    # --- Sector / asset-class concentration caps -------------------------------
    # Block over-concentration into one sector or asset class. Each cap is a fraction
    # of equity; a new entry is rejected if it would push that bucket's gross notional
    # past the cap. `sector_map` maps ticker -> sector label. None disables a cap
    # (default off). FAIL CLOSED: if a sector cap is set but a ticker is missing from
    # `sector_map`, the unknown symbol is treated as its own worst-case bucket and the
    # check still applies (an unmapped symbol cannot hide from the cap).
    max_sector_exposure_pct: Optional[Decimal] = None
    max_asset_class_exposure_pct: Optional[Decimal] = None
    sector_map: Optional[Mapping[str, str]] = None

    # --- Correlated-exposure cap ----------------------------------------------
    # Cap gross notional across an explicitly-declared correlated GROUP of tickers
    # (e.g. all the mega-cap tech names that move together). `correlation_groups` maps
    # a group label -> the set of tickers in it. A new entry whose group's combined
    # gross notional would exceed `max_correlated_exposure_pct * equity` is rejected.
    # None disables it (default off).
    max_correlated_exposure_pct: Optional[Decimal] = None
    correlation_groups: Optional[Mapping[str, frozenset]] = None

    # --- Hard notional limits (independent of % sizing) ------------------------
    # Absolute dollar caps that backstop the percentage-based sizing. `max_trade_notional`
    # caps any single new order's gross notional; `max_daily_notional` caps the cumulative
    # gross notional of all NEW entries opened in a trading day (reset by reset_daily()).
    # None disables a cap (default off). These are HARD floors that cannot be widened by
    # a large equity base or an aggressive strength.
    max_trade_notional: Optional[Decimal] = None
    max_daily_notional: Optional[Decimal] = None

    # --- ATR-based stop validation + trailing-stop support ---------------------
    # When `require_atr_stop` is True, an entry must supply an ATR (read off the signal as
    # `atr`) and a stop whose distance from the reference price is within
    # [atr_stop_min_multiple, atr_stop_max_multiple] * ATR. A stop tighter than the min is
    # noise-tight (whipsaw); wider than the max is too loose (oversized risk). Validation
    # only — the risk manager NEVER places the stop, it just refuses orders with a bad one.
    # `allow_trailing_stop` gates whether a signal flagged `trailing_stop=True` is accepted
    # (a trailing stop is still validated as a normal protective stop at entry). Defaults are
    # off / permissive so existing configs are unchanged.
    require_atr_stop: bool = False
    atr_stop_min_multiple: Decimal = Decimal("0.5")
    atr_stop_max_multiple: Decimal = Decimal("5.0")
    allow_trailing_stop: bool = True

    # --- Consecutive-rejection / error circuit breaker -------------------------
    # If `max_consecutive_rejections` consecutive evaluate() calls fail (a rejection OR a
    # fail-closed error), the manager HALTS the whole system and surfaces a HaltEvent (see
    # `last_halt_event`). A storm of rejections usually means something is structurally
    # wrong (bad feed, mis-wired strategy) and continuing to churn is itself a risk. A
    # single APPROVED order resets the counter. None disables it (default off).
    max_consecutive_rejections: Optional[int] = None


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

    def __init__(self, config: RiskConfig, clock: Optional[Clock] = None) -> None:
        self._config = config           # private + frozen = tamper-resistant
        self._clock = clock             # injected time source for the stale-data guard
        self._halted: bool = False
        self._halt_reason: str = ""
        self._last_halt_event: Optional[HaltEvent] = None
        # Per-day cumulative gross notional of NEW entries (for max_daily_notional).
        self._daily_notional: Decimal = Decimal("0")
        # Consecutive rejection/error count for the circuit breaker.
        self._consecutive_rejections: int = 0

    # ---- public API -------------------------------------------------------

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def last_halt_event(self) -> Optional[HaltEvent]:
        """The most recent HaltEvent this manager raised, or None. Read-only."""
        return self._last_halt_event

    @property
    def consecutive_rejections(self) -> int:
        """Count of consecutive failed evaluations (rejection or error)."""
        return self._consecutive_rejections

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
            halt = self._check_circuit_breakers(portfolio)
            if halt is not None:
                self._trigger_halt(halt.reason, halt.triggered_by, halt)
                self._reject(signal, halt.reason)
                return None

            # 1a. Stale-data guard. A signal built on a bar older than the configured
            # max age is rejected — trading on stale data when a feed stalls silently
            # bleeds money. FAIL CLOSED: enabled-but-unmeasurable => reject.
            if not self._check_bar_freshness(signal):
                self._reject(signal, "signal bar is stale (or unmeasurable)")
                return None

            # 1b. Reduce-aware path. Closing or trimming an existing position is
            # ALWAYS risk-reducing, so it is sized to flatten (never exceeding the
            # held quantity) and is exempt from the entry-side caps (exposure,
            # leverage) and the mandatory-stop requirement — you must always be
            # able to de-risk. Entry behaviour below is unchanged.
            if self._is_reducing(signal, portfolio):
                return self._evaluate_reduce(signal, portfolio)

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

            # 4a. ATR-based stop validation + trailing-stop gate. Validated, never placed.
            if not self._check_atr_stop(signal, stop, portfolio):
                self._reject(signal, "stop fails ATR validation / trailing not allowed")
                return None

            # 5. Position sizing (returns 0 if no room within exposure limits).
            quantity = self._size_position(signal, portfolio)
            if quantity <= 0:
                self._reject(signal, "sizing produced zero quantity (exposure cap)")
                return None

            # 5a. Sector / asset-class / correlated concentration caps.
            if not self._check_concentration(signal, quantity, portfolio):
                self._reject(signal, "would breach a concentration cap")
                return None

            # 5b. Hard notional limits (per-trade and per-day), independent of % sizing.
            if not self._check_notional_limits(signal, quantity, portfolio):
                self._reject(signal, "would breach a hard notional limit")
                return None

            # 6. Leverage check on the resulting portfolio.
            if not self._check_leverage(signal, quantity, portfolio):
                self._reject(signal, "would exceed max leverage")
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
                timestamp=utc_now(),
            )
            # Book the new entry's gross notional against the daily limit and clear
            # the consecutive-rejection streak — a healthy approval breaks the storm.
            self._daily_notional += self._order_notional(signal, quantity, portfolio)
            self._consecutive_rejections = 0
            logger.info(
                "APPROVED %s %s qty=%s stop=%s (strategy=%s)",
                signal.side.value, signal.symbol, quantity, stop, signal.strategy_id,
            )
            return order

        except Exception as exc:  # FAIL CLOSED — any error = reject.
            logger.error("Risk check errored, rejecting signal: %s", exc, exc_info=True)
            self._reject(signal, f"risk-check exception: {exc}")
            return None

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
        return (
            (qty > 0 and signal.side == OrderSide.SELL)
            or (qty < 0 and signal.side == OrderSide.BUY)
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
            stop_loss=signal.suggested_stop_loss,        # optional on an exit
            take_profit=signal.suggested_take_profit,
            strategy_id=signal.strategy_id,
            signal_id=signal.event_id,
            timestamp=utc_now(),
        )
        # A successful de-risk is a healthy outcome — clear the rejection streak.
        self._consecutive_rejections = 0
        logger.info(
            "APPROVED (reduce) %s %s qty=%s (strategy=%s)",
            signal.side.value, signal.symbol, qty, signal.strategy_id,
        )
        return order

    # ---- individual checks (private) --------------------------------------

    def _check_circuit_breakers(self, portfolio) -> Optional[HaltEvent]:
        """Max drawdown and max daily loss. These halt the WHOLE system."""
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
                    timestamp=utc_now(),
                )

        if day_start > 0:
            daily_loss = (day_start - equity) / day_start
            if daily_loss >= self._config.max_daily_loss_pct:
                return HaltEvent(
                    reason=f"max daily loss breached: {daily_loss:.2%} >= "
                           f"{self._config.max_daily_loss_pct:.2%}",
                    triggered_by="max_daily_loss",
                    timestamp=utc_now(),
                )
        return None

    def _check_bar_freshness(self, signal: SignalEvent) -> bool:
        """
        Stale-data guard. Returns True (pass) when the guard is disabled. When enabled
        (`max_bar_age_seconds` set), the signal must carry a timezone-aware timestamp and
        a Clock must have been injected; the bar's age (now - timestamp) must be within the
        limit. FAIL CLOSED: any missing piece, a future-dated bar, or any error => reject.
        """
        max_age = self._config.max_bar_age_seconds
        if max_age is None:
            return True
        if self._clock is None:
            logger.warning("stale-data guard enabled but no Clock injected — rejecting")
            return False
        ts = getattr(signal, "timestamp", None)
        if ts is None or getattr(ts, "tzinfo", None) is None:
            return False
        now = self._clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age = (now - ts).total_seconds()
        # A negative age means the bar is dated in the FUTURE — treat as corrupt → reject.
        if age < 0:
            return False
        return age <= float(max_age)

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
        target_dollars = equity * self._config.max_position_size_pct * strength * throttle * vol_mult

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
        frac = (drawdown - start) / (full - start)          # 0..1 across the ramp
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

    # ---- ATR-based stop validation + trailing-stop gate -------------------

    def _check_atr_stop(
        self, signal: SignalEvent, stop: Optional[Decimal], portfolio
    ) -> bool:
        """
        Validate the protective stop against ATR and gate trailing stops. Validation
        ONLY — the manager never places the stop, it refuses orders with a bad one.

        Trailing gate: a signal flagged `trailing_stop=True` is rejected when
        `allow_trailing_stop` is False. The trailing stop is otherwise validated as a
        normal protective stop at entry (it still needs a valid initial level).

        ATR gate (only when `require_atr_stop`): the signal must carry a positive `atr`,
        a resolvable reference price, and a valid stop whose distance from price lies in
        [atr_stop_min_multiple, atr_stop_max_multiple] * ATR. FAIL CLOSED on anything
        missing or malformed.
        """
        # Trailing-stop gate (independent of the ATR requirement).
        trailing = bool(getattr(signal, "trailing_stop", False))
        if trailing and not self._config.allow_trailing_stop:
            return False

        if not self._config.require_atr_stop:
            return True

        # ATR requirement is ON — a valid stop must already exist.
        if stop is None:
            return False
        atr_raw = getattr(signal, "atr", None)
        if atr_raw is None:
            return False
        try:
            atr = Decimal(str(atr_raw))
        except (InvalidOperation, ValueError, TypeError):
            return False
        if atr <= 0:
            return False

        price = self._reference_price(signal.symbol, portfolio)
        if price is None or price <= 0:
            return False

        distance = abs(price - stop)
        lo = self._config.atr_stop_min_multiple * atr
        hi = self._config.atr_stop_max_multiple * atr
        return lo <= distance <= hi

    # ---- concentration caps (sector / asset-class / correlated) ----------

    def _check_concentration(
        self, signal: SignalEvent, quantity: Decimal, portfolio
    ) -> bool:
        """
        Block over-concentration. Each enabled cap compares a bucket's projected gross
        notional (existing held notional in the bucket + this new order's notional)
        against `cap_pct * equity`. Returns True (pass) when every enabled cap holds and
        when caps are disabled. FAIL CLOSED: any error during computation rejects.
        """
        equity = Decimal(str(portfolio.equity))
        if equity <= 0:
            return False
        new_notional = self._order_notional(signal, quantity, portfolio)
        ticker = signal.symbol.ticker

        # --- sector cap ---
        sec_cap = self._config.max_sector_exposure_pct
        if sec_cap is not None:
            sector = self._sector_of(ticker)
            held = self._bucket_notional(portfolio, lambda t: self._sector_of(t) == sector)
            if held + new_notional > sec_cap * equity:
                return False

        # --- asset-class cap ---
        ac_cap = self._config.max_asset_class_exposure_pct
        if ac_cap is not None:
            ac = signal.symbol.asset_class
            held = self._bucket_notional(
                portfolio, lambda t: self._asset_class_of(t, portfolio) == ac
            )
            if held + new_notional > ac_cap * equity:
                return False

        # --- correlated-group cap ---
        corr_cap = self._config.max_correlated_exposure_pct
        groups = self._config.correlation_groups
        if corr_cap is not None and groups:
            for members in groups.values():
                if ticker in members:
                    held = self._bucket_notional(portfolio, lambda t: t in members)
                    if held + new_notional > corr_cap * equity:
                        return False
        return True

    def _sector_of(self, ticker: str) -> str:
        """
        Sector label for a ticker. An unmapped ticker FAILS CLOSED into a unique bucket
        keyed on itself, so a missing mapping can never let a symbol dodge the cap (it is
        simply treated as its own isolated sector and capped accordingly).
        """
        smap = self._config.sector_map
        if smap is None:
            return f"__unknown__:{ticker}"
        return smap.get(ticker, f"__unknown__:{ticker}")

    def _asset_class_of(self, ticker: str, portfolio):
        """Asset class for a held ticker via its Position.symbol; None if not held."""
        pos = portfolio.open_positions.get(ticker)
        if pos is None:
            return None
        return pos.symbol.asset_class

    def _bucket_notional(self, portfolio, predicate) -> Decimal:
        """Sum of abs(market_value) over held positions whose ticker matches predicate."""
        total = Decimal("0")
        for tkr, pos in portfolio.open_positions.items():
            if predicate(tkr):
                total += abs(Decimal(str(pos.market_value)))
        return total

    # ---- hard notional limits --------------------------------------------

    def _check_notional_limits(
        self, signal: SignalEvent, quantity: Decimal, portfolio
    ) -> bool:
        """
        Per-trade and per-day HARD notional caps, independent of % sizing. Returns True
        when both pass / are disabled. FAIL CLOSED on any error.
        """
        notional = self._order_notional(signal, quantity, portfolio)
        per_trade = self._config.max_trade_notional
        if per_trade is not None and notional > per_trade:
            return False
        per_day = self._config.max_daily_notional
        if per_day is not None and (self._daily_notional + notional) > per_day:
            return False
        return True

    def _order_notional(
        self, signal: SignalEvent, quantity: Decimal, portfolio
    ) -> Decimal:
        """Gross notional of a prospective order = qty * price * contract_multiplier."""
        price = self._reference_price(signal.symbol, portfolio)
        if price is None:
            raise ValueError(f"no reference price for {signal.symbol.ticker}")
        return quantity * price * signal.symbol.contract_multiplier

    # ---- helpers ----------------------------------------------------------

    def _reference_price(self, symbol: Symbol, portfolio) -> Optional[Decimal]:
        """Latest known price for sizing. Provided by the portfolio snapshot."""
        return portfolio.last_price.get(symbol.ticker)

    def _trigger_halt(
        self, reason: str, triggered_by: str, halt_event: Optional[HaltEvent] = None
    ) -> None:
        if not self._halted:
            self._halted = True
            self._halt_reason = reason
            self._last_halt_event = halt_event or HaltEvent(
                reason=reason, triggered_by=triggered_by, timestamp=utc_now()
            )
            logger.critical("TRADING HALTED [%s]: %s", triggered_by, reason)

    def _reject(self, signal: SignalEvent, reason: str) -> None:
        logger.warning(
            "REJECTED %s %s (strategy=%s): %s",
            signal.side.value if signal.side else "?",
            signal.symbol, signal.strategy_id, reason,
        )
        # Consecutive-rejection / error circuit breaker. A storm of failures usually
        # means something structural is broken; halting beats churning. Counting lives
        # here so EVERY rejection path (including the fail-closed exception handler)
        # contributes, with no extra wiring at each call site.
        self._consecutive_rejections += 1
        limit = self._config.max_consecutive_rejections
        if (
            limit is not None
            and not self._halted
            and self._consecutive_rejections >= limit
        ):
            self._trigger_halt(
                f"consecutive-rejection circuit breaker: "
                f"{self._consecutive_rejections} >= {limit}",
                "consecutive_rejections",
            )

    def reset_daily(self) -> None:
        """
        Call at the start of each trading day. Clears the daily-loss halt and resets the
        per-day notional tally. (A max-drawdown halt is sticky and is NOT cleared here.)
        """
        self._daily_notional = Decimal("0")
        if self._halt_reason and "daily loss" in self._halt_reason:
            self._halted = False
            self._halt_reason = ""
            logger.info("Daily risk state reset.")
