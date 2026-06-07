"""
apex.data.synthetic_bars
========================
Seeded geometric-Brownian-motion ``Bar`` generator for tests and backtests.

Real market data is messy to obtain, large, and — fatally for a test suite —
not reproducible from a short specification. This module manufactures a stream
of frozen, validated ``Bar`` models from nothing but a seed and a handful of
parameters, so a test can say "give me 250 daily bars of a stock that drifts up
8%/yr with 20% annualized vol, seed=7" and get the *exact same* bars every run,
on every machine.

The price path is a discrete geometric Brownian motion::

    S_{t+1} = S_t * exp((mu - sigma**2 / 2) * dt + sigma * sqrt(dt) * Z_t)

with ``Z_t`` drawn from a seeded ``numpy`` generator. GBM is the canonical
model for asset prices: it is multiplicative (prices stay positive — a hard
requirement for ``Bar``), its log-returns are normal, and ``mu`` / ``sigma`` map
directly onto the annualized drift and volatility a test wants to express.

Determinism (golden rule 10): the only randomness comes from
``numpy.random.default_rng(seed)``; there is no ``datetime.now()`` — the caller
injects ``start`` (or accepts the explicit default). Same inputs → identical
bars, always.

Money boundary (golden rule 14): the *statistical* path is computed in ``float``
(matching the metrics/indicator layer convention), but every value crossing into
a ``Bar`` is quantized and passed through ``str()`` into ``Decimal`` exactly as
``apex.data.normalizer`` does, so no binary-float artifact reaches price math.

This module is pure: no file or network I/O. ``generate_bars`` returns a list of
``Bar``; ``generate_market_events`` wraps each in a ``MarketEvent`` so a synthetic
feed is a drop-in for ``HistoricalDataFeed`` output.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional, Sequence

import numpy as np

from apex.core.events import MarketEvent
from apex.core.models import Bar, Symbol

# Trading periods per year, by timeframe, used to scale annualized mu/sigma down
# to a per-bar dt. Daily uses 252 trading days; intraday assumes a 6.5h session
# (390 minutes) over 252 days; calendar timeframes use their literal counts.
_PERIODS_PER_YEAR: dict[str, float] = {
    "1Min": 252.0 * 390.0,
    "5Min": 252.0 * 78.0,
    "15Min": 252.0 * 26.0,
    "1Hour": 252.0 * 6.5,
    "1Day": 252.0,
    "1Week": 52.0,
    "1Month": 12.0,
}

# How far to advance ``timestamp`` between consecutive bars, by timeframe.
_STEP: dict[str, timedelta] = {
    "1Min": timedelta(minutes=1),
    "5Min": timedelta(minutes=5),
    "15Min": timedelta(minutes=15),
    "1Hour": timedelta(hours=1),
    "1Day": timedelta(days=1),
    "1Week": timedelta(weeks=1),
    "1Month": timedelta(days=30),
}

# Default first-bar timestamp (UTC). Fixed, not wall-clock, so generation is
# deterministic without the caller having to supply a start every time.
_DEFAULT_START = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _to_money(value: float, *, places: Decimal) -> Decimal:
    """
    Quantize a float price to ``places`` and return it as ``Decimal``, going
    through ``str()`` first (normalizer convention) so a binary-float artifact
    never enters money math. ``max(value, 0)`` is a defensive floor; GBM stays
    positive but rounding a near-zero value must never produce a negative price.
    """
    return Decimal(str(max(value, 0.0))).quantize(places, rounding=ROUND_HALF_UP)


def generate_prices(
    n: int,
    *,
    seed: int,
    start_price: float = 100.0,
    mu: float = 0.08,
    sigma: float = 0.20,
    timeframe: str = "1Day",
) -> List[float]:
    """
    Return ``n`` GBM closing prices (floats) starting from ``start_price``.

    ``mu`` and ``sigma`` are *annualized* drift and volatility; they are scaled
    to the per-bar step using the trading-periods-per-year for ``timeframe``.
    The path is fully determined by ``seed``.

    Insufficient-data handling (golden rule): ``n <= 0`` returns ``[]`` rather
    than raising — an empty request yields an empty path.
    """
    if n <= 0:
        return []
    if start_price <= 0:
        raise ValueError(f"start_price must be positive, got {start_price}")
    if sigma < 0:
        raise ValueError(f"sigma must be non-negative, got {sigma}")

    periods = _PERIODS_PER_YEAR.get(timeframe)
    if periods is None:
        raise ValueError(
            f"unknown timeframe {timeframe!r}; known: {sorted(_PERIODS_PER_YEAR)}"
        )
    dt = 1.0 / periods

    rng = np.random.default_rng(seed)
    # Draw n shocks; price[i] is the close of bar i (the start_price is the
    # *previous* close, not itself emitted as a bar).
    shocks = rng.standard_normal(n)
    drift = (mu - 0.5 * sigma * sigma) * dt
    diffusion = sigma * math.sqrt(dt)
    log_returns = drift + diffusion * shocks

    prices: List[float] = []
    log_price = math.log(start_price)
    for r in log_returns:
        log_price += float(r)
        prices.append(math.exp(log_price))
    return prices


def generate_bars(
    symbol: Symbol,
    n: int,
    *,
    seed: int,
    start_price: float = 100.0,
    mu: float = 0.08,
    sigma: float = 0.20,
    timeframe: str = "1Day",
    start: Optional[datetime] = None,
    intrabar_range: float = 0.01,
    base_volume: float = 1_000_000.0,
    price_places: str = "0.01",
) -> List[Bar]:
    """
    Generate ``n`` validated, frozen ``Bar`` models along a seeded GBM path.

    Each bar's ``close`` is the GBM price; its ``open`` is the prior close (the
    first bar opens at ``start_price``). ``high``/``low`` bracket the open/close
    by a seeded fraction of price (up to ``intrabar_range`` each side) so the
    bar always satisfies ``low <= min(open, close) <= max(open, close) <= high``.
    ``volume`` jitters deterministically around ``base_volume``.

    Timestamps start at ``start`` (default: a fixed 2020-01-01 UTC — never
    wall-clock) and advance by one ``timeframe`` step per bar. A naive ``start``
    is assumed UTC, an aware one is converted to UTC.

    Returns ``[]`` for ``n <= 0``. All numeric guardrails (positive prices,
    high>=low, non-negative volume) are enforced by ``Bar`` itself; the geometry
    here is constructed so those never trip on valid inputs.
    """
    if n <= 0:
        return []
    if intrabar_range < 0:
        raise ValueError(f"intrabar_range must be non-negative, got {intrabar_range}")
    if base_volume < 0:
        raise ValueError(f"base_volume must be non-negative, got {base_volume}")

    step = _STEP.get(timeframe)
    if step is None:
        raise ValueError(f"unknown timeframe {timeframe!r}; known: {sorted(_STEP)}")

    if start is None:
        ts = _DEFAULT_START
    elif start.tzinfo is None:
        ts = start.replace(tzinfo=timezone.utc)
    else:
        ts = start.astimezone(timezone.utc)

    places = Decimal(price_places)
    closes = generate_prices(
        n, seed=seed, start_price=start_price, mu=mu, sigma=sigma, timeframe=timeframe
    )

    # A second, independently-seeded stream drives the intrabar geometry and
    # volume jitter so changing one knob does not reshuffle the price path.
    aux = np.random.default_rng(seed + 1)
    # Fractions in [0, intrabar_range] for the high extension above the bar top
    # and the low extension below the bar bottom; volume multiplier in [0.5, 1.5].
    high_frac = aux.uniform(0.0, intrabar_range, size=n)
    low_frac = aux.uniform(0.0, intrabar_range, size=n)
    vol_mult = aux.uniform(0.5, 1.5, size=n)

    bars: List[Bar] = []
    prev_close = start_price
    for i in range(n):
        close = closes[i]
        open_ = prev_close
        top = max(open_, close)
        bottom = min(open_, close)
        high = top * (1.0 + float(high_frac[i]))
        low = bottom * (1.0 - float(low_frac[i]))
        volume = base_volume * float(vol_mult[i])

        bars.append(
            Bar(
                symbol=symbol,
                timestamp=ts,
                open=_to_money(open_, places=places),
                high=_to_money(high, places=places),
                low=_to_money(low, places=places),
                close=_to_money(close, places=places),
                volume=Decimal(str(int(round(volume)))),
                timeframe=timeframe,
            )
        )
        prev_close = close
        ts = ts + step

    return bars


def generate_market_events(
    symbol: Symbol,
    n: int,
    *,
    seed: int,
    **kwargs: object,
) -> List[MarketEvent]:
    """
    Generate ``n`` ``MarketEvent``s wrapping a seeded GBM bar series — a
    drop-in substitute for ``HistoricalDataFeed`` output in tests/backtests.

    All keyword arguments accepted by :func:`generate_bars` (``start_price``,
    ``mu``, ``sigma``, ``timeframe``, ``start``, ...) pass straight through.
    """
    bars = generate_bars(symbol, n, seed=seed, **kwargs)  # type: ignore[arg-type]
    return [MarketEvent(bar=bar) for bar in bars]


def generate_multi_symbol_bars(
    symbols: Sequence[Symbol],
    n: int,
    *,
    seed: int,
    **kwargs: object,
) -> List[Bar]:
    """
    Generate ``n`` bars for each symbol on its own seeded path, merged into one
    chronologically-sorted list (stable on ``(timestamp, ticker)``), mirroring
    how a multi-symbol ``HistoricalDataFeed`` interleaves its stream.

    Each symbol gets a distinct, deterministic seed derived from its index so
    the paths differ but the whole result is reproducible from ``seed`` alone.
    """
    merged: List[Bar] = []
    for offset, symbol in enumerate(symbols):
        merged.extend(
            generate_bars(symbol, n, seed=seed + offset * 1000, **kwargs)  # type: ignore[arg-type]
        )
    merged.sort(key=lambda b: (b.timestamp, b.symbol.ticker))
    return merged
