"""
apex.ops.alerts
===============
Alert-policy module for the Apex Quant live cron.

Design goals
------------
* Actionable-only alerts — only notify when something meaningful happened.
* Once-daily heartbeat — silence on a *new* calendar day becomes a signal
  that the cron is down, not that everything is fine.
* Fully decoupled — no imports from scripts/run_once.py; operates on
  primitive inputs so it is trivially unit-testable.
* Deterministic — no clock reads inside this module. The caller injects
  ``today`` and ``last_alert_date`` as plain ``datetime.date`` objects
  (see :func:`should_heartbeat` and :func:`decide_alerts`).
* Network-isolated — only :class:`NtfyNotifier` touches the network, and
  it is written so it NEVER raises (catch + log). Every other object is
  pure Python with no I/O.

Public API
----------
``Alert``                  — frozen dataclass (title, message, priority)
``Notifier``               — Protocol that anything sendable must satisfy
``NtfyNotifier``           — real ntfy.sh sender; reads topic from arg or env
``decide_alerts()``        — pure decision function → list[Alert]
``should_heartbeat()``     — pure helper: True when no prior alert today
``send_alerts()``          — convenience dispatcher
"""

from __future__ import annotations

import logging
import os
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    """An immutable notification to be sent to the operator.

    Attributes
    ----------
    title:    Short headline shown in the push notification banner.
    message:  Full detail, e.g. the cycle summary.
    priority: ntfy.sh priority string — one of ``"urgent"``, ``"high"``,
              ``"default"``, ``"low"``, ``"min"``.
    """

    title: str
    message: str
    priority: str


# ---------------------------------------------------------------------------
# Notifier Protocol (dependency-injection seam)
# ---------------------------------------------------------------------------


class Notifier(Protocol):
    """Anything that can dispatch an :class:`Alert`.

    Implementations MUST NOT raise — a notification failure must never
    break the calling cron cycle.  Log and swallow instead.
    """

    def send(self, title: str, message: str, priority: str) -> None:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# Real ntfy.sh implementation
# ---------------------------------------------------------------------------


class NtfyNotifier:
    """Push alerts to https://ntfy.sh/{topic} via a plain HTTP POST.

    Parameters
    ----------
    topic:
        The ntfy.sh topic string.  When omitted (or ``None``) the
        ``NTFY_TOPIC`` environment variable is used.  If neither is set,
        :meth:`send` silently no-ops (the env var opt-in is preserved).

    Satisfies the :class:`Notifier` Protocol.
    """

    def __init__(self, topic: str | None = None) -> None:
        self._topic = topic or os.getenv("NTFY_TOPIC") or ""

    def send(self, title: str, message: str, priority: str = "default") -> None:
        """POST to ntfy.sh.  Never raises — all errors are logged and swallowed."""
        if not self._topic:
            return
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{self._topic}",
                data=message.encode("utf-8"),
                headers={
                    "Title": title,
                    "Priority": priority,
                    "Tags": "robot",
                },
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:  # noqa: BLE001
            logger.warning("NtfyNotifier: send failed: %s", exc)


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


def should_heartbeat(last_alert_date: date | None, today: date) -> bool:
    """Return ``True`` when a heartbeat is warranted on *today*.

    A heartbeat is warranted when the caller has not yet sent any alert
    today — i.e. ``last_alert_date`` is either ``None`` (never sent) or a
    date strictly before ``today``.

    Both arguments are plain ``datetime.date`` values, injected by the
    caller so this function stays deterministic (no clock reads here).

    Parameters
    ----------
    last_alert_date:
        The calendar date of the most recent alert that was actually sent,
        or ``None`` if no alert has ever been sent.
    today:
        The current calendar date, provided by the caller.
    """
    if last_alert_date is None:
        return True
    return last_alert_date < today


# ---------------------------------------------------------------------------
# Pure decision function
# ---------------------------------------------------------------------------

_HEARTBEAT_TITLE = "Apex Quant — daily heartbeat"
_HEARTBEAT_PRIORITY = "min"


def decide_alerts(
    *,
    killed: bool,
    quarantined: bool,
    halted: bool,
    orders_submitted: int,
    summary: str,
    is_new_day: bool,
) -> list[Alert]:
    """Return the list of :class:`Alert` objects to send for this cycle.

    Decision rules (in priority order):

    1. ``killed``           → one ``"urgent"`` alert, title "KILL SWITCH".
    2. ``quarantined``      → one ``"urgent"`` alert (mutually exclusive with 1).
    3. ``halted``           → one ``"high"`` alert (mutually exclusive with 1-2).
    4. ``orders_submitted > 0`` → one ``"default"`` alert (mutually exclusive with 1-3).
    5. If nothing actionable fired **and** ``is_new_day`` is ``True``
       → one ``"min"`` heartbeat alert (proves the cron is alive).
    6. Otherwise → empty list (quiet day, not a new calendar day).

    The function is **pure and deterministic**: same inputs → same output.
    No network, no clock, no randomness.

    Parameters
    ----------
    killed:
        Kill-switch was active this cycle.
    quarantined:
        Drift monitor quarantined the strategy this cycle.
    halted:
        Max-drawdown or daily circuit breaker tripped this cycle.
    orders_submitted:
        Number of orders submitted to the broker this cycle (0 = quiet).
    summary:
        Human-readable cycle summary string (used as the alert body).
    is_new_day:
        ``True`` when this cycle is the first of a new calendar day.
        The caller is responsible for computing this (see
        :func:`should_heartbeat`).  Keeping the clock read outside this
        module is what makes the function fully deterministic.
    """
    actionable: list[Alert] = []

    if killed:
        actionable.append(
            Alert(
                title="Apex Quant — KILL SWITCH",
                message=summary,
                priority="urgent",
            )
        )
    elif quarantined:
        actionable.append(
            Alert(
                title="Apex Quant — QUARANTINED",
                message=summary,
                priority="urgent",
            )
        )
    elif halted:
        actionable.append(
            Alert(
                title="Apex Quant — HALTED",
                message=summary,
                priority="high",
            )
        )
    elif orders_submitted > 0:
        actionable.append(
            Alert(
                title="Apex Quant — traded",
                message=summary,
                priority="default",
            )
        )

    if actionable:
        # An actionable alert already proves the cron is alive — skip heartbeat.
        return actionable

    if is_new_day:
        return [
            Alert(
                title=_HEARTBEAT_TITLE,
                message=summary,
                priority=_HEARTBEAT_PRIORITY,
            )
        ]

    # Quiet day, same calendar day — send nothing.
    return []


# ---------------------------------------------------------------------------
# Convenience dispatcher
# ---------------------------------------------------------------------------


def send_alerts(notifier: Notifier, alerts: list[Alert]) -> None:
    """Dispatch every alert in *alerts* through *notifier*.

    This is a thin loop so callers don't have to write it themselves.
    Errors are handled inside the notifier; this function never raises.

    Parameters
    ----------
    notifier:
        Any object satisfying :class:`Notifier`.
    alerts:
        The list returned by :func:`decide_alerts`.
    """
    for alert in alerts:
        notifier.send(alert.title, alert.message, alert.priority)
