"""
scripts.run_once
================
Entry point the GitHub Actions cron calls. Runs ONE trading cycle and exits.

⚠️ THIS IS A STUB. To be implemented in Phase 5 (see ROADMAP.md).

The contract this script must fulfill when built:
  1. Load AppConfig.from_env()  (reads APEX_MODE, broker, credentials)
  2. Restore portfolio state from state/portfolio.db (SQLite)
  3. Build the execution engine via the factory (paper vs live from config)
  4. Reconcile positions against the broker's truth on startup
  5. Fetch the latest bars for all symbols in the active strategies' universes
  6. For each new bar:
        strategy.on_bar(bar) -> signals
        for signal in signals:
            order = risk_manager.evaluate(signal, portfolio_snapshot)
            if order: execution_engine.submit_order(order)
  7. Process any fills -> portfolio.on_fill()
  8. Persist updated state back to state/portfolio.db
  9. Exit cleanly (the cron commits the state file)

Must be IDEMPOTENT: running twice in a row with no new bars does nothing harmful.
Must FAIL SAFE: any unhandled error exits non-zero (cron notifies) without
leaving partial/corrupt state.
"""
from __future__ import annotations

import sys


def main() -> int:
    print("run_once: STUB — implement in Phase 5. See ROADMAP.md and CLAUDE.md.")
    # Intentionally a no-op so the cron workflow is testable before Phase 5.
    return 0


if __name__ == "__main__":
    sys.exit(main())
