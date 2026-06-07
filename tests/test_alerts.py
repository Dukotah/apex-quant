"""
Tests for apex.ops.alerts.

All tests are offline, deterministic, and use a FakeNotifier — no network
calls, no clock reads, no filesystem I/O.

Coverage targets:
  - Each actionable condition maps to the correct priority.
  - Priority precedence: killed > quarantined > halted > traded.
  - Heartbeat fires only on a new day with nothing actionable.
  - Heartbeat does NOT fire when something actionable fired.
  - Heartbeat does NOT fire on the same calendar day (is_new_day=False).
  - Empty list when quiet and not a new day.
  - NtfyNotifier swallows network errors — never raises.
  - should_heartbeat helper logic.
  - send_alerts dispatches every alert.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest

from apex.ops.alerts import (
    Alert,
    NtfyNotifier,
    decide_alerts,
    send_alerts,
    should_heartbeat,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeNotifier:
    """Records every send() call for inspection in tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send(self, title: str, message: str, priority: str) -> None:
        self.calls.append((title, message, priority))

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def priorities(self) -> list[str]:
        return [c[2] for c in self.calls]

    def titles(self) -> list[str]:
        return [c[0] for c in self.calls]


def _decide(
    *,
    killed: bool = False,
    quarantined: bool = False,
    halted: bool = False,
    orders_submitted: int = 0,
    summary: str = "cycle ok",
    is_new_day: bool = False,
) -> list[Alert]:
    """Thin wrapper so tests only specify the fields they care about."""
    return decide_alerts(
        killed=killed,
        quarantined=quarantined,
        halted=halted,
        orders_submitted=orders_submitted,
        summary=summary,
        is_new_day=is_new_day,
    )


# ---------------------------------------------------------------------------
# Alert dataclass
# ---------------------------------------------------------------------------


def test_alert_is_frozen() -> None:
    a = Alert(title="t", message="m", priority="default")
    with pytest.raises(Exception):
        a.title = "changed"  # type: ignore[misc]


def test_alert_fields() -> None:
    a = Alert(title="my-title", message="my-msg", priority="urgent")
    assert a.title == "my-title"
    assert a.message == "my-msg"
    assert a.priority == "urgent"


# ---------------------------------------------------------------------------
# decide_alerts — actionable conditions map to correct priorities
# ---------------------------------------------------------------------------


def test_killed_maps_to_urgent() -> None:
    alerts = _decide(killed=True, summary="kill switch!")
    assert len(alerts) == 1
    assert alerts[0].priority == "urgent"
    assert "KILL SWITCH" in alerts[0].title
    assert alerts[0].message == "kill switch!"


def test_quarantined_maps_to_urgent() -> None:
    alerts = _decide(quarantined=True, summary="drift detected")
    assert len(alerts) == 1
    assert alerts[0].priority == "urgent"
    assert "QUARANTINED" in alerts[0].title


def test_halted_maps_to_high() -> None:
    alerts = _decide(halted=True, summary="drawdown breach")
    assert len(alerts) == 1
    assert alerts[0].priority == "high"
    assert "HALTED" in alerts[0].title


def test_traded_maps_to_default() -> None:
    alerts = _decide(orders_submitted=3, summary="3 orders")
    assert len(alerts) == 1
    assert alerts[0].priority == "default"
    assert "traded" in alerts[0].title.lower()


# ---------------------------------------------------------------------------
# decide_alerts — priority precedence
# ---------------------------------------------------------------------------


def test_killed_beats_quarantined() -> None:
    alerts = _decide(killed=True, quarantined=True)
    assert len(alerts) == 1
    assert alerts[0].priority == "urgent"
    assert "KILL SWITCH" in alerts[0].title


def test_killed_beats_halted() -> None:
    alerts = _decide(killed=True, halted=True)
    assert len(alerts) == 1
    assert "KILL SWITCH" in alerts[0].title


def test_killed_beats_traded() -> None:
    alerts = _decide(killed=True, orders_submitted=5)
    assert len(alerts) == 1
    assert "KILL SWITCH" in alerts[0].title


def test_quarantined_beats_halted() -> None:
    alerts = _decide(quarantined=True, halted=True)
    assert len(alerts) == 1
    assert alerts[0].priority == "urgent"
    assert "QUARANTINED" in alerts[0].title


def test_quarantined_beats_traded() -> None:
    alerts = _decide(quarantined=True, orders_submitted=2)
    assert len(alerts) == 1
    assert "QUARANTINED" in alerts[0].title


def test_halted_beats_traded() -> None:
    alerts = _decide(halted=True, orders_submitted=1)
    assert len(alerts) == 1
    assert alerts[0].priority == "high"
    assert "HALTED" in alerts[0].title


# ---------------------------------------------------------------------------
# decide_alerts — heartbeat logic
# ---------------------------------------------------------------------------


def test_heartbeat_fires_on_new_day_when_nothing_actionable() -> None:
    alerts = _decide(is_new_day=True)
    assert len(alerts) == 1
    assert alerts[0].priority == "min"
    assert "heartbeat" in alerts[0].title.lower()


def test_heartbeat_carries_summary() -> None:
    alerts = _decide(is_new_day=True, summary="all quiet")
    assert alerts[0].message == "all quiet"


def test_heartbeat_does_not_fire_when_killed() -> None:
    alerts = _decide(killed=True, is_new_day=True)
    assert len(alerts) == 1
    assert "KILL SWITCH" in alerts[0].title


def test_heartbeat_does_not_fire_when_quarantined() -> None:
    alerts = _decide(quarantined=True, is_new_day=True)
    assert len(alerts) == 1
    assert "QUARANTINED" in alerts[0].title


def test_heartbeat_does_not_fire_when_halted() -> None:
    alerts = _decide(halted=True, is_new_day=True)
    assert len(alerts) == 1
    assert "HALTED" in alerts[0].title


def test_heartbeat_does_not_fire_when_traded() -> None:
    alerts = _decide(orders_submitted=1, is_new_day=True)
    assert len(alerts) == 1
    assert "traded" in alerts[0].title.lower()


def test_heartbeat_does_not_fire_same_day() -> None:
    """is_new_day=False means we already sent a heartbeat today — skip it."""
    alerts = _decide(is_new_day=False)
    assert alerts == []


def test_empty_on_quiet_not_new_day() -> None:
    alerts = _decide()  # all defaults: quiet, not new day
    assert alerts == []


# ---------------------------------------------------------------------------
# should_heartbeat
# ---------------------------------------------------------------------------


def test_should_heartbeat_when_never_sent() -> None:
    assert should_heartbeat(last_alert_date=None, today=date(2026, 6, 7)) is True


def test_should_heartbeat_when_last_was_yesterday() -> None:
    assert should_heartbeat(last_alert_date=date(2026, 6, 6), today=date(2026, 6, 7)) is True


def test_no_heartbeat_when_already_sent_today() -> None:
    assert should_heartbeat(last_alert_date=date(2026, 6, 7), today=date(2026, 6, 7)) is False


def test_no_heartbeat_when_last_in_future() -> None:
    # Edge case: last_alert_date is somehow in the future — still False.
    assert should_heartbeat(last_alert_date=date(2026, 6, 8), today=date(2026, 6, 7)) is False


# ---------------------------------------------------------------------------
# send_alerts
# ---------------------------------------------------------------------------


def test_send_alerts_dispatches_all() -> None:
    notifier = FakeNotifier()
    alerts = [
        Alert("t1", "m1", "urgent"),
        Alert("t2", "m2", "default"),
    ]
    send_alerts(notifier, alerts)
    assert notifier.call_count == 2
    assert notifier.titles() == ["t1", "t2"]
    assert notifier.priorities() == ["urgent", "default"]


def test_send_alerts_empty_list_does_nothing() -> None:
    notifier = FakeNotifier()
    send_alerts(notifier, [])
    assert notifier.call_count == 0


def test_send_alerts_preserves_order() -> None:
    notifier = FakeNotifier()
    alerts = [Alert(title=f"t{i}", message="m", priority="default") for i in range(5)]
    send_alerts(notifier, alerts)
    assert notifier.titles() == [f"t{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# NtfyNotifier — swallows network errors, never raises
# ---------------------------------------------------------------------------


def test_ntfy_notifier_swallows_urlerror(monkeypatch: Any) -> None:
    """NtfyNotifier must not raise when urllib raises URLError."""

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.URLError("simulated network failure")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)

    notifier = NtfyNotifier(topic="test-topic")
    # Should complete without raising:
    notifier.send("title", "message", "urgent")


def test_ntfy_notifier_swallows_timeout(monkeypatch: Any) -> None:
    """NtfyNotifier must not raise when the request times out."""

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)

    notifier = NtfyNotifier(topic="test-topic")
    notifier.send("title", "message", "default")


def test_ntfy_notifier_swallows_generic_exception(monkeypatch: Any) -> None:
    """NtfyNotifier must swallow arbitrary unexpected exceptions."""

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)

    notifier = NtfyNotifier(topic="test-topic")
    notifier.send("title", "message", "high")


def test_ntfy_notifier_no_topic_is_noop(monkeypatch: Any) -> None:
    """With no topic configured, send() must not attempt any network call."""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    called = []

    def _mock_urlopen(*args: Any, **kwargs: Any) -> None:
        called.append(True)

    monkeypatch.setattr(urllib.request, "urlopen", _mock_urlopen)

    notifier = NtfyNotifier(topic="")  # no topic
    notifier.send("title", "message", "default")
    assert called == []


def test_ntfy_notifier_reads_env_topic(monkeypatch: Any) -> None:
    """NtfyNotifier falls back to NTFY_TOPIC env var when no topic is passed."""
    monkeypatch.setenv("NTFY_TOPIC", "my-env-topic")
    captured: list[Any] = []

    def _mock_urlopen(req: Any, **kwargs: Any) -> None:
        captured.append(req)
        return MagicMock()  # simulate a successful response

    monkeypatch.setattr(urllib.request, "urlopen", _mock_urlopen)

    notifier = NtfyNotifier()  # no explicit topic
    notifier.send("t", "m", "default")
    assert len(captured) == 1
    url = captured[0].full_url if hasattr(captured[0], "full_url") else str(captured[0])
    assert "my-env-topic" in url


def test_ntfy_notifier_explicit_topic_overrides_env(monkeypatch: Any) -> None:
    """An explicit topic arg must take precedence over the env var."""
    monkeypatch.setenv("NTFY_TOPIC", "env-topic")
    captured: list[Any] = []

    def _mock_urlopen(req: Any, **kwargs: Any) -> None:
        captured.append(req)
        return MagicMock()

    monkeypatch.setattr(urllib.request, "urlopen", _mock_urlopen)

    notifier = NtfyNotifier(topic="explicit-topic")
    notifier.send("t", "m", "default")
    assert len(captured) == 1
    # The URL must reference the explicit topic, not the env var.
    url = captured[0].full_url
    assert "explicit-topic" in url
    assert "env-topic" not in url


# ---------------------------------------------------------------------------
# Determinism check
# ---------------------------------------------------------------------------


def test_decide_alerts_is_deterministic() -> None:
    """Same inputs must always produce identical output."""
    kwargs = dict(
        killed=False,
        quarantined=False,
        halted=True,
        orders_submitted=0,
        summary="drawdown breach",
        is_new_day=False,
    )
    a = decide_alerts(**kwargs)
    b = decide_alerts(**kwargs)
    assert a == b
    assert a[0].priority == "high"
