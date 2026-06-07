"""
apex.strategy.library.options_income
=====================================
Defined-risk options income strategies: CoveredCall, CashSecuredPut, BullPutSpread.

ARCHITECTURE NOTES
------------------
These classes produce OptionSignal objects, NOT the equity SignalEvent used by
linear strategies.  OptionSignal is defined here (a frozen dataclass) because the
options path through risk → execution is not yet wired.  The integration steps the
maintainer must perform are documented at the bottom of this module.

DEFINED-RISK ONLY — every structure in this module has a finite, pre-computed
maximum loss.  Naked short calls (unbounded upside risk to the seller) are
explicitly excluded and will never appear here.

DETERMINISM
-----------
All strike selection is deterministic:
  1. Filter to the nearest expiry >= min_dte calendar days from now.
  2. Among qualifying quotes, pick the one whose |delta - target_delta| is smallest.
  3. On ties, prefer the higher strike (puts) or lower strike (calls) — i.e. the
     more OTM leg wins ties, consistent with income-strategy conservatism.
  4. If no greeks are available, fall back to the closest strike above/below spot.

NO I/O — strategies are pure functions of their inputs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from apex.core.models import Symbol

# ---------------------------------------------------------------------------
# Import the option primitives produced by the sibling agent.
# If apex.core.option is not yet on disk the ImportError is caught here and
# callers get a clear message rather than a silent AttributeError.
# ---------------------------------------------------------------------------
try:
    from apex.core.option import (  # type: ignore[import-not-found]
        OptionLeg,
        OptionOrder,
        OptionQuote,
        OptionRight,
        OptionType,
    )

    _OPTION_MODULE_AVAILABLE = True
except ImportError:  # pragma: no cover — expected until sibling agent lands
    _OPTION_MODULE_AVAILABLE = False
    raise ImportError(
        "apex.core.option is not yet available.  "
        "The sibling agent that creates it must run first.  "
        "Tests use a local shim; see tests/test_options_income.py."
    )

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OptionSignal — the options-specific intent object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionSignal:
    """
    A defined-risk options intent, analogous to SignalEvent for linear instruments.

    Fields
    ------
    order       : The multi-leg OptionOrder expressing the trade structure.
    strategy_id : Identifier of the originating strategy instance.
    reason      : Human-readable rationale (for audit / AI diagnostics).
    max_loss    : Pre-computed, finite maximum loss in dollars.  The risk manager
                  MUST assert ``max_loss > 0`` and reserve this cash before
                  accepting the signal.

    Integration path (maintainer TODO)
    ------------------------------------
    OptionSignals are currently a *terminal* output — they are not wired through
    the RiskManager → ExecutionEngine path.  To complete the integration:

    1. Add an ``OptionSignal`` branch to ``RiskManager.evaluate()`` that:
       a. Checks ``max_loss`` against available buying power.
       b. Confirms the structure is defined-risk (all legs present, no orphaned
          short legs).
       c. Emits an ``OptionOrderEvent`` (a new event type you must add to
          ``apex/core/events.py``) that carries the full ``OptionOrder``.

    2. Add ``OptionOrderEvent`` handling to the execution engine so it can route
       multi-leg orders to the broker (Alpaca option legs endpoint).

    3. Add options position tracking to ``Portfolio`` (delta, theta, max-loss
       reserved cash).

    Until those three steps are done, OptionSignals are safe to generate in paper
    mode — they will simply be logged and not acted upon.
    """

    order: OptionOrder
    strategy_id: str
    reason: str
    max_loss: Decimal  # always positive, always finite


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DAYS_IN_YEAR = 365


def _calendar_days(from_date: date, to_date: date) -> int:
    """Return calendar day count between two dates (non-negative)."""
    return max(0, (to_date - from_date).days)


def _midpoint(quote: OptionQuote) -> Decimal:
    """Natural fill estimate: midpoint of bid/ask."""
    return (quote.bid + quote.ask) / Decimal("2")


def _select_expiry(
    chain: list[OptionQuote],
    as_of: date,
    min_dte: int,
) -> date | None:
    """
    Return the nearest expiry in ``chain`` that is >= ``min_dte`` calendar days
    from ``as_of``, or None if no qualifying expiry exists.
    """
    qualifying: set[date] = set()
    for q in chain:
        dte = _calendar_days(as_of, q.contract.expiry)
        if dte >= min_dte:
            qualifying.add(q.contract.expiry)
    if not qualifying:
        return None
    return min(qualifying)


def _filter_chain(
    chain: list[OptionQuote],
    option_type: OptionType,
    expiry: date,
) -> list[OptionQuote]:
    """Return all quotes matching option_type and expiry."""
    return [
        q for q in chain if q.contract.option_type == option_type and q.contract.expiry == expiry
    ]


def _pick_by_delta(
    quotes: list[OptionQuote],
    target_delta: float,
    prefer_higher_strike: bool,
) -> OptionQuote | None:
    """
    Select the quote whose |delta - target_delta| is smallest.

    Tie-break: if two quotes are equally close to target_delta, prefer the one
    with the higher strike when prefer_higher_strike=True (more OTM for puts),
    or lower strike when False (more OTM for calls).

    Falls back to strike-proximity when greeks are absent (greeks=None on all
    matching quotes).
    """
    if not quotes:
        return None

    has_greeks = any(q.greeks is not None for q in quotes)

    if has_greeks:

        def _sort_key_greek(q: OptionQuote) -> tuple:
            delta = q.greeks.delta if q.greeks is not None else float("inf")
            distance = abs(delta - target_delta)
            # tie-break: negate strike if we prefer higher (so higher sorts first with -)
            tie = -float(q.contract.strike) if prefer_higher_strike else float(q.contract.strike)
            return (distance, tie)

        return min(quotes, key=_sort_key_greek)

    # No greeks — fall back to closest strike to target_delta interpreted as
    # a fraction-of-spot offset.  For our purposes, just take the quote whose
    # strike is furthest OTM from spot given the preference direction.
    if prefer_higher_strike:
        return max(quotes, key=lambda q: q.contract.strike)
    return min(quotes, key=lambda q: q.contract.strike)


def _pick_by_strike_otm(
    quotes: list[OptionQuote],
    spot: Decimal,
    option_type: OptionType,
    target_delta: float,
) -> OptionQuote | None:
    """
    Pure-strike fallback: find the quote closest to spot*(1 ± k) where k is
    approximated from target_delta (linear approximation; good enough for strike
    selection when greeks are missing).
    """
    if not quotes:
        return None
    # For calls: target OTM strike above spot.  For puts: below spot.
    target_strike = (
        spot * Decimal(str(1.0 + (1.0 - abs(target_delta)) * 0.05))
        if option_type == OptionType.CALL
        else spot * Decimal(str(1.0 - (1.0 - abs(target_delta)) * 0.05))
    )

    def _dist(q: OptionQuote) -> tuple:
        d = abs(q.contract.strike - target_strike)
        return (d, -q.contract.strike if option_type == OptionType.PUT else q.contract.strike)

    return min(quotes, key=_dist)


# ---------------------------------------------------------------------------
# CoveredCall
# ---------------------------------------------------------------------------


class CoveredCall:
    """
    Covered-call income strategy.

    Assumes the caller holds 100 shares (one contract multiplier) of the
    underlying.  Sells a single OTM call.

    Risk profile: defined.  Maximum loss = current stock price - premium
    received (i.e. the downside is the stock declining, but that loss is
    bounded by the stock reaching zero — the same risk as holding the stock
    outright).  Upside is capped at the strike.

    Args
    ----
    strategy_id     : Unique identifier for this instance.
    underlying      : The Symbol for the held equity.
    target_delta    : Delta of the call to sell (default 0.30 — moderately OTM).
    min_dte         : Minimum calendar days to expiry (default 21 — avoid
                      gamma blowup in the final week).
    contract_mult   : Shares per contract (default 100).
    """

    def __init__(
        self,
        strategy_id: str,
        underlying: Symbol,
        target_delta: float = 0.30,
        min_dte: int = 21,
        contract_mult: int = 100,
    ) -> None:
        self.strategy_id = strategy_id
        self.underlying = underlying
        self.target_delta = target_delta
        self.min_dte = min_dte
        self.contract_mult = contract_mult

    def evaluate(
        self,
        spot: Decimal,
        chain: list[OptionQuote],
        as_of: date,
    ) -> OptionSignal | None:
        """
        Given the current underlying price and an option chain, return an
        OptionSignal to sell one OTM call, or None if no suitable contract exists.

        Parameters
        ----------
        spot   : Current price of the underlying.
        chain  : Full option chain (may contain puts, calls, multiple expiries).
        as_of  : The current date (injected for determinism — never datetime.now()).
        """
        expiry = _select_expiry(chain, as_of, self.min_dte)
        if expiry is None:
            _log.debug("CoveredCall[%s]: no expiry >= %d DTE", self.strategy_id, self.min_dte)
            return None

        calls = _filter_chain(chain, OptionType.CALL, expiry)
        # Only OTM calls (strike > spot)
        otm_calls = [q for q in calls if q.contract.strike > spot]
        if not otm_calls:
            _log.debug("CoveredCall[%s]: no OTM calls available", self.strategy_id)
            return None

        selected = _pick_by_delta(otm_calls, self.target_delta, prefer_higher_strike=False)
        if selected is None:
            return None

        premium = _midpoint(selected)
        # Max loss per share: stock goes to zero, offset by premium received.
        # We express max_loss as the net downside risk on the covered position.
        # Conventional covered-call max_loss = (spot - premium) * contract_mult.
        max_loss = (spot - premium) * Decimal(str(self.contract_mult))
        assert max_loss > 0, "max_loss must be positive (spot > premium always for OTM)"

        leg = OptionLeg(contract=selected.contract, right=OptionRight.SELL, ratio=1)
        order = OptionOrder(legs=(leg,), quantity=1, limit_price=premium)

        return OptionSignal(
            order=order,
            strategy_id=self.strategy_id,
            reason=(
                f"Covered call: sell {selected.contract.occ_symbol} "
                f"@ {premium:.4f} mid, delta≈{self.target_delta:.2f}, "
                f"DTE={_calendar_days(as_of, expiry)}"
            ),
            max_loss=max_loss,
        )


# ---------------------------------------------------------------------------
# CashSecuredPut
# ---------------------------------------------------------------------------


class CashSecuredPut:
    """
    Cash-secured put income strategy.

    Sells an OTM put with full cash reservation equal to the strike price.
    The put buyer has the right to put shares to us at the strike; the maximum
    loss (net of premium) is strike - premium, which equals the cost of buying
    100 shares at strike and immediately selling at zero.  This is finite and
    fully reserved by the broker before the trade is accepted.

    Args
    ----
    strategy_id     : Unique identifier for this instance.
    underlying      : The Symbol for the target equity.
    target_delta    : Absolute delta of the put to sell (default 0.30).
                      Convention: put deltas are negative, but we accept 0.30
                      and compare against abs(delta).
    min_dte         : Minimum calendar days to expiry (default 21).
    contract_mult   : Shares per contract (default 100).
    """

    def __init__(
        self,
        strategy_id: str,
        underlying: Symbol,
        target_delta: float = 0.30,
        min_dte: int = 21,
        contract_mult: int = 100,
    ) -> None:
        self.strategy_id = strategy_id
        self.underlying = underlying
        self.target_delta = target_delta
        self.min_dte = min_dte
        self.contract_mult = contract_mult

    def evaluate(
        self,
        spot: Decimal,
        chain: list[OptionQuote],
        as_of: date,
    ) -> OptionSignal | None:
        """
        Return an OptionSignal to sell one OTM put, cash-secured, or None.

        max_loss = (strike - premium) * contract_mult
        This is the net cost of being assigned and taking delivery at strike
        when the underlying has gone to zero — the absolute worst case.
        """
        expiry = _select_expiry(chain, as_of, self.min_dte)
        if expiry is None:
            _log.debug("CSP[%s]: no expiry >= %d DTE", self.strategy_id, self.min_dte)
            return None

        puts = _filter_chain(chain, OptionType.PUT, expiry)
        # Only OTM puts (strike < spot)
        otm_puts = [q for q in puts if q.contract.strike < spot]
        if not otm_puts:
            _log.debug("CSP[%s]: no OTM puts available", self.strategy_id)
            return None

        # For puts, delta is negative; we compare abs(delta) to target_delta.
        # _pick_by_delta expects the raw delta in the greeks, so we pass
        # -target_delta (negative) as the target.
        selected = _pick_by_delta(otm_puts, -self.target_delta, prefer_higher_strike=True)
        if selected is None:
            return None

        premium = _midpoint(selected)
        strike = selected.contract.strike
        # Cash reserved = strike * contract_mult (broker requirement)
        # Net max_loss = (strike - premium) * contract_mult
        max_loss = (strike - premium) * Decimal(str(self.contract_mult))
        assert max_loss > 0, f"max_loss must be positive; strike={strike}, premium={premium}"

        leg = OptionLeg(contract=selected.contract, right=OptionRight.SELL, ratio=1)
        order = OptionOrder(legs=(leg,), quantity=1, limit_price=premium)

        return OptionSignal(
            order=order,
            strategy_id=self.strategy_id,
            reason=(
                f"Cash-secured put: sell {selected.contract.occ_symbol} "
                f"@ {premium:.4f} mid, strike={strike}, "
                f"max_loss={max_loss:.2f}, DTE={_calendar_days(as_of, expiry)}"
            ),
            max_loss=max_loss,
        )


# ---------------------------------------------------------------------------
# BullPutSpread
# ---------------------------------------------------------------------------


class BullPutSpread:
    """
    Bull put spread (vertical credit spread) — strictly defined-risk.

    Structure:
      SELL  higher-strike put  (short leg, collects credit)
      BUY   lower-strike put   (long leg, defines maximum loss)

    Net credit = premium_sold - premium_bought (both at mid).
    Maximum loss = (width_of_spread - net_credit) * contract_mult
    where width = short_strike - long_strike.

    The maximum loss is fixed at order entry and cannot grow — the long put
    provides a hard floor.  This is what makes the structure defined-risk.

    Args
    ----
    strategy_id        : Unique identifier for this instance.
    underlying         : The Symbol for the target equity.
    short_delta        : |Delta| of the short (sold) put, closer to ATM (default 0.30).
    long_delta         : |Delta| of the long (bought) put, further OTM (default 0.15).
    min_dte            : Minimum calendar days to expiry (default 21).
    contract_mult      : Shares per contract (default 100).
    """

    def __init__(
        self,
        strategy_id: str,
        underlying: Symbol,
        short_delta: float = 0.30,
        long_delta: float = 0.15,
        min_dte: int = 21,
        contract_mult: int = 100,
    ) -> None:
        if long_delta >= short_delta:
            raise ValueError(
                f"long_delta ({long_delta}) must be < short_delta ({short_delta}) "
                "so the long leg is further OTM"
            )
        self.strategy_id = strategy_id
        self.underlying = underlying
        self.short_delta = short_delta
        self.long_delta = long_delta
        self.min_dte = min_dte
        self.contract_mult = contract_mult

    def evaluate(
        self,
        spot: Decimal,
        chain: list[OptionQuote],
        as_of: date,
    ) -> OptionSignal | None:
        """
        Return an OptionSignal for a 2-leg bull put spread, or None.

        The two legs share the same expiry.  If the chain does not have at
        least two distinct OTM put strikes, returns None.
        """
        expiry = _select_expiry(chain, as_of, self.min_dte)
        if expiry is None:
            _log.debug("BPS[%s]: no expiry >= %d DTE", self.strategy_id, self.min_dte)
            return None

        puts = _filter_chain(chain, OptionType.PUT, expiry)
        otm_puts = [q for q in puts if q.contract.strike < spot]
        if len(otm_puts) < 2:
            _log.debug("BPS[%s]: need >=2 OTM puts, got %d", self.strategy_id, len(otm_puts))
            return None

        # Short leg: closer to ATM (higher |delta|, i.e. higher strike among OTM puts)
        short_leg_quote = _pick_by_delta(otm_puts, -self.short_delta, prefer_higher_strike=True)
        if short_leg_quote is None:
            return None

        # Long leg: further OTM (lower |delta|, lower strike) — must be a different quote
        long_candidates = [
            q for q in otm_puts if q.contract.strike < short_leg_quote.contract.strike
        ]
        if not long_candidates:
            _log.debug(
                "BPS[%s]: no put strikes below short leg %s",
                self.strategy_id,
                short_leg_quote.contract.strike,
            )
            return None

        long_leg_quote = _pick_by_delta(
            long_candidates, -self.long_delta, prefer_higher_strike=True
        )
        if long_leg_quote is None:
            return None

        short_premium = _midpoint(short_leg_quote)
        long_premium = _midpoint(long_leg_quote)
        net_credit = short_premium - long_premium  # positive: we collect cash
        if net_credit <= 0:
            _log.debug("BPS[%s]: net credit non-positive (%s), skip", self.strategy_id, net_credit)
            return None

        short_strike = short_leg_quote.contract.strike
        long_strike = long_leg_quote.contract.strike
        width = short_strike - long_strike
        assert width > 0, "spread width must be positive"

        max_loss = (width - net_credit) * Decimal(str(self.contract_mult))
        assert max_loss > 0, (
            f"max_loss must be positive (got {max_loss}); width={width}, credit={net_credit}"
        )

        short_leg = OptionLeg(
            contract=short_leg_quote.contract,
            right=OptionRight.SELL,
            ratio=1,
        )
        long_leg = OptionLeg(
            contract=long_leg_quote.contract,
            right=OptionRight.BUY,
            ratio=1,
        )
        # Canonical order: short leg first, long leg second
        order = OptionOrder(legs=(short_leg, long_leg), quantity=1, limit_price=net_credit)

        return OptionSignal(
            order=order,
            strategy_id=self.strategy_id,
            reason=(
                f"Bull put spread: sell {short_leg_quote.contract.occ_symbol} "
                f"/ buy {long_leg_quote.contract.occ_symbol}, "
                f"credit={net_credit:.4f}, width={width}, max_loss={max_loss:.2f}, "
                f"DTE={_calendar_days(as_of, expiry)}"
            ),
            max_loss=max_loss,
        )
