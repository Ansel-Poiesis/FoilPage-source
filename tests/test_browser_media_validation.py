# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Browser receipts must work in the slim Desktop without PyAV."""
import shutil
import sys
import subprocess
import wave
from pathlib import Path

import pytest

from app.controller import AppController
from app.download_coordinator import DownloadCoordinator
from engine.downloader import DownloadAdapterError
from engine.contracts import EngineError
from engine.settings import SettingsStore
from engine import audio


@pytest.fixture
def coordinator(tmp_path):
    controller = AppController(SettingsStore(base_dir=tmp_path / "data"))
    worker = DownloadCoordinator(controller)
    yield worker
    worker.stop()
    controller.close()


def browser_job(coordinator):
    return coordinator.submit("https://www.douyin.com/video/1234567890123456",
        mode="browser", media_url="https://v26.douyinvod.com/fixture.mp4",
        options={"browser_fallback": True, "transcribe_after_download": False})


def test_slim_desktop_accepts_real_browser_video_without_av(coordinator, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "av", None)
    source = tmp_path / "browser.mp4"
    shutil.copyfile(Path(__file__).parents[1] / "assets/sample.mp4", source)
    row = browser_job(coordinator)
    result = coordinator.submit_local_file(str(source), download_id=row["id"],
        transcribe=False, validate_media=True)
    assert coordinator.get(row["id"])["status"] == "done"
    assert Path(result["path"]).read_bytes() == source.read_bytes()
    assert result["job_id"] is None and coordinator.controller.queue.list_jobs() == []
    assert coordinator.get(row["id"])["error"] is None


def test_slim_desktop_rejects_html_disguised_as_video(coordinator, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "av", None)
    source = tmp_path / "browser.mp4"
    source.write_text("<html>access denied</html>", encoding="utf-8")
    row = browser_job(coordinator)
    with pytest.raises(DownloadAdapterError) as caught:
        coordinator.submit_local_file(str(source), download_id=row["id"], validate_media=True)
    assert caught.value.code == "media_not_video"
    current = coordinator.get(row["id"])
    assert current["error"]["diagnostic"]["browser_error"] == "SERVER_BAD_CONTENT"
    assert current["downloaded_path"] is None
    assert coordinator.controller.media.list_media() == []
    assert source.is_file()


def test_slim_desktop_accepts_audio_stream_without_av(coordinator, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "av", None)
    source = tmp_path / "browser.wav"
    with wave.open(str(source), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x00\x00" * 1600)
    row = browser_job(coordinator)
    result = coordinator.submit_local_file(str(source), download_id=row["id"],
        transcribe=False, validate_media=True)
    assert coordinator.get(row["id"])["status"] == "done"
    assert result["job_id"] is None
    assert coordinator.controller.media.get_media(result["media_id"])["media_type"] == "audio"


@pytest.mark.parametrize("failure,code", [
    (subprocess.TimeoutExpired("ffmpeg", 15), "media_validation_timeout"),
    (OSError("component could not start"), "media_validation_unavailable"),
])
def test_probe_distinguishes_local_runtime_failures(monkeypatch, failure, code):
    monkeypatch.setattr(audio, "get_ffmpeg", lambda *_: "ffmpeg")
    def unavailable(*_, **__):
        raise failure
    monkeypatch.setattr(audio.subprocess, "run", unavailable)
    with pytest.raises(EngineError) as caught:
        audio.validate_media_streams("browser.mp4")
    assert caught.value.code == code


@pytest.mark.parametrize("code", ["ffmpeg_missing", "media_validation_timeout", "media_validation_unavailable"])
def test_local_validation_failure_preserves_transfer_without_cdn_fallback(coordinator, tmp_path, monkeypatch, code):
    source = tmp_path / "browser.mp4"
    shutil.copyfile(Path(__file__).parents[1] / "assets/sample.mp4", source)
    row = browser_job(coordinator)
    def unavailable(*_):
        raise EngineError(code, "local validation unavailable")
    monkeypatch.setattr("app.download_coordinator.validate_media_streams", unavailable)
    with pytest.raises(DownloadAdapterError) as caught:
        coordinator.submit_local_file(str(source), download_id=row["id"], validate_media=True)
    assert caught.value.code == code
    current = coordinator.get(row["id"])
    assert current["status"] == "awaiting_browser"
    assert current["error"]["code"] == code
    assert "fallback_pending" not in current["error"]["diagnostic"]
    assert len(current["options"]["browser_attempted"]) == 1
    assert current["downloaded_path"] is None and source.is_file()
    assert coordinator.controller.media.list_media() == []
