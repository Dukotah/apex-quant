"""
apex.ops.heartbeat
==================
External dead-man's-switch pings for the Apex Quant live cron.

Why this exists
---------------
The in-repo watchdog (``.github/workflows/watchdog.yml``) is a SAME-PLATFORM
check: it alerts when ``state/status.json`` goes stale, but it runs on the very
GitHub Actions scheduler it is meant to police. If GitHub disables scheduled
workflows (e.g. the 60-day repo-inactivity auto-disable) or the schedule simply
never fires, the watchdog is silenced too — the exact blind spot behind the
June 2026 three-day outage, where a pre-``run_once`` preflight failure and a
schedule that didn't fire alerted nobody.

A truly independent dead-man's-switch lives OUTSIDE GitHub: the trade cron pings
an external monitor (e.g. healthchecks.io — free, no account needed for a single
check) on every SUCCESSFUL cycle, and that monitor emails you when the pings
stop. Because the ping is sent from inside a genuinely-completed cycle, a
preflight skip, an errored run, AND a schedule that never fires all collapse to
the SAME observable: **no ping → the monitor alerts.** That is the failure mode
the same-platform watchdog cannot see.

Design (mirrors :mod:`apex.ops.alerts`)
---------------------------------------
* **Env opt-in.** The ping URL comes from the constructor argument or the
  ``APEX_HEARTBEAT_URL`` environment variable. If neither is set, :meth:`ping`
  silently no-ops — the feature is off until you opt in, exactly like
  :class:`~apex.ops.alerts.NtfyNotifier`.
* **Never raises.** A monitoring failure must NEVER break a trading cycle. All
  network errors are caught, logged, and swallowed.
* **Network-isolated + injectable.** Only :class:`HealthchecksPinger` touches the
  network, via an injectable ``opener`` seam (default ``urllib.request.urlopen``)
  so the whole module is unit-testable offline.
* **Healthchecks.io ping convention.** ``GET <url>`` signals success, ``GET
  <url>/fail`` signals failure, and ``GET <url>/start`` (optional) marks a run
  start. Any check-in service using the same suffix convention works; a plain
  success-only URL also works (the suffixes are simply appended).

Public API
----------
``Pinger``               — Protocol that anything pingable must satisfy.
``HealthchecksPinger``   — real GET-based pinger; reads URL from arg or env.
``ping_heartbeat()``     — convenience dispatcher used by the cron wiring.
"""

from __future__ import annotations

import logging
import os
import urllib.request
from typing import Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Environment variable that opts the cron into external heartbeat pinging.
HEARTBEAT_URL_ENV = "APEX_HEARTBEAT_URL"

#: Default per-ping network timeout, in seconds. Kept short so a slow monitor
#: never delays a trading cycle.
DEFAULT_TIMEOUT = 10.0

# An opener is any callable matching ``urllib.request.urlopen(url, timeout=...)``.
Opener = Callable[..., object]


# ---------------------------------------------------------------------------
# Pinger Protocol (dependency-injection seam)
# ---------------------------------------------------------------------------


@runtime_checkable
class Pinger(Protocol):
    """Anything that can send a liveness ping to an external monitor.

    Implementations MUST NOT raise — a monitoring failure must never break the
    calling cron cycle. Log and swallow instead.
    """

    def ping(self, success: bool = True) -> None:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Real healthchecks.io-style implementation
# ---------------------------------------------------------------------------


class HealthchecksPinger:
    """Ping an external check-in monitor with a plain HTTP ``GET``.

    Parameters
    ----------
    url:
        The base check-in URL (e.g. ``https://hc-ping.com/<uuid>``). When omitted
        (or ``None``) the ``APEX_HEARTBEAT_URL`` environment variable is used. If
        neither is set, :meth:`ping` silently no-ops (the env opt-in is
        preserved). Any trailing slash is stripped so suffixes append cleanly.
    opener:
        Injectable callable matching ``urllib.request.urlopen(url, timeout=...)``.
        Defaults to ``urllib.request.urlopen``. The seam keeps the class fully
        unit-testable offline.
    timeout:
        Per-ping network timeout in seconds (default :data:`DEFAULT_TIMEOUT`).

    Satisfies the :class:`Pinger` Protocol.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        opener: Opener | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        raw = url if url is not None else os.getenv(HEARTBEAT_URL_ENV) or ""
        self._url = raw.strip().rstrip("/")
        self._opener: Opener = opener or urllib.request.urlopen
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        """True when a ping URL is configured (the feature is opted in)."""
        return bool(self._url)

    def ping(self, success: bool = True) -> None:
        """Signal liveness to the monitor. Never raises.

        ``success=True`` pings the base URL (a healthy completed cycle);
        ``success=False`` pings ``<url>/fail`` so the monitor can alert
        immediately on a started-then-errored run rather than waiting for the
        silence window to elapse. No-ops when no URL is configured.
        """
        if not self._url:
            return
        target = self._url if success else f"{self._url}/fail"
        try:
            self._opener(target, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 — monitoring must never break the cron
            logger.warning("HealthchecksPinger: ping failed: %s", exc)


# ---------------------------------------------------------------------------
# Convenience dispatcher
# ---------------------------------------------------------------------------


def ping_heartbeat(success: bool = True, *, pinger: Pinger | None = None) -> None:
    """Send one liveness ping through *pinger* (defaults to a new
    :class:`HealthchecksPinger` reading ``APEX_HEARTBEAT_URL``).

    A thin wrapper so callers need not construct the pinger themselves. Never
    raises: the pinger swallows network errors, and construction is trivial.
    """
    (pinger or HealthchecksPinger()).ping(success=success)
