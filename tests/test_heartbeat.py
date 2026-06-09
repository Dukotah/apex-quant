"""Tests for apex.ops.heartbeat — the external dead-man's-switch pinger.

The module must (1) stay OFF until the env var opts it in, (2) use healthchecks.io
ping conventions (base URL on success, ``/fail`` on failure), and (3) NEVER raise —
a monitoring failure must not break a trading cycle.
"""

from __future__ import annotations

import pytest

from apex.ops.heartbeat import (
    HEARTBEAT_URL_ENV,
    HealthchecksPinger,
    Pinger,
    ping_heartbeat,
)


class _RecordingOpener:
    """An injectable opener that records every URL it was asked to open."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float | None]] = []

    def __call__(self, url, timeout=None):  # noqa: ANN001 - mirrors urlopen signature
        self.calls.append((url, timeout))
        return object()  # urlopen returns a response; the pinger ignores it


class _BoomOpener:
    """An opener that always raises — proves ping() swallows network errors."""

    def __call__(self, url, timeout=None):  # noqa: ANN001
        raise OSError("network down")


# --------------------------------------------------------------------- opt-in


def test_no_url_anywhere_is_a_noop(monkeypatch):
    """No arg and no env var → disabled, and ping never touches the opener."""
    monkeypatch.delenv(HEARTBEAT_URL_ENV, raising=False)
    opener = _RecordingOpener()
    pinger = HealthchecksPinger(opener=opener)
    assert pinger.enabled is False
    pinger.ping(success=True)
    pinger.ping(success=False)
    assert opener.calls == []


def test_env_var_opts_in(monkeypatch):
    monkeypatch.setenv(HEARTBEAT_URL_ENV, "https://hc-ping.com/abc")
    opener = _RecordingOpener()
    pinger = HealthchecksPinger(opener=opener)
    assert pinger.enabled is True
    pinger.ping()
    assert opener.calls == [("https://hc-ping.com/abc", pytest.approx(10.0))]


def test_explicit_url_overrides_env(monkeypatch):
    monkeypatch.setenv(HEARTBEAT_URL_ENV, "https://hc-ping.com/from-env")
    opener = _RecordingOpener()
    HealthchecksPinger("https://hc-ping.com/explicit", opener=opener).ping()
    assert opener.calls[0][0] == "https://hc-ping.com/explicit"


# ----------------------------------------------------------- ping conventions


def test_success_pings_base_url():
    opener = _RecordingOpener()
    HealthchecksPinger("https://hc-ping.com/x", opener=opener).ping(success=True)
    assert opener.calls[0][0] == "https://hc-ping.com/x"


def test_failure_pings_fail_suffix():
    opener = _RecordingOpener()
    HealthchecksPinger("https://hc-ping.com/x", opener=opener).ping(success=False)
    assert opener.calls[0][0] == "https://hc-ping.com/x/fail"


def test_default_success_is_true():
    opener = _RecordingOpener()
    HealthchecksPinger("https://hc-ping.com/x", opener=opener).ping()
    assert opener.calls[0][0] == "https://hc-ping.com/x"  # no /fail


def test_trailing_slash_is_stripped():
    """A trailing slash must not produce a double-slash before /fail."""
    opener = _RecordingOpener()
    HealthchecksPinger("https://hc-ping.com/x/", opener=opener).ping(success=False)
    assert opener.calls[0][0] == "https://hc-ping.com/x/fail"


def test_whitespace_url_is_disabled(monkeypatch):
    monkeypatch.delenv(HEARTBEAT_URL_ENV, raising=False)
    opener = _RecordingOpener()
    pinger = HealthchecksPinger("   ", opener=opener)
    assert pinger.enabled is False
    pinger.ping()
    assert opener.calls == []


def test_custom_timeout_is_passed_through():
    opener = _RecordingOpener()
    HealthchecksPinger("https://hc-ping.com/x", opener=opener, timeout=2.5).ping()
    assert opener.calls[0][1] == pytest.approx(2.5)


# ------------------------------------------------------------- never raises


def test_ping_swallows_opener_errors():
    """A failing opener must not propagate — the cron cycle must survive."""
    pinger = HealthchecksPinger("https://hc-ping.com/x", opener=_BoomOpener())
    pinger.ping(success=True)  # must not raise
    pinger.ping(success=False)  # must not raise


# ------------------------------------------------------- convenience wrapper


def test_ping_heartbeat_uses_injected_pinger():
    class _FakePinger:
        def __init__(self) -> None:
            self.pings: list[bool] = []

        def ping(self, success: bool = True) -> None:
            self.pings.append(success)

    fake = _FakePinger()
    ping_heartbeat(True, pinger=fake)
    ping_heartbeat(False, pinger=fake)
    assert fake.pings == [True, False]


def test_ping_heartbeat_default_pinger_is_noop_without_env(monkeypatch):
    """With no URL configured, the convenience wrapper is a safe no-op."""
    monkeypatch.delenv(HEARTBEAT_URL_ENV, raising=False)
    ping_heartbeat(True)  # must not raise


def test_healthchecks_pinger_satisfies_protocol():
    assert isinstance(HealthchecksPinger("https://hc-ping.com/x"), Pinger)
