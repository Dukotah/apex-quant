"""
apex.execution.stateful_sim
===========================
StatefulSimExecutionEngine: a SimulatedExecutionEngine whose "broker truth" is a
caller-supplied snapshot of positions — typically the last persisted state of a
paper EXPERIMENT book.

WHY THIS EXISTS
---------------
The live cron (``scripts/run_once``) trusts the BROKER to remember positions
between cycles: each run reconciles Alpaca's real holdings into a fresh local
portfolio. A *simulated* book has no broker to ask — the plain
``SimulatedExecutionEngine.reconcile_positions()`` returns ``{}``, so a sim book
would forget everything it holds the moment the cron process exits.

For the multi-book experiment harness (``scripts/run_experiments``) we want N
independent paper books, each remembering its own positions across cron cycles
via its own SQLite state DB. This engine closes that gap WITHOUT touching the
live trading path: it reports a seed snapshot as the venue's truth, so
``run_once`` reconciles the *last persisted book state* into the portfolio
exactly as it would reconcile a real broker. The discrepancy detector also stays
quiet, because the seed equals what the previous cycle persisted.

Fills are still fully simulated by the parent class (deterministic slippage +
commission). The ONLY behavioral change is the source of reconciliation truth.
"""

from __future__ import annotations

from typing import Dict, Optional

from apex.execution.simulated import SimulatedExecutionEngine


class StatefulSimExecutionEngine(SimulatedExecutionEngine):
    """
    A simulated engine that reports a seeded position snapshot as broker truth.

    The seed is the prior cycle's persisted positions in the run_once snapshot
    shape: ``{ticker: {"qty": ..., "avg_entry_price": ..., "current_price": ...}}``.
    run_once's ``_reconcile`` reads ``qty`` + ``avg_entry_price`` from each entry
    to rebuild the portfolio; its reconcile-discrepancy guard compares this same
    snapshot against the last persisted run, so a faithful seed never false-fires.

    Parameters mirror ``SimulatedExecutionEngine`` plus ``seed_positions``.
    """

    def __init__(
        self,
        seed_positions: Optional[Dict[str, dict]] = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        # Copy so external mutation of the caller's dict can't change our truth.
        self._seed_positions: Dict[str, dict] = {
            ticker: dict(pos) for ticker, pos in (seed_positions or {}).items()
        }

    def reconcile_positions(self) -> Dict[str, object]:
        """Return the seeded book state as the venue's reconciliation truth."""
        return {ticker: dict(pos) for ticker, pos in self._seed_positions.items()}
