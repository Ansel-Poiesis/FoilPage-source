# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""One queue snapshot serves a library read without weakening active-file protection."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.bridge import Bridge
from app.controller import AppController
from engine.settings import SettingsStore


@pytest.fixture
def application(tmp_path, monkeypatch):
    store = SettingsStore(base_dir=tmp_path / "data")
    controller = AppController(store)
    bridge = Bridge(controller, Mock(), store, lambda: None, lambda: None)
    monkeypatch.setattr(controller.video_catalog, "request_scan", Mock(return_value={"running": False}))
    monkeypatch.setattr(bridge, "_preview_server", lambda: Mock(urls=lambda _id: {}))
    yield controller, bridge
    controller.close()


def registered(controller, path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return controller.media.import_file(str(path)).item


def test_library_reads_empty_queue_once_for_all_videos(application, tmp_path, monkeypatch):
    controller, bridge = application
    for index in range(4):
        registered(controller, tmp_path / "originals" / f"{index}.mp4", f"synthetic-{index}".encode())
    read = Mock(wraps=controller.queue.list_jobs)
    monkeypatch.setattr(controller.queue, "list_jobs", read)
    result = bridge.get_downloaded_videos()
    assert result["ok"] and result["total"] == 4
    assert all(item["deletable"] and not item["delete_file"] for item in result["items"])
    read.assert_called_once_with()


@pytest.mark.parametrize("status", ["queued", "running", "retryable"])
def test_cached_queue_protects_every_active_status_and_delete_rechecks(application, tmp_path, monkeypatch, status):
    controller, bridge = application
    active_path = tmp_path / "originals" / "active.mp4"
    item = registered(controller, active_path, b"synthetic-active")
    idle = registered(controller, tmp_path / "originals" / "idle.mp4", b"synthetic-idle")
    job_id = controller.enqueue_media([item["id"]]).created[0]
    if status != "queued":
        assert controller.queue.claim_next("synthetic-worker")["id"] == job_id
        if status == "retryable":
            controller.queue.update_status(job_id, "retryable", worker_id="synthetic-worker")
    read = Mock(wraps=controller.queue.list_jobs)
    monkeypatch.setattr(controller.queue, "list_jobs", read)
    result = bridge.get_downloaded_videos()
    by_id = {row["id"]: row for row in result["items"]}
    assert by_id[item["id"]]["deletable"] is False
    assert by_id[idle["id"]]["deletable"] is True
    read.assert_called_once_with()
    # A direct action must get a fresh queue snapshot, not retain the list cache.
    assert bridge.delete_video(item["id"])["ok"] is False
    assert read.call_count == 2
    assert active_path.is_file() and controller.media.get_media(item["id"])
    assert controller.queue.get_job(job_id)["status"] == status


def test_download_activity_still_protects_without_an_asr_job(application, tmp_path, monkeypatch):
    controller, bridge = application
    path = Path(controller.settings["download_dir"]) / "active.mp4"
    item = registered(controller, path, b"synthetic-download-active")
    download = controller.downloads.create("https://www.bilibili.com/video/BV1synthetic", "video", "bilibili", {})
    for status in ("resolving", "downloading"):
        controller.downloads.transition(download["id"], status)
    controller.downloads.transition(download["id"], "imported", downloaded_path=str(path), media_id=item["id"])
    read = Mock(wraps=controller.queue.list_jobs)
    monkeypatch.setattr(controller.queue, "list_jobs", read)
    assert bridge.get_downloaded_videos()["items"][0]["deletable"] is False
    read.assert_called_once_with()
    assert bridge.delete_video(item["id"])["ok"] is False
    assert path.is_file() and controller.media.get_media(item["id"])


def test_historical_jobs_do_not_probe_paths_while_listing_videos(application, tmp_path, monkeypatch):
    controller, bridge = application
    for index in range(12):
        registered(controller, tmp_path / "originals" / f"{index}.mp4", f"video-{index}".encode())
    # A long history can contain moved or disconnected sources. Such files
    # cannot be active, and resolving all history paths stalls every UI read.
    historical = [{"source_path": f"Z:/offline/{index}.mp4", "status": status}
        for index in range(240) for status in ("done", "failed", "cancelled")]
    monkeypatch.setattr(controller.queue, "list_jobs", lambda: historical)
    from engine import queue_store
    probe = Mock(side_effect=AssertionError("historical path must not be probed"))
    monkeypatch.setattr(queue_store, "_source_paths_equivalent", probe)
    result = bridge.get_downloaded_videos()
    assert result["ok"] and result["total"] == 12
    assert all(item["deletable"] for item in result["items"])
    probe.assert_not_called()
