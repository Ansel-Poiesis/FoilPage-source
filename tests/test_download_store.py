# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Browser download queue persistence and state-machine tests."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from engine.download_store import (
    DownloadNotFoundError,
    DownloadStateError,
    DownloadStore,
)


OPTIONS = {
    "quality": "best-available",
    "audio_only": False,
    "transcribe_after_download": True,
    "language": "zh",
}


def test_download_store_lifecycle_and_persistence() -> None:
    with tempfile.TemporaryDirectory(prefix="download_store_") as temp:
        db = str(Path(temp) / "library.db")
        store = DownloadStore(db)
        created = store.create(
            "https://www.bilibili.com/video/BV1test", "示例", "bilibili", OPTIONS,
        )
        assert created["status"] == "received"
        claimed = store.claim_next()
        assert claimed and claimed["id"] == created["id"]
        assert claimed["status"] == "resolving" and claimed["attempts"] == 1
        store.transition(created["id"], "downloading", progress=0.25, bytes_done=25)
        store.transition(
            created["id"], "imported", progress=1.0, bytes_done=100,
            bytes_total=100, downloaded_path=str(Path(temp) / "video.mp4"),
            media_id="media-1",
        )
        store.transition(created["id"], "queued", job_id="job-1")
        store.transition(created["id"], "transcribing")
        store.transition(created["id"], "done")
        store.close()

        reopened = DownloadStore(db)
        try:
            restored = reopened.get(created["id"])
            assert restored and restored["status"] == "done"
            assert restored["options"] == OPTIONS
            assert restored["bytes_total"] == 100
        finally:
            reopened.close()


def test_cancel_and_retry_rules() -> None:
    store = DownloadStore()
    try:
        queued = store.create("https://youtu.be/example", "", "youtube", OPTIONS)
        cancelled = store.request_cancel(queued["id"])
        assert cancelled["status"] == "cancelled"
        with pytest.raises(DownloadStateError):
            store.retry(cancelled["id"])

        browser = store.create("https://www.douyin.com/video/1234567890", "浏览器下载", "douyin", OPTIONS)
        store.claim_next()
        store.transition(browser["id"], "awaiting_browser")
        assert store.request_cancel(browser["id"])["status"] == "cancelled"

        failed = store.create("https://youtu.be/failed", "", "youtube", OPTIONS)
        store.claim_next()
        store.transition(failed["id"], "downloading")
        store.transition(failed["id"], "failed", error={
            "code": "network", "message": "网络失败", "retryable": True,
        })
        retry = store.retry(failed["id"])
        assert retry["status"] == "received"
        assert retry["error"] is None and retry["progress"] == 0.0

        reclaimed = store.claim_next()
        assert reclaimed and reclaimed["id"] == failed["id"]
        store.transition(failed["id"], "failed", error={
            "code": "bad", "message": "不可重试", "retryable": False,
        })
        with pytest.raises(DownloadStateError):
            store.retry(failed["id"])
        with pytest.raises(DownloadNotFoundError):
            store.request_cancel("missing")
    finally:
        store.close()


def test_invalid_transitions_and_filters_are_rejected() -> None:
    store = DownloadStore()
    try:
        item = store.create("https://www.youtube.com/watch?v=x", "", "youtube", OPTIONS)
        with pytest.raises(DownloadStateError):
            store.transition(item["id"], "done")
        with pytest.raises(DownloadStateError):
            store.list(("not-a-status",))
    finally:
        store.close()


def test_active_download_can_be_force_deleted() -> None:
    store = DownloadStore()
    try:
        item = store.create("https://www.bilibili.com/video/BV1active", "活动下载", "bilibili", OPTIONS)
        claimed = store.claim_next()
        assert claimed and claimed["id"] == item["id"]
        store.transition(item["id"], "downloading", progress=0.5)
        # 非 force 仍拒绝运行中删除
        with pytest.raises(DownloadStateError):
            store.delete(item["id"])
        # force 直接删除；缺失行视为已取消
        assert store.delete(item["id"], force=True) is True
        assert store.get(item["id"]) is None
        assert store.is_cancel_requested(item["id"]) is True
    finally:
        store.close()


def test_schema_version_registration_is_idempotent_and_guards_future_versions(tmp_path) -> None:
    from engine.download_store import DownloadStore, DownloadStoreError

    db = tmp_path / "downloads.db"
    first = DownloadStore(str(db))
    first.create("https://example.com/a", "A", "generic", {})
    first.close()
    # 重开（含桌面与 Worker 双进程各自打开）幂等：组件版本保持 2，不重复迁移。
    second = DownloadStore(str(db))
    try:
        version = second._conn.execute(
            "SELECT version FROM app_schema WHERE component='downloads'"
        ).fetchone()["version"]
        assert version == 2
        # 未来版本拒绝：模拟更高版本的库被当前程序打开。
        second._conn.execute("UPDATE app_schema SET version=3 WHERE component='downloads'")
        second._conn.commit()
        second.close()
        try:
            DownloadStore(str(db))
        except DownloadStoreError as error:
            assert "高于当前程序支持版本" in str(error)
        else:
            raise AssertionError("future schema version must be rejected")
    finally:
        try:
            second.close()
        except Exception:
            pass
