"""
apex.risk.capital_allocation
============================
Phase F3.3 — the LIVE, risk-aware multi-strategy capital allocator.

The backtest allocator (``apex.backtest.allocator``) proves a blend on history. This module is
its LIVE counterpart: it splits the book's capital across strategies and scopes each strategy's
ENTRY sizing to its slice — **without touching the immutable RiskManager**.

How it stays golden-rule-clean: the RiskManager remains the sole producer of ``OrderEvent``s and
is unchanged. The allocator simply presents a strategy's entry signal a READ-ONLY portfolio VIEW
whose equity (and the peak / day-start equity the drawdown throttle reads) is scaled by that
strategy's capital weight. Because the RiskManager sizes as a percent of the equity it sees, an
80%-weighted sleeve sizes against 80% of the book — a clean capital split, no new risk surface.

LIVE GATING (build the vehicle, don't fund it). Weights come from
``apex.backtest.allocator.AllocationConfig.live_weights()``, which zeroes any UNFUNDED sleeve. The
value sleeve ships ``funded=False`` until survivorship-free validation clears (W8), so today's live
split is ``{trend: 1.0}`` — and a single-sleeve allocator at weight 1.0 is byte-identical to no
allocator at all. The feature is therefore OFF by construction until a ``funded`` flag flips, and
``AppConfig.allocation`` defaults to ``None`` (the deployed bot supplies no allocator).

Exits are NEVER scoped: a reduce/close signal must be free to flatten its full position, so the
caller (``scripts.run_once``) routes only ENTRY signals through the allocator — reduces always see
the real portfolio.

Disjoint-universe note: trend trades asset-class ETFs and value trades single names, so a sleeve's
positions never overlap another's. Scoping entry sizing to the sleeve's equity slice is therefore
correct; the exposure check runs the whole book's notional against the scaled equity, which can
only REJECT more (never over-trade) — fail-safe.

Money/weights are ``Decimal``. Pure and deterministic; the only state is the frozen weight map.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

# Tolerance for the "weights sum to <= 1" check — guards Decimal/float rounding, not sloppiness.
_WEIGHT_SUM_TOL = Decimal("0.0001")


class _ScopedPortfolio:
    """Read-only portfolio view with the equity scalars scaled by a sleeve's capital weight.

    Scales ``equity``, ``peak_equity`` and ``day_start_equity`` — the fields the RiskManager's
    position sizing and drawdown throttle read — by ``weight``. Everything else
    (``open_positions``, ``exposure``, ``last_price``, broker capital attrs, ...) passes through
    unchanged. Never mutates the wrapped portfolio.

    Note all three equity scalars scale by the SAME factor, so derived ratios the RiskManager
    computes (drawdown = (peak - equity) / peak, daily loss) are unchanged — only the absolute
    sizing base shrinks. That is exactly the intended effect.
    """

    __slots__ = ("_pf", "_w")

    def __init__(self, portfolio: object, weight: Decimal) -> None:
        self._pf = portfolio
        self._w = weight

    @property
    def equity(self) -> Decimal:
        return Decimal(str(self._pf.equity)) * self._w

    @property
    def peak_equity(self) -> Decimal:
        return Decimal(str(self._pf.peak_equity)) * self._w

    @property
    def day_start_equity(self) -> Decimal:
        return Decimal(str(self._pf.day_start_equity)) * self._w

    def __getattr__(self, name: str) -> object:
        # Guard the slots so a pre-init attribute miss can't recurse into itself.
        if name in ("_pf", "_w"):
            raise AttributeError(name)
        # Anything not scaled above passes straight through to the real portfolio.
        return getattr(self._pf, name)


@dataclass(frozen=True)
class CapitalAllocator:
    """An immutable ``strategy_id -> live capital fraction`` map, with fail-closed validation.

    ``weights`` are the LIVE (deployable) fractions — already funded-gated / renormalized by the
    caller (typically ``AllocationConfig.live_weights()``). Each weight must be in ``[0, 1]`` and
    the set must sum to ``<= 1`` (within tolerance). A strategy absent from the map gets ZERO entry
    capital — an unallocated strategy does not trade (fail closed). Construction raises on a
    malformed split rather than silently trading a book that doesn't add up.
    """

    weights: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        norm: dict[str, Decimal] = {}
        for sid, w in dict(self.weights).items():
            wd = w if isinstance(w, Decimal) else Decimal(str(w))
            if not (Decimal("0") <= wd <= Decimal("1")):
                raise ValueError(f"Weight for {sid!r} is {wd}, outside [0, 1].")
            norm[sid] = wd
        total = sum(norm.values(), Decimal("0"))
        if total - Decimal("1") > _WEIGHT_SUM_TOL:
            raise ValueError(f"Live weights must sum to <= 1, got {total}.")
        object.__setattr__(self, "weights", norm)

    def weight_for(self, strategy_id: str) -> Decimal:
        """This strategy's live capital fraction; ``0`` if unallocated (fail closed)."""
        return self.weights.get(strategy_id, Decimal("0"))

    def scoped(self, portfolio: object, strategy_id: str) -> object:
        """A capital-scoped, read-only view of ``portfolio`` for this strategy's ENTRY sizing.

        At weight 1.0 the view sizes identically to the portfolio itself, so a single-sleeve
        allocator is a no-op — which is why the deployed 100%-trend book is unaffected.
        """
        return _ScopedPortfolio(portfolio, self.weight_for(strategy_id))

    @classmethod
    def from_live_weights(cls, weights: Mapping[str, float]) -> "CapitalAllocator":
        """Build from ``AllocationConfig.live_weights()`` (float fractions -> Decimal)."""
        return cls({sid: Decimal(str(w)) for sid, w in weights.items()})

    @classmethod
    def single(cls, strategy_id: str) -> "CapitalAllocator":
        """The trivial 100%-to-one allocator (today's deployed shape: all capital to trend)."""
        return cls({strategy_id: Decimal("1")})
