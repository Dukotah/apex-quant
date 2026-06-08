"""
scripts/run_experiments.py
==========================
The MULTI-BOOK paper-experiment harness. Runs a roster of strategies as N fully
isolated paper books — each with its OWN simulated execution, portfolio, risk
manager, and SQLite state DB — all off the same live Alpaca data each cron cycle.
This is how we forward-test several strategies side by side while the deployed
``multi_asset_trend`` sleeve runs for real on paper (see scripts/run_once).

KEY IDEA — reuse the live cycle verbatim
----------------------------------------
Each book is one ordinary ``run_once`` cycle with everything injected:

  * its own ``StatefulSimExecutionEngine`` seeded from the book's last persisted
    state — so the book REMEMBERS its positions across cron runs even though
    there is no real broker to reconcile against (a plain simulator forgets);
  * its own ``Portfolio`` + ``RiskManager`` (a shared EXPERIMENT_RISK envelope so
    differences reflect the STRATEGY, not the risk config);
  * its own state DB at ``state/experiments/<book_id>.db``, committed back by the
    cron just like the deployed book.

Because run_once is reused unchanged, every guardrail (stops, drawdown halts,
drift monitor, reconcile-discrepancy block) applies to each experiment exactly as
it would in production — so the leaderboard reflects how each strategy would
really behave if deployed, not an idealized backtest.

NOTHING here touches the live trading path: experiments are simulated books only.
The export side (scripts/export_status) reads these DBs into the dashboard's
multi-book ``books[]`` array.

Run (research; the cron invokes this after run_once):
    APEX_MODE=paper APEX_BROKER=alpaca python -m scripts.run_experiments
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from apex.core.clock import Clock
from apex.core.config import AppConfig, Broker, ExecutionMode
from apex.core.models import AssetClass, Symbol
from apex.data.base_feed import BaseDataFeed
from apex.execution.stateful_sim import StatefulSimExecutionEngine
from apex.risk.portfolio import Portfolio
from apex.risk.risk_manager import RiskConfig, RiskManager
from apex.strategy.base_strategy import BaseStrategy
from scripts.run_once import RunReport, StateStore, run_once

logger = logging.getLogger("apex.run_experiments")

DEFAULT_EXPERIMENTS_DIR = Path("state/experiments")

# A shared risk envelope for every experiment book. Deliberately ONE config across
# all books so the leaderboard isolates the strategy's edge, not a hand-tuned risk
# setting. A 25% per-position cap leaves single-name strategies (e.g. RSI(2) on SPY)
# room to express a real position while still bounding concentration; circuit
# breakers sit well above normal drawdowns so they act as catastrophe stops.
EXPERIMENT_RISK = RiskConfig(
    max_position_size_pct=Decimal("0.25"),
    max_total_exposure_pct=Decimal("1.0"),
    max_leverage=Decimal("1.0"),
    max_drawdown_pct=Decimal("0.40"),
    max_daily_loss_pct=Decimal("0.10"),
    require_stop_loss=True,
    drawdown_throttle_start=Decimal("0.12"),
    drawdown_throttle_full=Decimal("0.30"),
    drawdown_throttle_floor=Decimal("0.35"),
    target_volatility=Decimal("0.12"),
)

# Warm every strategy's longest indicator: 200-day SMAs + 252-bar (12-month)
# momentum need >252 bars; 400 daily bars covers them with comfortable slack.
EXPERIMENT_LOOKBACK = 400


@dataclass(frozen=True)
class ExperimentBook:
    """One paper experiment: an id, a display name, and a strategy factory."""

    id: str
    name: str
    # A factory (not an instance) so each cron cycle gets a fresh, stateless
    # strategy — position truth comes from the reconciled portfolio, never from
    # strategy-internal flags.
    make_strategies: Callable[[], List[BaseStrategy]]
    risk: RiskConfig = field(default=EXPERIMENT_RISK)
    capital: Decimal = field(default=Decimal("100000"))


def _syms(*tickers: str) -> List[Symbol]:
    """Build a list of ETF Symbols (the experiment universe is all liquid ETFs)."""
    return [Symbol(t, AssetClass.ETF) for t in tickers]


def default_experiments() -> List[ExperimentBook]:
    """
    The starter roster: the deployed trend strategy plus a spread of uncorrelated
    library strategies, each on a sensible liquid-ETF universe. Strategy ids match
    the book id so positions/signals attribute cleanly.
    """
    from apex.strategy.library.cross_sectional_momentum import CrossSectionalMomentumStrategy
    from apex.strategy.library.dual_momentum import DualMomentumStrategy
    from apex.strategy.library.etf_rotation import ETFRotationStrategy
    from apex.strategy.library.multi_asset_trend import MultiAssetTrendStrategy
    from apex.strategy.library.rsi2_mean_reversion import RSI2MeanReversionStrategy
    from apex.strategy.library.trend_bond import TrendBondStrategy

    return [
        ExperimentBook(
            "trend_multi_asset",
            "Multi-Asset Trend (5-sleeve)",
            lambda: [
                MultiAssetTrendStrategy(
                    "trend_multi_asset", _syms("SPY", "EFA", "TLT", "GLD", "DBC")
                )
            ],
        ),
        ExperimentBook(
            "dual_momentum",
            "Dual Momentum (GEM)",
            lambda: [DualMomentumStrategy("dual_momentum", _syms("SPY", "EFA", "AGG"))],
        ),
        ExperimentBook(
            "trend_bond",
            "Trend + Bond Rotation",
            lambda: [TrendBondStrategy("trend_bond", _syms("SPY", "AGG"))],
        ),
        ExperimentBook(
            "etf_rotation",
            "Sector ETF Rotation",
            lambda: [
                ETFRotationStrategy(
                    "etf_rotation",
                    _syms("XLK", "XLF", "XLE", "XLV", "XLY", "XLI", "XLP", "XLU", "XLB", "AGG"),
                )
            ],
        ),
        ExperimentBook(
            "rsi2_spy",
            "RSI(2) Mean Reversion (SPY)",
            lambda: [RSI2MeanReversionStrategy("rsi2_spy", _syms("SPY"))],
        ),
        ExperimentBook(
            "cross_sec_momo",
            "Cross-Sectional Momentum",
            lambda: [
                CrossSectionalMomentumStrategy(
                    "cross_sec_momo", _syms("SPY", "EFA", "TLT", "GLD", "DBC")
                )
            ],
        ),
    ]


def _load_seed(store: StateStore) -> Dict[str, dict]:
    """The last persisted positions for this book — the sim engine's seed truth."""
    last = store.last_run()
    if last is None:
        return {}
    try:
        return json.loads(last["positions"]) or {}
    except (TypeError, ValueError, KeyError):
        return {}


def _paper_config(book: ExperimentBook) -> AppConfig:
    """A paper/simulated config for a book. Broker is irrelevant — we inject a sim engine."""
    return AppConfig(
        mode=ExecutionMode.PAPER,
        broker=Broker.SIMULATED,
        initial_capital=book.capital,
        risk=book.risk,
    )


def run_experiment_book(
    book: ExperimentBook,
    *,
    state_dir: str | Path = DEFAULT_EXPERIMENTS_DIR,
    config: Optional[AppConfig] = None,
    feed: Optional[BaseDataFeed] = None,
    clock: Optional[Clock] = None,
    lookback: int = EXPERIMENT_LOOKBACK,
) -> RunReport:
    """
    Run ONE evaluation cycle for a single experiment book and persist its state.

    The book's last persisted positions seed a StatefulSimExecutionEngine, so the
    book carries its holdings across cron cycles. With ``feed`` unset, run_once
    builds a live AlpacaDataFeed for the book's symbols; tests inject a fake feed.
    """
    config = config or _paper_config(book)
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    store = StateStore(state_dir / f"{book.id}.db")
    try:
        engine = StatefulSimExecutionEngine(seed_positions=_load_seed(store))
        portfolio = Portfolio(book.capital)
        risk_manager = RiskManager(book.risk)
        return run_once(
            config,
            book.make_strategies(),
            clock=clock,
            feed=feed,
            execution_engine=engine,
            portfolio=portfolio,
            risk_manager=risk_manager,
            state_store=store,
            lookback=lookback,
        )
    finally:
        store.close()


def run_all(
    books: Sequence[ExperimentBook],
    *,
    state_dir: str | Path = DEFAULT_EXPERIMENTS_DIR,
    config: Optional[AppConfig] = None,
    feed: Optional[BaseDataFeed] = None,
    clock: Optional[Clock] = None,
) -> Dict[str, Optional[RunReport]]:
    """
    Run every book, isolating failures: one book that errors logs and yields None
    rather than aborting the whole roster (a bad strategy can't sink the cron).
    With ``feed`` unset each book fetches its own live data for its own universe.
    """
    reports: Dict[str, Optional[RunReport]] = {}
    for book in books:
        try:
            reports[book.id] = run_experiment_book(
                book, state_dir=state_dir, config=config, feed=feed, clock=clock
            )
        except Exception as exc:  # noqa: BLE001 — one book failing must not kill the rest
            logger.error("experiment book %s failed: %s", book.id, exc)
            reports[book.id] = None
    return reports


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - live wiring
    """Run the full roster against live Alpaca data and print a one-line summary each."""
    logging.basicConfig(level=logging.INFO)
    config = AppConfig(
        mode=ExecutionMode.PAPER,
        broker=Broker.ALPACA,
        risk=EXPERIMENT_RISK,
        alpaca_key=os.getenv("ALPACA_API_KEY"),
        alpaca_secret=os.getenv("ALPACA_SECRET_KEY"),
    )
    books = default_experiments()
    reports = run_all(books, config=config)
    for book in books:
        report = reports.get(book.id)
        print(f"[{book.id}] {report.summary() if report else 'FAILED — see logs'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main(argv=sys.argv[1:]))
