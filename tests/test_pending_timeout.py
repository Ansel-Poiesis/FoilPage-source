# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""悬挂任务判失败（先生 2026-10-25：默认两小时交给用户设置）。

awaiting_browser 是等待浏览器授权/接续的悬挂态；页面采集窗口约两小时，
超龄地址注定失效。sweep_stale_pending 按设置限期判失败并给出
收藏 → 刷新 → 重新下载 的指引（可重试）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.controller import AppController
from app.download_coordinator import DownloadCoordinator
from engine.contracts import DownloadOptions
from engine.settings import SettingsStore, SettingsValidationError


def make_coordinator(tmp: str, **settings):
    store = SettingsStore(base_dir=Path(tmp) / "data")
    if settings:
        store.update(**settings)
    controller = AppController(store)
    coordinator = DownloadCoordinator(controller)
    return controller, coordinator


def make_awaiting(coordinator: DownloadCoordinator) -> str:
    item = coordinator.store.create(
        "https://www.douyin.com/video/7300000000000000000",
        "悬挂中的视频",
        "douyin",
        DownloadOptions(mode="browser"),
    )
    coordinator.store.transition(item["id"], "resolving")
    coordinator.store.transition(item["id"], "awaiting_browser")
    return item["id"]


def age_download(coordinator: DownloadCoordinator, download_id: str, minutes: int) -> None:
    stale = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with coordinator.store._lock, coordinator.store._conn:
        coordinator.store._conn.execute(
            "UPDATE download_jobs SET updated_at=? WHERE id=?", (stale, download_id)
        )


def test_pending_timeout_setting_validated_and_defaulted(tmp_path):
    store = SettingsStore(base_dir=tmp_path / "data")
    assert store.load()["download_pending_timeout_minutes"] == 120
    store.update(download_pending_timeout_minutes=30)
    assert store.load()["download_pending_timeout_minutes"] == 30
    for bad in (4, 1441, "x", None, True):
        with pytest.raises(SettingsValidationError):
            store.update(download_pending_timeout_minutes=bad)


def test_sweep_fails_stale_awaiting_browser_with_guidance(tmp_path):
    controller, coordinator = make_coordinator(str(tmp_path))
    download_id = make_awaiting(coordinator)
    age_download(coordinator, download_id, minutes=200)

    swept = coordinator.sweep_stale_pending()

    assert swept == [download_id]
    item = coordinator.get(download_id)
    assert item["status"] == "failed"
    assert item["error"]["code"] == "download_pending_timeout"
    assert item["error"]["retryable"] is True
    assert "收藏" in item["error"]["message"]
    assert "刷新" in item["error"]["message"]
    assert "重新下载" in item["error"]["message"]


def test_sweep_keeps_fresh_and_respects_user_limit(tmp_path):
    controller, coordinator = make_coordinator(str(tmp_path), download_pending_timeout_minutes=30)
    fresh = make_awaiting(coordinator)
    assert coordinator.sweep_stale_pending() == []
    assert coordinator.get(fresh)["status"] == "awaiting_browser"

    just_inside = make_awaiting(coordinator)
    age_download(coordinator, just_inside, minutes=29)
    assert coordinator.sweep_stale_pending() == []

    beyond = make_awaiting(coordinator)
    age_download(coordinator, beyond, minutes=31)
    assert coordinator.sweep_stale_pending() == [beyond]
    assert coordinator.get(beyond)["error"]["code"] == "download_pending_timeout"


def test_sweep_invalid_setting_falls_back_to_default_window(tmp_path):
    controller, coordinator = make_coordinator(str(tmp_path))
    with controller._settings_lock:
        controller.settings["download_pending_timeout_minutes"] = "broken"
    download_id = make_awaiting(coordinator)
    age_download(coordinator, download_id, minutes=130)

    assert coordinator.sweep_stale_pending() == [download_id]
    assert "120 分钟" in coordinator.get(download_id)["error"]["message"]
