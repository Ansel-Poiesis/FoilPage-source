# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Registered-video actions against real isolated SQLite/file stores."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.bridge import Bridge
from app.controller import AppController
from app.download_coordinator import DownloadCoordinator
from app.local_api import public_download_job
from engine.settings import SettingsStore


@pytest.fixture
def app(tmp_path):
    store = SettingsStore(base_dir=tmp_path / "data")
    controller = AppController(store)
    bridge = Bridge(controller, Mock(), store, lambda: None, lambda: None)
    yield controller, bridge
    bridge.close_media_preview()
    controller.close()


def video(controller, root, content=b"fixture-video"):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "video.mp4"
    path.write_bytes(content)
    item = controller.media.import_file(str(path)).item
    return path, item


def download(controller, path, item, *, options=None):
    row = controller.downloads.create("https://www.bilibili.com/video/BV1fixture", "video", "bilibili", options or {})
    controller.downloads.transition(row["id"], "resolving")
    controller.downloads.transition(row["id"], "downloading")
    controller.downloads.transition(row["id"], "imported", downloaded_path=str(path), media_id=item["id"])
    return controller.downloads.transition(row["id"], "done")


def test_imported_source_is_preserved_and_location_uses_registered_id(app, tmp_path, monkeypatch):
    controller, bridge = app
    path, item = video(controller, tmp_path / "original")
    opened = []
    monkeypatch.setattr(bridge, "open_path", lambda value: opened.append(value) or {"ok": True})
    assert bridge.open_video_location(item["id"])["ok"]
    assert opened == [str(path)]
    assert not bridge.open_video_location(str(path))["ok"]
    assert bridge.get_downloaded_videos()["items"][0]["delete_label"] == "从视频库移除"
    assert bridge.delete_video(item["id"])["ok"]
    assert path.exists()
    assert bridge.get_downloaded_videos()["items"] == []


def test_managed_download_deletes_file_and_detaches_download_record(app):
    controller, bridge = app
    path, item = video(controller, Path(controller.settings["download_dir"]))
    row = download(controller, path, item)
    result = bridge.delete_video(item["id"])
    assert result["ok"] and result["message"] == "视频已删除"
    assert not path.exists() and result["items"] == []
    assert controller.downloads.get(row["id"])["media_id"] is None
    assert controller.downloads.get(row["id"])["downloaded_path"] is None
    assert not controller.queue_download_for_transcription(row["id"]).created


def test_active_job_prevents_delete_and_keeps_source_and_record(app, tmp_path):
    controller, bridge = app
    path, item = video(controller, tmp_path / "original")
    queued = controller.enqueue_media([item["id"]])
    assert queued.created
    assert bridge.get_downloaded_videos()["items"][0]["deletable"] is False
    assert not bridge.delete_video(item["id"])["ok"]
    assert path.exists() and controller.media.get_media(item["id"])


def test_missing_record_can_be_removed_but_changed_file_is_preserved(app):
    controller, bridge = app
    path, item = video(controller, Path(controller.settings["download_dir"]))
    download(controller, path, item)
    path.write_bytes(b"replacement-user-content")
    assert not bridge.delete_video(item["id"])["ok"]
    assert path.read_bytes() == b"replacement-user-content"
    path.unlink()
    assert bridge.get_downloaded_videos()["items"][0]["delete_label"] == "移除记录"
    assert bridge.delete_video(item["id"])["ok"]


def test_save_directory_applies_to_new_jobs_preserves_queued_destination_and_private_root(app, tmp_path):
    controller, bridge = app
    coordinator = DownloadCoordinator(controller)
    old = coordinator.submit("https://www.bilibili.com/video/BV1old")
    folder = tmp_path / "chosen folder"
    window = Mock()
    window.create_file_dialog.return_value = [str(folder)]
    bridge._window = lambda: window
    result = bridge.pick_setting_dir("download_dir")
    assert result["ok"] and Path(controller.settings["download_dir"]) == folder
    new = coordinator.submit("https://www.bilibili.com/video/BV1new")
    assert new["options"]["managed_download_root"] == str(folder.resolve())
    assert old["options"]["managed_download_root"] != new["options"]["managed_download_root"]
    assert "managed_download_root" not in public_download_job(new)["options"]
    # A downloaded file remains managed even after its folder is changed.
    path, item = video(controller, Path(old["options"]["managed_download_root"]))
    download(controller, path, item, options=old["options"])
    assert bridge.delete_video(item["id"])["ok"] and not path.exists()


def test_folder_picker_cancel_leaves_setting_unchanged(app):
    controller, bridge = app
    previous = controller.settings["download_dir"]
    window = Mock()
    window.create_file_dialog.return_value = None
    bridge._window = lambda: window
    assert bridge.pick_setting_dir("download_dir")["cancelled"]
    assert controller.settings["download_dir"] == previous


def test_reimported_replacement_does_not_inherit_download_file_ownership(app):
    controller, bridge = app
    path, item = video(controller, Path(controller.settings["download_dir"]))
    row = download(controller, path, item)
    path.write_bytes(b"new-user-video")
    replacement = controller.import_files([str(path)])[0].item
    assert replacement["id"] == item["id"]
    assert replacement["original_content_sha256"] != replacement["content_sha256"]
    assert bridge.get_downloaded_videos()["items"][0]["delete_label"] == "从视频库移除"
    assert bridge.delete_video(item["id"])["ok"]
    assert path.read_bytes() == b"new-user-video"
    # Restart recovery must respect an intentional library removal.
    controller._recover_one_download(controller.downloads.get(row["id"]))
    assert controller.media.list_media() == []


def test_inflight_download_cannot_be_deleted_before_enqueue(app):
    controller, bridge = app
    path, item = video(controller, Path(controller.settings["download_dir"]))
    row = controller.downloads.create("https://www.bilibili.com/video/BV1flight", "video", "bilibili", {})
    for state in ("resolving", "downloading"):
        controller.downloads.transition(row["id"], state)
    controller.downloads.transition(row["id"], "imported", downloaded_path=str(path), media_id=item["id"])
    assert not bridge.delete_video(item["id"])["ok"]
    assert path.exists()


def test_preview_disappearance_does_not_fail_whole_video_library(app, tmp_path, monkeypatch):
    controller, bridge = app
    _, item = video(controller, tmp_path / "original")
    monkeypatch.setattr(bridge._preview_server(), "urls", Mock(side_effect=FileNotFoundError()))
    data = bridge.get_downloaded_videos()
    assert data["ok"] and data["items"][0]["id"] == item["id"]
    assert data["items"][0]["available"] is False


def test_browser_download_uses_managed_copy_in_chosen_folder(app, tmp_path, monkeypatch):
    controller, bridge = app
    controller.save_settings({"download_dir": str(tmp_path / "chosen")})
    coordinator = DownloadCoordinator(controller)
    source, _ = video(controller, tmp_path / "chrome" / "FoilPage")
    # Remove test registration before exercising the browser ingestion path.
    controller.media.clear_media()
    monkeypatch.setattr("app.download_coordinator.validate_media_streams", lambda *a: None)
    row = controller.downloads.create("https://www.bilibili.com/video/BV1browser", "video", "bilibili",
        {"mode": "browser", "transcribe_after_download": False, "managed_download_root": str(tmp_path / "chosen")})
    coordinator.store.transition(row["id"], "resolving")
    coordinator.store.transition(row["id"], "awaiting_browser")
    result = coordinator.submit_local_file(str(source), transcribe=False, download_id=row["id"], validate_media=True)
    managed = Path(result["path"])
    assert managed.parent == tmp_path / "chosen" and managed.read_bytes() == source.read_bytes()
    assert bridge.delete_video(result["media_id"])["ok"]
    assert not managed.exists() and source.exists()


def test_cancel_during_browser_validation_never_imports_or_enqueues(app, tmp_path, monkeypatch):
    controller, bridge = app
    source, _ = video(controller, tmp_path / "chrome" / "声笺")
    controller.media.clear_media()
    coordinator = DownloadCoordinator(controller)
    row = controller.downloads.create("https://www.bilibili.com/video/BV1cancel", "video", "bilibili", {"mode": "browser"})
    for status in ("resolving", "awaiting_browser"):
        coordinator.store.transition(row["id"], status)
    def validate(*_):
        coordinator.store.request_cancel(row["id"])
    monkeypatch.setattr("app.download_coordinator.validate_media_streams", validate)
    result = coordinator.submit_local_file(str(source), transcribe=True, download_id=row["id"], validate_media=True)
    assert result["job_id"] is None and controller.queue.list_jobs() == []
    assert controller.media.list_media() == [] and source.exists()
