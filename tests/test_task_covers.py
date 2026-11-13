# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""TASK-8.17 三平台封面：URL 白名单、取图、cover_path 落库与门控。

扩展侧对应用例：tests/browser_extension.test.mjs（hook 封面提取）与
tests/extension_queue_behavior.test.mjs（提交载荷）。
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from app.controller import AppController
from app.download_coordinator import DownloadCoordinator
from engine.download_store import DownloadStore
from engine.downloader import (
    DownloadAdapterError,
    download_cover_image,
    is_douyin_cover_url,
    is_extractor_thumbnail_url,
)
from engine.settings import SettingsStore


class FakeCoverAdapter:
    """yt-dlp 适配器的测试替身：with_cover=True 时模拟写同名封面。"""

    def __init__(self, filename: str = "bili-source.mp4", thumbnail: bool = True) -> None:
        self.filename = filename
        self.thumbnail = thumbnail
        self.with_cover: list[bool] = []

    def resolve(self, *_args, **_kwargs):
        return {}

    def download(
        self, _url, destination, *, audio_only, rate_limit_bps, referer="", user_agent="",
        session_cookies=None, progress, cancel_check, js_runtime_path="",
        proxy_policy="auto", proxy_url="", with_cover=False,
    ):
        self.with_cover.append(bool(with_cover))
        target = Path(destination) / self.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-video-bytes")
        if with_cover and self.thumbnail:
            (Path(destination) / f"{target.stem}.jpg").write_bytes(b"fake-cover")
        progress(1, 1)
        return target


class StubClientConfig:
    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    def covers_enabled(self) -> bool:
        return self._enabled


DOUYIN_URL = "https://www.douyin.com/video/7673838983419530149"
BILI_URL = "https://www.bilibili.com/video/BV1cover"
COVER_URL = "https://p3-sign.douyinpic.com/tos-cn-i-0813/oQcover~noop.webp?x-expires=1"
MEDIA_HINT = "https://v3-web.douyinvod.com/video/tos/example.mp4?token=x"


def wait_for(coordinator: DownloadCoordinator, download_id: str, statuses: set[str]):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        item = coordinator.get(download_id)
        if item and item["status"] in statuses:
            return item
        time.sleep(0.02)
    raise AssertionError(f"下载任务未进入预期状态: {coordinator.get(download_id)}")


def _make_coordinator(tmp: str, adapter, *, covers_setting=True, cloud_flag=True):
    store = SettingsStore(base_dir=Path(tmp) / "data")
    store.update(download_covers=covers_setting)
    controller = AppController(store)
    coordinator = DownloadCoordinator(controller, adapter=adapter)
    coordinator.client_config = StubClientConfig(cloud_flag)
    return controller, coordinator


# ---------------------------------------------------------------- validators
@pytest.mark.parametrize("url,expected", [
    ("https://p3-sign.douyinpic.com/tos/oQ.jpeg", True),
    ("https://p9-va.byteimg.com/img/oQ.webp", True),
    ("//p3-sign.douyinpic.com/tos/oQ.jpeg", False),  # 未协议化即拒（桌面只收完整 https）
    ("http://p3-sign.douyinpic.com/tos/oQ.jpeg", False),
    ("https://v3-web.douyinvod.com/video/tos/example.mp4", False),
    ("https://evil.example/cover.jpg", False),
    ("", False),
])
def test_douyin_cover_url_allowlist(url: str, expected: bool) -> None:
    assert is_douyin_cover_url(url) is expected


@pytest.mark.parametrize("url,expected", [
    ("https://i0.hdslb.com/bfs/archive/abc.jpg", True),       # B 站
    ("https://i.ytimg.com/vi/abc/hq720.jpg", True),           # YouTube
    ("https://p3-sign.douyinpic.com/tos/oQ.jpeg", True),      # 抖音
    ("https://evil.example/thumb.jpg", False),
])
def test_extractor_thumbnail_allowlist(url: str, expected: bool) -> None:
    assert is_extractor_thumbnail_url(url) is expected


def test_download_cover_image_writes_file_and_rejects_hosts(tmp_path: Path) -> None:
    payload = b"\xff\xd8fake-jpeg"
    target = tmp_path / "cover.jpg"
    with mock.patch("engine.downloader._open_media") as opener:
        opener.return_value.__enter__.return_value.read.return_value = payload
        assert download_cover_image(
            "https://p3-sign.douyinpic.com/tos/oQ.jpeg", target,
        ) == target.resolve()
        assert target.read_bytes() == payload
    with pytest.raises(DownloadAdapterError):
        download_cover_image("https://evil.example/x.jpg", tmp_path / "bad.jpg")
    with mock.patch("engine.downloader._open_media") as opener:
        opener.return_value.__enter__.return_value.read.return_value = b""
        with pytest.raises(DownloadAdapterError):
            download_cover_image(
                "https://p3-sign.douyinpic.com/tos/oQ.jpeg", tmp_path / "empty.jpg"
            )


# -------------------------------------------------------------------- store
def test_store_persists_cover_path_and_migrates_v1_database() -> None:
    with tempfile.TemporaryDirectory(prefix="cover_store_") as temp:
        db = Path(temp) / "downloads.db"
        # 造一座 v1 旧库（无 cover_path 列）。
        legacy = sqlite3.connect(db)
        legacy.executescript(
            """
            CREATE TABLE app_schema (component TEXT PRIMARY KEY, version INTEGER NOT NULL);
            INSERT INTO app_schema VALUES ('downloads', 1);
            CREATE TABLE download_jobs (
                id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
                source TEXT NOT NULL, platform_hint TEXT NOT NULL,
                requested_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                options TEXT NOT NULL, status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0, bytes_total INTEGER,
                bytes_done INTEGER NOT NULL DEFAULT 0, downloaded_path TEXT,
                media_id TEXT, job_id TEXT, error TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO download_jobs
              (id,url,title,source,platform_hint,requested_at,updated_at,options,status)
            VALUES ('legacy0000001','https://youtu.be/x','旧任务','browser','youtube',
                    '2026-01-01','2026-01-01','{}','done');
            """
        )
        legacy.commit()
        legacy.close()

        store = DownloadStore(str(db))
        assert store.get("legacy0000001")["cover_path"] is None
        store.transition("legacy0000001", "done", cover_path="C:/dl/legacy.jpg")
        assert store.get("legacy0000001")["cover_path"] == "C:/dl/legacy.jpg"

        created = store.create(DOUYIN_URL, "新任务", "douyin", {"transcribe_after_download": False})
        assert created["cover_path"] is None
        store.transition(created["id"], "received", cover_path="C:/dl/new.jpg")
        assert store.get(created["id"])["cover_path"] == "C:/dl/new.jpg"
        # retry 清位：重下后旧封面记录不得残留。
        store.transition(created["id"], "resolving")
        store.transition(created["id"], "failed", error={"code": "x", "message": "x", "retryable": True})
        retried = store.retry(created["id"])
        assert retried["cover_path"] is None
        store.close()


# ------------------------------------------------------------- coordinator
def test_submit_persists_valid_cover_url_and_drops_invalid() -> None:
    with tempfile.TemporaryDirectory(prefix="cover_submit_") as temp:
        controller, coordinator = _make_coordinator(temp, FakeCoverAdapter())
        try:
            item = coordinator.submit(
                BILI_URL, options={"transcribe_after_download": False}, cover_url=COVER_URL,
            )
            assert item["options"]["cover_url"] == COVER_URL

            bad = coordinator.submit(
                "https://youtu.be/coverless", options={"transcribe_after_download": False},
                cover_url="https://evil.example/cover.jpg",
            )
            assert bad["options"]["cover_url"] == ""  # 非法封面只丢弃，不拒任务
        finally:
            controller.close()


def _fake_direct_media(destination_key: str = "captured"):
    def fake_direct(url, destination, **kwargs):
        target = Path(destination) / "captured-douyin.mp4"
        kwargs["progress"](4, 8)
        target.write_bytes(b"captured-media")
        kwargs["progress"](9, 9)
        return target
    return fake_direct


def test_douyin_media_hint_task_downloads_cover_to_sibling(tmp_path: Path, monkeypatch) -> None:
    with tempfile.TemporaryDirectory(prefix="cover_hint_") as temp:
        controller, coordinator = _make_coordinator(temp, FakeCoverAdapter())
        written: list[Path] = []

        def fake_fetch(url, destination, **kwargs):
            assert url == COVER_URL
            Path(destination).write_bytes(b"cover-bytes")
            written.append(Path(destination))
            return Path(destination)

        monkeypatch.setattr("app.download_coordinator.download_cover_image", fake_fetch)
        monkeypatch.setattr("app.download_coordinator.download_direct_media", _fake_direct_media())
        coordinator.start()
        try:
            item = coordinator.submit(
                DOUYIN_URL, options={"transcribe_after_download": False},
                media_url=MEDIA_HINT, cover_url=COVER_URL,
            )
            done = wait_for(coordinator, item["id"], {"done", "failed"})
            assert done["status"] == "done"
            assert done["cover_path"]
            assert Path(done["cover_path"]).name == Path(done["downloaded_path"]).stem + ".jpg"
            assert Path(done["cover_path"]).read_bytes() == b"cover-bytes"
            # 直下轨不经适配器；专用封面地址已由协调器自己取回。
            assert coordinator.adapter.with_cover == []
        finally:
            coordinator.stop()
            controller.close()


def test_ytdlp_track_records_adapter_cover(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="cover_ytdlp_") as temp:
        controller, coordinator = _make_coordinator(temp, FakeCoverAdapter())
        coordinator.start()
        try:
            item = coordinator.submit(
                BILI_URL, options={"transcribe_after_download": False},
            )
            done = wait_for(coordinator, item["id"], {"done", "failed"})
            assert done["status"] == "done"
            assert coordinator.adapter.with_cover == [True]
            assert done["cover_path"] and Path(done["cover_path"]).is_file()
            assert Path(done["cover_path"]).name == "bili-source.jpg"
        finally:
            coordinator.stop()
            controller.close()


def test_cover_failure_never_fails_the_task(tmp_path: Path, monkeypatch) -> None:
    with tempfile.TemporaryDirectory(prefix="cover_fail_") as temp:
        controller, coordinator = _make_coordinator(temp, FakeCoverAdapter())

        def boom(*_args, **_kwargs):
            raise DownloadAdapterError("cover_download_failed", "x")

        monkeypatch.setattr("app.download_coordinator.download_cover_image", boom)
        monkeypatch.setattr("app.download_coordinator.download_direct_media", _fake_direct_media())
        coordinator.start()
        try:
            item = coordinator.submit(
                DOUYIN_URL, options={"transcribe_after_download": False},
                media_url=MEDIA_HINT, cover_url=COVER_URL,
            )
            done = wait_for(coordinator, item["id"], {"done", "failed"})
            assert done["status"] == "done"
            assert done["cover_path"] is None
        finally:
            coordinator.stop()
            controller.close()


def test_local_setting_or_cloud_flag_disables_covers(tmp_path: Path) -> None:
    for covers_setting, cloud_flag, expect_cover in ((False, True, False), (True, False, False)):
        with tempfile.TemporaryDirectory(prefix="cover_gate_") as temp:
            controller, coordinator = _make_coordinator(
                temp, FakeCoverAdapter(), covers_setting=covers_setting, cloud_flag=cloud_flag,
            )
            coordinator.start()
            try:
                item = coordinator.submit(
                    BILI_URL, options={"transcribe_after_download": False},
                )
                done = wait_for(coordinator, item["id"], {"done", "failed"})
                assert done["status"] == "done"
                assert done["cover_path"] is None
                assert coordinator.adapter.with_cover == [False]
            finally:
                coordinator.stop()
                controller.close()


def test_coordinator_without_client_config_is_cover_dark(tmp_path: Path) -> None:
    """云开关缺省视为关闭：能力随热切换配置开启（TASK-8.16 语义）。"""
    with tempfile.TemporaryDirectory(prefix="cover_dark_") as temp:
        store = SettingsStore(base_dir=Path(temp) / "data")
        controller = AppController(store)
        coordinator = DownloadCoordinator(controller, adapter=FakeCoverAdapter())
        coordinator.start()
        try:
            item = coordinator.submit(
                BILI_URL, options={"transcribe_after_download": False},
            )
            done = wait_for(coordinator, item["id"], {"done", "failed"})
            assert done["status"] == "done"
            assert done["cover_path"] is None
            assert coordinator.adapter.with_cover == [False]
        finally:
            coordinator.stop()
            controller.close()


def test_browser_import_path_downloads_cover(tmp_path: Path, monkeypatch) -> None:
    """Chrome 下载轨：/v1/imports 导入完成后封面同样落位。"""
    with tempfile.TemporaryDirectory(prefix="cover_import_") as temp:
        controller, coordinator = _make_coordinator(temp, FakeCoverAdapter())
        media = Path(temp) / "chrome-dl.mp4"
        media.write_bytes(b"chrome-video")

        def fake_fetch(url, destination, **kwargs):
            Path(destination).write_bytes(b"cover-bytes")
            return Path(destination)

        monkeypatch.setattr("app.download_coordinator.download_cover_image", fake_fetch)
        row = controller.downloads.create(
            DOUYIN_URL, "导入任务", "douyin",
            {"mode": "browser", "transcribe_after_download": False, "cover_url": COVER_URL,
             "managed_download_root": str(Path(temp) / "dl")},
        )
        for status in ("resolving", "awaiting_browser"):
            controller.downloads.transition(row["id"], status)
        try:
            result = coordinator.submit_local_file(
                str(media), download_id=row["id"], transcribe=False, validate_media=False,
            )
            final = coordinator.get(row["id"])
            assert final["status"] == "done"
            assert final["downloaded_path"] == result["path"]
            # 比文件名与父目录归属，不比整串大小写（CI 的 TEMP 位于
            # C:\WINDOWS\TEMP 时 resolve 与原串的大小写可能不同）。
            assert Path(final["cover_path"]).name == Path(result["path"]).stem + ".jpg"
            assert Path(final["cover_path"]).parent.samefile(Path(result["path"]).parent)
            assert Path(final["cover_path"]).read_bytes() == b"cover-bytes"
        finally:
            controller.close()


# ------------------------------------------------------------ task rows / 删除
def test_task_rows_map_cover_to_transcription_row_and_delete_cleans_file() -> None:
    with tempfile.TemporaryDirectory(prefix="cover_rows_") as temp:
        store = SettingsStore(base_dir=Path(temp) / "data")
        store.update(download_dir=str(Path(temp) / "dl"))
        controller = AppController(store)
        download_root = Path(temp) / "dl"
        download_root.mkdir(parents=True, exist_ok=True)
        video = download_root / "t-video.mp4"
        cover = download_root / "t-video.jpg"
        video.write_bytes(b"v")
        cover.write_bytes(b"c")

        imported = controller.import_files([str(video)], display_names=["标题"])[0]
        queued = controller.enqueue_media([imported.item["id"]])
        job_id = queued.created[0]
        row = controller.downloads.create(
            BILI_URL, "标题", "bilibili",
            {"transcribe_after_download": True},
        )
        for status in ("resolving", "downloading", "imported"):
            controller.downloads.transition(row["id"], status)
        controller.downloads.transition(
            row["id"], "queued", media_id=imported.item["id"],
            downloaded_path=str(video), job_id=job_id, cover_path=str(cover),
        )

        rows = controller.task_rows()
        job_row = next(r for r in rows if r["task_id"] == job_id)
        assert job_row["cover_path"] == str(cover)  # 转写行继承下载行封面
        assert not any(r.get("task_id") == row["id"] for r in rows)  # 下载行已并入

        controller.delete_tasks([job_id])
        assert not video.exists()
        assert not cover.exists()  # 封面随源文件一并清理
        assert controller.downloads.get(row["id"]) is None
        controller.close()
