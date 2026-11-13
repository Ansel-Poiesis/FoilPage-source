# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""CI Runner 服务联动（app/ci_runner.py）与启动钩子测试。"""
from __future__ import annotations

from unittest import mock

from app import ci_runner, desktop_web


def test_wake_runner_service_skips_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr(ci_runner.os, "name", "posix")
    assert ci_runner.wake_runner_service() == "skipped-non-windows"


def test_wake_runner_service_maps_known_service_states(monkeypatch) -> None:
    monkeypatch.setattr(ci_runner.os, "name", "nt")
    for stdout, expected in (
        ("not-installed", "not-installed"),
        ("already-running", "already-running"),
        ("started", "started"),
        ("start-pending", "start-pending"),
        ("start-failed", "start-failed"),
    ):
        probe = mock.Mock(return_value=stdout)
        monkeypatch.setattr(ci_runner, "_powershell", probe)
        assert ci_runner.wake_runner_service() == expected


def test_wake_runner_service_reports_probe_failure(monkeypatch) -> None:
    monkeypatch.setattr(ci_runner.os, "name", "nt")
    monkeypatch.setattr(ci_runner, "_powershell", mock.Mock(return_value=None))
    assert ci_runner.wake_runner_service() == "probe-failed"
    monkeypatch.setattr(ci_runner, "_powershell", mock.Mock(return_value="unexpected-output"))
    assert ci_runner.wake_runner_service() == "probe-failed"


def _wakeup_app(settings: dict, demo: bool = False) -> desktop_web.WebDesktopApp:
    app = desktop_web.WebDesktopApp.__new__(desktop_web.WebDesktopApp)
    app.demo = demo
    app.controller = mock.Mock()
    app.controller.settings = settings
    return app


def test_startup_hook_wakes_when_enabled(monkeypatch) -> None:
    wake = mock.Mock(return_value="started")
    monkeypatch.setattr(desktop_web.ci_runner, "wake_runner_service", wake)
    _wakeup_app({"ci_runner_wakeup": True})._ci_runner_wakeup_once()
    wake.assert_called_once()


def test_startup_hook_skips_when_disabled_or_demo(monkeypatch) -> None:
    wake = mock.Mock(return_value="started")
    monkeypatch.setattr(desktop_web.ci_runner, "wake_runner_service", wake)
    _wakeup_app({"ci_runner_wakeup": False})._ci_runner_wakeup_once()
    _wakeup_app({"ci_runner_wakeup": True}, demo=True)._ci_runner_wakeup_once()
    wake.assert_not_called()


def test_startup_hook_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(desktop_web.ci_runner, "wake_runner_service", mock.Mock(side_effect=RuntimeError("boom")))
    _wakeup_app({"ci_runner_wakeup": True})._ci_runner_wakeup_once()
