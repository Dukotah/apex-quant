"""
scripts/health.py
=================
Read-only system status — one screen telling you, at a glance, whether the bot is
safe and running: the current mode/broker, whether the manual kill switch is armed,
how fresh the last cron cycle is, how many positions are open, and whether the
config's key risk caps and symbol whitelist look sane.

Run:  python -m scripts.health            # default state DB
      python -m scripts.health --help     # options

It NEVER places orders and NEVER requires the network — if the broker is
unreachable (or no keys are set) it simply reports that and degrades gracefully.
The exit code is the machine-readable verdict: non-zero ONLY on a genuine problem
(the kill switch is armed, or the persisted state is stale), 0 otherwise. Importable
without side effects.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from apex.core.config import AppConfig
from apex.risk.risk_manager import RiskConfig
from scripts.run_once import DEFAULT_STATE_PATH, StateStore, _kill_switch_active

logger = logging.getLogger("apex.health")

UTC = timezone.utc

# A cron that runs once per trading day; anything older than this is "stale" and a
# genuine problem (the scheduler stopped, or the runner is failing silently).
DEFAULT_STALE_AFTER_HOURS = 48


# ----------------------------------------------------------------- status model

@dataclass(frozen=True)
class HealthStatus:
    """The observable state of the system at one instant — a pure value object."""
    mode: str
    broker: str
    halted: bool                       # manual kill switch (APEX_HALT) armed
    last_run_ts: Optional[str]         # ISO timestamp of the last cron cycle, or None
    last_run_mode: Optional[str]
    age_hours: Optional[float]         # how long ago the last run was, in hours
    stale: bool                        # last run older than the stale threshold
    stale_after_hours: float
    open_positions: int
    whitelist_size: Optional[int]      # None = no whitelist (all symbols allowed)
    max_position_size_pct: str
    max_drawdown_pct: str
    max_daily_loss_pct: str
    max_leverage: str

    @property
    def ok(self) -> bool:
        """A genuine problem is the kill switch armed OR persisted state gone stale."""
        return not self.halted and not self.stale


def _parse_ts(ts: str) -> Optional[datetime]:
    """Parse an ISO timestamp from the state DB; tolerate naive ones as UTC."""
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _count_positions(positions_json: str) -> int:
    """Count open positions from the persisted JSON snapshot, tolerating garbage."""
    try:
        data = json.loads(positions_json)
    except (ValueError, TypeError):
        return 0
    return len(data) if isinstance(data, dict) else 0


def gather_status(
    config: AppConfig,
    store: StateStore,
    *,
    now: datetime,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
) -> HealthStatus:
    """
    Assemble a HealthStatus from the immutable config and the persisted state DB.

    Pure inspection: reads the last recorded cron cycle, never runs one. ``now`` is
    injected (no datetime.now() in logic) so the staleness check is deterministic.
    All collaborators are passed in, so this runs fully offline in tests.
    """
    risk: RiskConfig = config.risk

    last = store.last_run()
    last_ts: Optional[str] = None
    last_mode: Optional[str] = None
    age_hours: Optional[float] = None
    open_positions = 0
    stale = False

    if last is not None:
        last_ts = last["ts"]
        last_mode = last["mode"]
        open_positions = _count_positions(last["positions"])
        parsed = _parse_ts(last_ts)
        if parsed is not None:
            age_hours = (now - parsed).total_seconds() / 3600.0
            stale = age_hours > stale_after_hours
    else:
        # No run ever recorded — there is no fresh state to trust, so treat it as
        # stale (a genuine "the bot has never completed a cycle" problem).
        stale = True

    whitelist = risk.symbol_whitelist
    whitelist_size = len(whitelist) if whitelist is not None else None

    return HealthStatus(
        mode=config.mode.value,
        broker=config.broker.value,
        halted=_kill_switch_active(),
        last_run_ts=last_ts,
        last_run_mode=last_mode,
        age_hours=age_hours,
        stale=stale,
        stale_after_hours=stale_after_hours,
        open_positions=open_positions,
        whitelist_size=whitelist_size,
        max_position_size_pct=str(risk.max_position_size_pct),
        max_drawdown_pct=str(risk.max_drawdown_pct),
        max_daily_loss_pct=str(risk.max_daily_loss_pct),
        max_leverage=str(risk.max_leverage),
    )


def _pct(raw: str) -> str:
    """Render a fractional Decimal string (e.g. '0.16') as a percent ('16.0%')."""
    try:
        return f"{float(raw) * 100:.1f}%"
    except (ValueError, TypeError):
        return raw


def _age_str(age_hours: Optional[float]) -> str:
    if age_hours is None:
        return "never"
    if age_hours < 1.0:
        return f"{age_hours * 60:.0f}m ago"
    if age_hours < 48.0:
        return f"{age_hours:.1f}h ago"
    return f"{age_hours / 24.0:.1f}d ago"


def render(status: HealthStatus) -> str:
    """Format a HealthStatus as a compact one-screen report (pure → easy to test)."""
    halt_line = "ARMED (APEX_HALT) — orders blocked" if status.halted else "off"
    if status.last_run_ts is None:
        run_line = "no cron cycle ever recorded"
    else:
        flag = "  ** STALE **" if status.stale else ""
        run_line = (f"{status.last_run_ts[:16]}  ({_age_str(status.age_hours)}, "
                    f"mode {status.last_run_mode}){flag}")
    wl = "all (no whitelist)" if status.whitelist_size is None else f"{status.whitelist_size} symbol(s)"

    verdict = "OK — healthy" if status.ok else (
        "PROBLEM — KILL SWITCH ARMED" if status.halted else "PROBLEM — STATE STALE")

    lines = [
        "APEX QUANT — SYSTEM HEALTH",
        "=" * 56,
        f"  mode/broker  {status.mode} / {status.broker}",
        f"  kill switch  {halt_line}",
        f"  last run     {run_line}",
        f"  positions    {status.open_positions} open",
        "-" * 56,
        f"  whitelist    {wl}",
        f"  risk caps    pos {_pct(status.max_position_size_pct)}   "
        f"maxDD {_pct(status.max_drawdown_pct)}   "
        f"dailyLoss {_pct(status.max_daily_loss_pct)}   "
        f"lev {status.max_leverage}x",
        "-" * 56,
        f"  verdict      {verdict}",
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.health",
        description="Read-only one-screen system status for Apex Quant. "
                    "Never trades, never requires the network.",
    )
    parser.add_argument(
        "--state-path", default=str(DEFAULT_STATE_PATH),
        help=f"Path to the state SQLite DB (default: {DEFAULT_STATE_PATH}).",
    )
    parser.add_argument(
        "--stale-after-hours", type=float, default=DEFAULT_STALE_AFTER_HOURS,
        help="Treat the last run as stale (a problem) if older than this "
             f"many hours (default: {DEFAULT_STALE_AFTER_HOURS}).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    args = _build_parser().parse_args(argv)

    # Config from env, degrading gracefully if it can't be built (e.g. an invalid
    # mode/broker combo) — a status tool must never crash, just report the problem.
    try:
        config = AppConfig.from_env()
    except Exception as exc:  # noqa: BLE001
        print("APEX QUANT — SYSTEM HEALTH")
        print("=" * 56)
        print(f"  config error: {exc}")
        print("-" * 56)
        print("  verdict      PROBLEM — CONFIG INVALID")
        return 1

    store = StateStore(args.state_path)
    try:
        status = gather_status(
            config, store,
            now=datetime.now(UTC),  # entry-point only; logic stays deterministic
            stale_after_hours=args.stale_after_hours,
        )
    finally:
        store.close()

    print(render(status))
    return 0 if status.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
