# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""VPN 隧道诊断：412/网络错误必须带出系统级隧道证据（2026-11-01 取证）。"""
from __future__ import annotations

import subprocess
import tempfile
import types
from pathlib import Path

from app.controller import AppController
from app.download_coordinator import DownloadCoordinator
from app.network_diagnostics import (
    active_vpn_tunnel, reset_vpn_probe_cache_for_tests,
)
from engine.settings import SettingsStore


def _fake_probe_run(alias: str, *, returncode: int = 0):
    def run(cmd, **_kwargs):
        return types.SimpleNamespace(
            stdout=alias.encode("utf-8"), returncode=returncode,
        )
    return run


def test_probe_reports_vpn_interface_alias(monkeypatch) -> None:
    reset_vpn_probe_cache_for_tests()
    monkeypatch.setattr(subprocess, "run", _fake_probe_run("WindscribeWireguard\r\n"))
    assert active_vpn_tunnel() == "WindscribeWireguard"


def test_probe_silent_for_physical_adapter(monkeypatch) -> None:
    reset_vpn_probe_cache_for_tests()
    monkeypatch.setattr(subprocess, "run", _fake_probe_run("以太网"))
    assert active_vpn_tunnel() == ""


def test_probe_swallows_subprocess_failure(monkeypatch) -> None:
    reset_vpn_probe_cache_for_tests()

    def boom(*_args, **_kwargs):
        raise OSError("powershell 不可用")

    monkeypatch.setattr(subprocess, "run", boom)
    assert active_vpn_tunnel() == ""


def test_probe_ignores_error_output(monkeypatch) -> None:
    reset_vpn_probe_cache_for_tests()
    monkeypatch.setattr(subprocess, "run", _fake_probe_run("", returncode=1))
    assert active_vpn_tunnel() == ""


def test_412_failure_persists_vpn_tunnel_evidence(monkeypatch) -> None:
    reset_vpn_probe_cache_for_tests()
    monkeypatch.setattr(
        "app.download_coordinator.active_vpn_tunnel", lambda: "WindscribeWireguard",
    )
    with tempfile.TemporaryDirectory(prefix="vpn_diag_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = DownloadCoordinator(controller, adapter=types.SimpleNamespace())
        try:
            created = coordinator.submit(
                "https://www.bilibili.com/video/BV1vpntunnel", title="隧道诊断",
                options={"transcribe_after_download": False},
            )
            coordinator._fail(
                created["id"], "source_rate_limited",
                "B 站暂时拒绝了请求（HTTP 412），请稍后重试或先在浏览器登录后再下载", True,
            )
            failed = coordinator.get(created["id"])
            assert failed["status"] == "received"  # 可重试错误先回队列，错误仍持久化
            error = failed["error"]
            assert "VPN 隧道" in error["message"]
            assert error["diagnostic"]["vpn_tunnel"] == "WindscribeWireguard"
        finally:
            coordinator.stop()
            controller.close()


def test_unrelated_failure_keeps_message_untouched(monkeypatch) -> None:
    reset_vpn_probe_cache_for_tests()
    monkeypatch.setattr("app.download_coordinator.active_vpn_tunnel", lambda: "")
    with tempfile.TemporaryDirectory(prefix="vpn_diag_other_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = DownloadCoordinator(controller, adapter=types.SimpleNamespace())
        try:
            created = coordinator.submit(
                "https://www.bilibili.com/video/BV1vpnother", title="无隧道网络错误",
                options={"transcribe_after_download": False},
            )
            coordinator._fail(created["id"], "network_error", "网络连接不稳定或平台暂时无响应，请稍后重试", True)
            error = coordinator.get(created["id"])["error"]
            assert "VPN" not in error["message"]
            assert "diagnostic" not in error
        finally:
            coordinator.stop()
            controller.close()
