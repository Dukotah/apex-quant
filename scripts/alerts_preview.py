"""
scripts/alerts_preview.py
=========================
A dry-run that RENDERS the ntfy push alerts the current state would produce —
without sending anything over the network. It is the safe way to answer "what
would the bot have pinged my phone?" before (or instead of) firing a real cron.

scripts/run_once.py decides what to push in ``_notify_cycle``: the same priority
ladder of KILL SWITCH > QUARANTINED > HALTED > traded, and a quiet no-op on
ordinary cycles. This script mirrors that ladder EXACTLY in a pure, deterministic
core (``preview_alert``) so a change to the alert policy is caught by a test, not
by a missed page at 3am.

Run:  python -m scripts.alerts_preview               # last recorded run, paper
      python -m scripts.alerts_preview --mode live   # last live run
      python -m scripts.alerts_preview --state state/apex_state.db

Read-only: it reads the run_once state DB and prints the alert that WOULD be sent.
It never opens a socket, never touches the broker, and never calls datetime.now()
in its logic — the timestamp it renders comes from the recorded run.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Optional, Sequence

# Notes on isolation/determinism:
#  * Module import has ZERO side effects — no logging config, no I/O, no env reads.
#  * Every dependency on run_once (RunReport, StateStore) is lazy-imported INSIDE
#    a function, so importing this module never drags in the live wiring.
#  * The decision core (preview_alert) is pure: it takes a RunReport-shaped value
#    and returns an AlertPreview. No clock, no network, no globals.


@dataclass(frozen=True)
class AlertPreview:
    """
    What ntfy WOULD receive for one cycle — or that nothing would be sent.

    ``would_send`` is False on a quiet cycle (no kill/quarantine/halt and no
    orders), in which case title/message/priority are empty strings. This mirrors
    run_once._notify_cycle, which simply returns without calling _notify.
    """

    would_send: bool
    title: str = ""
    message: str = ""
    priority: str = ""
    reason: str = ""        # why this alert (or silence) was chosen

    def render(self) -> str:
        """Human-readable one-screen preview of the alert (or the silence)."""
        if not self.would_send:
            return (
                "NO ALERT — this cycle is quiet.\n"
                f"  reason   {self.reason}"
            )
        return "\n".join(
            [
                "ALERT PREVIEW (not sent)",
                f"  title    {self.title}",
                f"  priority {self.priority}",
                f"  reason   {self.reason}",
                "  message  |",
                *(f"    {line}" for line in self.message.splitlines() or [""]),
            ]
        )


# --------------------------------------------------------------------- pure core

def preview_alert(report) -> AlertPreview:
    """
    PURE, DETERMINISTIC core. Given a RunReport (or any value exposing the same
    fields: ``killed``, ``quarantined``, ``halted``, ``orders_submitted`` and a
    ``summary()`` method), return the AlertPreview that run_once._notify_cycle
    would produce — WITHOUT sending it.

    The priority ladder is identical to run_once._notify_cycle and must stay so:
        killed            -> "Apex Quant - KILL SWITCH"  urgent
        quarantined       -> "Apex Quant - QUARANTINED"  urgent
        halted            -> "Apex Quant - HALTED"       high
        orders_submitted  -> "Apex Quant - traded"       default
        otherwise         -> no alert (quiet cycle)

    No I/O, no clock, no network — only reads from the supplied report.
    """
    message = report.summary()
    if getattr(report, "killed", False):
        return AlertPreview(True, "Apex Quant - KILL SWITCH", message, "urgent",
                            "kill switch (APEX_HALT) active — all orders blocked")
    if getattr(report, "quarantined", False):
        return AlertPreview(True, "Apex Quant - QUARANTINED", message, "urgent",
                            "strategy quarantined — alpha decay below drift floor")
    if getattr(report, "halted", False):
        return AlertPreview(True, "Apex Quant - HALTED", message, "high",
                            "risk manager halted the system (drawdown/daily breaker)")
    if getattr(report, "orders_submitted", 0) > 0:
        return AlertPreview(True, "Apex Quant - traded", message, "default",
                            f"{report.orders_submitted} order(s) submitted this cycle")
    return AlertPreview(False, reason="quiet cycle — no kill/quarantine/halt and no orders")


# ------------------------------------------------------------------ state lookup

def _load_last_report(state_path: Optional[str], mode: str):
    """
    Lazy-build the latest RunReport for ``mode`` from the run_once state DB.

    Returns None if there is no recorded run for that mode. All run_once imports
    happen HERE, inside the function, so importing this module stays side-effect
    free and never requires the live wiring to be importable.
    """
    from datetime import datetime, timezone

    from scripts.run_once import RunReport, StateStore  # lazy: keep import clean

    store = StateStore(state_path) if state_path else StateStore()
    try:
        rows = store.history(mode)
    finally:
        store.close()
    if not rows:
        return None

    row = rows[-1]
    ts_raw = row["ts"]
    try:
        ts = datetime.fromisoformat(ts_raw)
    except (ValueError, TypeError):
        ts = datetime(1970, 1, 1, tzinfo=timezone.utc)

    # Reconstruct just the fields the alert ladder cares about. orders_submitted,
    # halted and equity are persisted; killed/quarantined are not stored per-row,
    # so they default to False — a recorded historical row reflects an already-
    # completed (non-killed) cycle. The "halted" flag is the persisted signal.
    return RunReport(
        timestamp=ts,
        mode=row["mode"],
        equity=float(row["equity"]),
        num_positions=int(row["num_positions"]),
        orders_submitted=int(row["orders"]),
        halted=bool(int(row["halted"])),
    )


# -------------------------------------------------------------------- cli wiring

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alerts_preview",
        description="Render the ntfy alert the current run_once state would push, "
                    "without sending it (read-only dry-run).",
    )
    parser.add_argument(
        "--mode", default="paper",
        help="execution mode to preview (paper/live/...). Default: paper.",
    )
    parser.add_argument(
        "--state", default=None,
        help="path to the run_once SQLite state DB (default: run_once's default).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - I/O shell
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    args = _build_parser().parse_args(argv)
    report = _load_last_report(args.state, args.mode)
    if report is None:
        print(f"No '{args.mode}' runs recorded yet — nothing to preview.")
        return 0
    print(f"Previewing alert for the last '{args.mode}' cycle "
          f"@ {report.timestamp:%Y-%m-%d %H:%M}Z (not sent):")
    print(preview_alert(report).render())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
