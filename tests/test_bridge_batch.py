# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""批量 Agent 整理的 Bridge 接口测试（mock controller / 批量内核）。"""
from __future__ import annotations

from unittest import mock

from app.bridge import Bridge
from account_auth_fixtures import grant_account


class FakeStore:
    def __init__(self, base_dir):
        self.base_dir = base_dir


class FakeController:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.queue = mock.Mock()

    def save_settings(self, changes):
        self.settings.update(changes)
        return self.settings


def make_bridge(settings=None, *, account_dir):
    controller = FakeController(settings=settings)
    store = FakeStore(account_dir)
    grant_account(controller.settings, store.base_dir)
    bridge = Bridge(
        controller,
        worker=mock.Mock(),
        settings_store=store,
        window_provider=lambda: None,
        on_quit=lambda: None,
    )
    return bridge, controller


def test_batch_translate_requires_enabled_agent_and_key(tmp_path) -> None:
    bridge, _ = make_bridge({"agent_enabled": False, "agent_api_key": ""}, account_dir=tmp_path / "disabled")
    result = bridge.batch_agent_translate(["job:a", "job:b"])
    assert result["ok"] is False
    assert "启用 Agent 转译" in result["error"]

    bridge, _ = make_bridge({"agent_enabled": True, "agent_api_key": ""}, account_dir=tmp_path / "missing-key")
    result = bridge.batch_agent_translate(["job:a"])
    assert result["ok"] is False
    assert "API Key" in result["error"]


def test_batch_translate_rejects_when_already_running(tmp_path) -> None:
    bridge, _ = make_bridge({"agent_enabled": True, "agent_api_key": "k"}, account_dir=tmp_path / "data")
    bridge._batch_agent["running"] = True
    result = bridge.batch_agent_translate(["job:a"])
    assert result["ok"] is False
    assert "进行中" in result["error"]


def test_batch_translate_starts_worker_and_reports_stats(tmp_path) -> None:
    bridge, _ = make_bridge({"agent_enabled": True, "agent_api_key": "k"}, account_dir=tmp_path / "data")
    with mock.patch(
        "app.agent_service.batch_apply_agent_translation",
        return_value={"done": 2, "skipped": 1, "failed": 0, "errors": []},
    ) as core:
        with mock.patch("app.bridge.threading.Thread"):  # 不真正启动后台线程
            started = bridge.batch_agent_translate(["job:a", "job:b", "job:c"])
            assert started["ok"] is True
            assert started["count"] == 3
            bridge._batch_agent_worker(["a", "b", "c"])
            core.assert_called_once()
            assert core.call_args.args[0] == ["a", "b", "c"]
            status = bridge.get_batch_agent_status()
    assert status["running"] is False
    assert status["done"] == 2
    assert status["skipped"] == 1
    assert status["failed"] == 0
    assert "成功 2" in status["message"]


def test_batch_worker_failure_fallback_is_readable(tmp_path) -> None:
    bridge, _ = make_bridge({"agent_enabled": True, "agent_api_key": "k"}, account_dir=tmp_path / "data")
    with mock.patch("app.agent_service.batch_apply_agent_translation", side_effect=RuntimeError("boom")):
        bridge._batch_agent_worker(["a"])
    status = bridge.get_batch_agent_status()
    assert status["running"] is False
    assert status["failed"] == 1
    assert status["errors"] == ["boom"]
