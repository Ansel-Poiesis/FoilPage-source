# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""TASK-8.18 抖音图集：载荷清洗、下载引擎（并发/候选回退/断点续传）与门控。

取证依据见 Poiesis vault 140 号计划 2026-10-22 回填：签名图片直链裸 UA
可直下（无 Referer/Cookie 门禁），CDN 全在 douyinpic.com 域。
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from app.controller import AppController
from app.download_coordinator import DownloadCoordinator
from engine.downloader import DownloadAdapterError
from engine.settings import SettingsStore


NOTE_URL = "https://www.douyin.com/note/7699296292874789243"


def gallery_images(count: int = 3, bad_rows: list[int] | None = None) -> list[list[str]]:
    bad_rows = bad_rows or []
    rows: list[list[str]] = []
    for index in range(count):
        if index in bad_rows:
            rows.append(["https://evil.example/x.jpg", f"https://p3-sign.douyinpic.com/obj/fallback-{index}.jpeg"])
        else:
            rows.append([
                f"https://p3-sign.douyinpic.com/tos-cn-i-0813/obj-{index}-a.webp",
                f"https://p9-pc.douyinpic.com/tos-cn-i-0813/obj-{index}-b.jpeg",
            ])
    return rows


class StubClientConfig:
    def __init__(self, *, covers: bool = True, gallery: bool = True) -> None:
        self._covers = covers
        self._gallery = gallery

    def covers_enabled(self) -> bool:
        return self._covers

    def gallery_enabled(self) -> bool:
        return self._gallery


def wait_for(coordinator: DownloadCoordinator, download_id: str, statuses: set[str]):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        item = coordinator.get(download_id)
        if item and item["status"] in statuses:
            return item
        time.sleep(0.02)
    raise AssertionError(f"图集任务未进入预期状态: {coordinator.get(download_id)}")


def make_coordinator(tmp: str, *, covers_setting: bool = True, cloud: StubClientConfig | None = None):
    store = SettingsStore(base_dir=Path(tmp) / "data")
    store.update(download_covers=covers_setting)
    controller = AppController(store)
    coordinator = DownloadCoordinator(controller)
    coordinator.client_config = cloud or StubClientConfig()
    return controller, coordinator


def fake_image_fetch(store_dir: Path, fail_urls: set[str] | None = None):
    """图片直下替身：按 URL 落 bytes；fail_urls 中的候选全部失败。"""
    fail_urls = fail_urls or set()

    def fetch(url, destination, **kwargs):
        if url in fail_urls:
            raise DownloadAdapterError("media_http_error", "fixture refusal")
        Path(destination).write_bytes(b"img-" + url[-12:].encode("utf-8", "ignore"))
        return Path(destination)

    return fetch


# ------------------------------------------------------------------- submit
def test_submit_persists_gallery_and_forces_no_transcribe() -> None:
    with tempfile.TemporaryDirectory(prefix="gallery_submit_") as temp:
        controller, coordinator = make_coordinator(temp)
        try:
            item = coordinator.submit(
                NOTE_URL, title="格子图",
                options={"transcribe_after_download": True},
                gallery_images=gallery_images(3, bad_rows=[1]),
            )
            assert len(item["options"]["gallery_images"]) == 3
            # 坏 host 候选被剔除，合法候选保留（候选回退面）。
            assert item["options"]["gallery_images"][1] == [
                "https://p3-sign.douyinpic.com/obj/fallback-1.jpeg",
            ]
            assert item["options"]["transcribe_after_download"] is False  # 图文无音轨
        finally:
            controller.close()


@pytest.mark.parametrize("payload,code", [
    ([], "invalid_gallery"),                     # 空组
    ([[f"https://p3.douyinpic.com/a{i}.jpg" for i in range(9)]], "invalid_gallery"),  # 候选>8
    ([["https://evil.example/x.jpg"]], "invalid_gallery"),      # 全坏 host
    ("not-a-list", "invalid_gallery"),
])
def test_submit_rejects_invalid_gallery_payloads(payload, code) -> None:
    with tempfile.TemporaryDirectory(prefix="gallery_bad_") as temp:
        controller, coordinator = make_coordinator(temp)
        try:
            with pytest.raises(DownloadAdapterError) as exc:
                coordinator.submit(NOTE_URL, gallery_images=payload)
            assert exc.value.code == code
        finally:
            controller.close()


# -------------------------------------------------------------------- engine
def test_gallery_engine_downloads_all_images_and_cover() -> None:
    with tempfile.TemporaryDirectory(prefix="gallery_ok_") as temp:
        controller, coordinator = make_coordinator(temp)
        cover_url = "https://p3-sign.douyinpic.com/tos-cn-i-0813/cover.webp"
        with mock.patch("app.download_coordinator.download_cover_image",
                        fake_image_fetch(Path(temp))):
            coordinator.start()
            try:
                item = coordinator.submit(
                    NOTE_URL, title="图书馆", cover_url=cover_url,
                    gallery_images=gallery_images(3),
                )
                done = wait_for(coordinator, item["id"], {"done", "failed"})
                assert done["status"] == "done"
                gallery_dir = Path(done["downloaded_path"])
                assert gallery_dir.is_dir()
                names = sorted(p.name for p in gallery_dir.iterdir())
                assert names == ["001.webp", "002.webp", "003.webp", "cover.jpg"]
                assert done["cover_path"] and Path(done["cover_path"]).is_file()
                assert done["options"]["gallery_done"] == [0, 1, 2]
                assert done["media_id"] is None  # 无音轨，不进媒体库/转写
            finally:
                coordinator.stop()
                controller.close()


def test_gallery_partial_failure_resumes_only_missing_images() -> None:
    with tempfile.TemporaryDirectory(prefix="gallery_resume_") as temp:
        controller, coordinator = make_coordinator(temp)
        images = gallery_images(3)
        failing = {images[1][0], images[1][1]}  # 第 2 张全部候选失败
        with mock.patch("app.download_coordinator.download_cover_image",
                        fake_image_fetch(Path(temp), fail_urls=failing)):
            coordinator.start()
            try:
                item = coordinator.submit(NOTE_URL, title="断点", gallery_images=images)
                failed = wait_for(coordinator, item["id"], {"failed"})
                assert failed["status"] == "failed"
                assert failed["error"]["code"] == "gallery_partial"
                assert failed["options"]["gallery_done"] == [0, 2]
                # 失败行目录路径留在 downloading 阶段；按已落盘文件断言：
                saved = sorted(p.name for p in Path(controller.settings["download_dir"]).rglob("00*.webp"))
                assert saved == ["001.webp", "003.webp"]
            finally:
                coordinator.stop()
                controller.close()

        # 重试：第 2 张候选恢复可用，只补缺图（其余文件在即跳过）。
        controller2, coordinator2 = make_coordinator(temp)
        fetched: list[str] = []

        def tracking_fetch(url, destination, **kwargs):
            fetched.append(url)
            Path(destination).write_bytes(b"img")
            return Path(destination)

        with mock.patch("app.download_coordinator.download_cover_image", tracking_fetch):
            coordinator2.start()
            try:
                coordinator2.retry(item["id"])
                done = wait_for(coordinator2, item["id"], {"done", "failed"})
                assert done["status"] == "done"
                # 只补了第 2 张（候选 1 个成功即止）——001/003 文件在即跳过。
                assert all("obj-1-" in url for url in fetched)
                assert len(fetched) == 1
            finally:
                coordinator2.stop()
                controller2.close()


def test_local_api_strips_gallery_when_cloud_flag_off(tmp_path) -> None:
    from account_auth_fixtures import grant_account
    from app.local_api import LocalApiServer
    from tests.test_local_api import StubCoordinator, call, ORIGIN

    controller = AppController(SettingsStore(base_dir=tmp_path / "data"))
    controller.settings = grant_account(controller.settings_store, controller.settings_store.base_dir)
    coordinator = StubCoordinator()
    server = LocalApiServer(controller, coordinator, port=0)
    server.start()
    token = controller.settings["local_api_token"]
    payload = {
        "url": NOTE_URL, "title": "门控",
        "gallery_images": [["https://p3.douyinpic.com/a.webp"]],
        "options": {"mode": "local"},
    }
    try:
        status, data, _ = call(server, "POST", "/v1/downloads", token=token, origin=ORIGIN, body=payload)
        assert status == 202
        assert coordinator.gallery_images == [None], "默认云开关关：gallery_images 必须剥成 None"

        with mock.patch.object(server.client_config, "gallery_enabled", return_value=True):
            status, data, _ = call(server, "POST", "/v1/downloads", token=token, origin=ORIGIN,
                                   body={**payload, "url": "https://www.douyin.com/note/7699296292874789244"})
            assert status == 202
            assert coordinator.gallery_images[-1] == [["https://p3.douyinpic.com/a.webp"]]

            status, data, _ = call(server, "POST", "/v1/downloads", token=token, origin=ORIGIN,
                                   body={**payload, "url": "https://www.douyin.com/note/7699296292874789245",
                                         "gallery_images": [["https://p3.douyinpic.com/a.webp"], "bad"]})
            assert status == 400 and data["error"]["code"] == "invalid_gallery"
    finally:
        server.stop()
        controller.close()


def test_controller_delete_removes_gallery_directory() -> None:
    with tempfile.TemporaryDirectory(prefix="gallery_del_") as temp:
        store = SettingsStore(base_dir=Path(temp) / "data")
        controller = AppController(store)
        root = Path(controller.settings["download_dir"])
        root.mkdir(parents=True, exist_ok=True)
        gallery_dir = root / "格子图-7699296292874789243"
        gallery_dir.mkdir()
        (gallery_dir / "001.webp").write_bytes(b"a")
        (gallery_dir / "cover.webp").write_bytes(b"c")
        row = controller.downloads.create(
            NOTE_URL, "格子图", "douyin",
            {"gallery_images": [["https://p3.douyinpic.com/a.webp"]], "transcribe_after_download": False},
        )
        controller.downloads.transition(row["id"], "resolving")
        controller.downloads.transition(row["id"], "downloading")
        controller.downloads.transition(
            row["id"], "imported", downloaded_path=str(gallery_dir),
            cover_path=str(gallery_dir / "cover.webp"),
            options={"gallery_images": [["https://p3.douyinpic.com/a.webp"]], "gallery_done": [0]},
        )
        controller.downloads.transition(row["id"], "done")
        assert controller.delete_download_tasks([row["id"]]) == [row["id"]]
        assert not gallery_dir.exists()
        controller.close()
