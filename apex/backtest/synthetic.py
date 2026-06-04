"""
apex.backtest.synthetic
=======================
Deterministic synthetic OHLCV generation for pipeline tests and demos.

This is NOT market data and makes NO claim of realism beyond "has trends,
regimes, and noise." It exists so the end-to-end engine + Gauntlet can be
exercised without a data vendor. Every series is produced from a SEEDED RNG,
so runs are fully reproducible (the determinism rule applies to test data too).

For real backtests, feed the engine a HistoricalDataFeed pointed at actual
OHLCV files — these helpers are only for synthetic, reproducible scenarios.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Sequence, Tuple

from apex.core.events import MarketEvent
from apex.core.models import AssetClass, Bar, Symbol


def generate_closes(
    seed: int,
    n: int,
    start_price: float,
    drift_schedule: Sequence[Tuple[int, float]],
    vol: float,
) -> List[float]:
    """
    Build a multiplicative random-walk close series with regime-dependent drift.

    Args:
        seed: RNG seed (determinism).
        n: number of bars.
        start_price: first close.
        drift_schedule: list of (start_index, daily_log_drift) regimes, applied
                        from each start_index onward (later entries override).
        vol: daily log-return standard deviation.
    """
    rng = random.Random(seed)
    regimes = sorted(drift_schedule)
    prices = [start_price]
    for i in range(1, n):
        drift = 0.0
        for start_idx, d in regimes:
            if i >= start_idx:
                drift = d
        shock = rng.gauss(0.0, vol)
        prices.append(prices[-1] * math.exp(drift + shock))
    return prices


def make_bars(
    ticker: str,
    closes: Sequence[float],
    start_date: datetime | None = None,
    asset_class: AssetClass = AssetClass.ETF,
    timeframe: str = "1Day",
) -> List[Bar]:
    """Turn a close series into daily Bars (open=prev close, small HL band)."""
    start_date = start_date or datetime(2014, 1, 1, tzinfo=timezone.utc)
    sym = Symbol(ticker, asset_class)
    bars: List[Bar] = []
    prev_close = closes[0]
    for i, c in enumerate(closes):
        close = Decimal(str(round(c, 4)))
        open_ = Decimal(str(round(prev_close, 4)))
        hi = max(open_, close) * Decimal("1.005")
        lo = min(open_, close) * Decimal("0.995")
        ts = start_date + timedelta(days=i)
        bars.append(Bar(symbol=sym, timestamp=ts, open=open_, high=hi, low=lo,
                        close=close, volume=Decimal("1000000"), timeframe=timeframe))
        prev_close = c
    return bars


def interleave(*bar_lists: Sequence[Bar]) -> List[MarketEvent]:
    """Merge multiple per-symbol bar lists into one chronological event stream."""
    all_bars = [b for lst in bar_lists for b in lst]
    all_bars.sort(key=lambda b: (b.timestamp, b.symbol.ticker))
    return [MarketEvent(bar=b) for b in all_bars]
