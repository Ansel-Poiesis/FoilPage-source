# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""通知循环对库级瞬时错误的退避（先生 2026-11-15）。

2026-11-14 08:20 实机 sqlite3.OperationalError（"database or disk is full"，
磁盘实际剩余 399GiB）连续 4 分钟每秒刷屏 234 次堆栈。本组用例锁住：同类
错误按指数退避节流且首错附带磁盘/库体积诊断；诊断自身异常不影响主流程。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from app.desktop_web import WebDesktopApp


class _Notifications:
    def update(self, tasks, enabled=True):
        return []


class _Retention:
    def run_if_due(self, settings):
        return None


class _Controller:
    def __init__(self, database_path: Path) -> None:
        self.settings = {"database_path": str(database_path), "task_notifications": True}
        self.media_action_lock = threading.Lock()
        self.failed_task_retention = _Retention()


class _Bridge:
    def __init__(self, fail: bool = True) -> None:
        self.fail = fail

    def get_notification_tasks(self):
        if self.fail:
            raise sqlite3.OperationalError("database or disk is full")
        return []

    def sync_subscription_in_background(self):
        return None

    def sync_product_notifications_in_background(self):
        return None


def _build_app(tmp_path: Path, bridge: _Bridge) -> WebDesktopApp:
    app = object.__new__(WebDesktopApp)
    app.bridge = bridge
    app.controller = _Controller(tmp_path / "library.db")
    app._closed = False
    app._close_lock = threading.Lock()
    app._notification_stop = threading.Event()
    app._notification_events = []
    app._notifications = _Notifications()
    app._plan_expiry_notice_once = lambda: None
    return app


def test_operational_error_backoff_logs_once_and_survives(tmp_path, caplog) -> None:
    app = _build_app(tmp_path, _Bridge(fail=True))
    caplog.set_level(logging.ERROR, logger="shengjian.desktop")
    thread = threading.Thread(target=app._notification_loop, daemon=True)
    thread.start()
    try:
        # 基础轮询周期 1s：窗口内恰好完成一次失败 + 进入 1s 退避；无退避时
        # 这里会以每秒一条的速度累积多条错误日志。
        time.sleep(1.6)
    finally:
        app._notification_stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    backoffs = [record for record in caplog.records if "开始退避" in record.getMessage()]
    assert len(backoffs) == 1, [record.getMessage() for record in caplog.records]
    assert "database or disk is full" in backoffs[0].getMessage()
    assert "disk_free=" in backoffs[0].getMessage()
    assert "db_size=" in backoffs[0].getMessage()


def test_operational_error_backoff_recovers_after_database_returns(tmp_path, caplog) -> None:
    bridge = _Bridge(fail=True)
    app = _build_app(tmp_path, bridge)
    caplog.set_level(logging.ERROR, logger="shengjian.desktop")
    thread = threading.Thread(target=app._notification_loop, daemon=True)
    thread.start()
    try:
        # 轮询周期 1s：t≈1.0 首次失败（退避 1s+）→ t≈1.3 恢复可读 → t≈3.0
        # 的一轮成功把退避复位 → t≈3.3 再次故障 → t≈4.0 的失败重新记录首错。
        time.sleep(1.3)
        bridge.fail = False
        time.sleep(2.0)
        bridge.fail = True
        time.sleep(1.2)
    finally:
        app._notification_stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    backoffs = [record for record in caplog.records if "开始退避" in record.getMessage()]
    assert len(backoffs) == 2, [record.getMessage() for record in caplog.records]


def test_database_health_snapshot_reports_free_and_size(tmp_path) -> None:
    database = tmp_path / "library.db"
    database.write_bytes(b"x" * 2048)
    app = _build_app(tmp_path, _Bridge())
    snapshot = app._database_health_snapshot()
    assert "disk_free=" in snapshot
    assert "GiB" in snapshot
    assert "db_size=0.0MiB" in snapshot


def test_database_health_snapshot_tolerates_missing_database(tmp_path) -> None:
    app = _build_app(tmp_path / "absent" / "library.db", _Bridge())
    snapshot = app._database_health_snapshot()
    # 磁盘统计与库体积各自独立降级，不抛异常。
    assert isinstance(snapshot, str) and "db_size=" in snapshot
