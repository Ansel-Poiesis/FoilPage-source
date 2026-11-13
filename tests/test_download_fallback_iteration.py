# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""0.14 download operations remain one task across distinct channels."""
from pathlib import Path
from unittest.mock import Mock
import sys
import types

import pytest

from app.controller import AppController
from app.download_coordinator import DownloadCoordinator
from engine.downloader import DownloadAdapterError, DownloadCancelled, YtDlpAdapter
from engine.settings import SettingsStore
from app.task_notifications import TaskNotifications
from app.local_api import public_download_job


PAGE = "https://www.douyin.com/video/1234567890123456"
FIRST = "https://v3-web.douyinvod.com/video/tos/first.mp4?signature=one"
SECOND = "https://v26-web.douyinvod.com/video/tos/second.mp4?signature=two"
PLAY = "https://www.douyin.com/aweme/v1/play/?video_id=media123"


class Adapter:
    def download(self, url, destination, *, progress, **kwargs):
        path = destination / "resolved.mp4"
        path.write_bytes(b"resolved-media" * 10)
        progress(path.stat().st_size, path.stat().st_size)
        return path


@pytest.fixture
def coordinator(tmp_path):
    controller = AppController(SettingsStore(base_dir=tmp_path / "data"))
    worker = DownloadCoordinator(controller, adapter=Adapter())
    worker.download_dir.mkdir(parents=True, exist_ok=True)
    yield worker
    worker.stop()
    controller.close()


def failed_direct(*args, **kwargs):
    raise DownloadAdapterError("media_forbidden", "HTTP 403", retryable=False)


def test_resolver_failure_keeps_bounded_safe_browser_failure_history(coordinator):
    item = coordinator.submit(PAGE, mode="browser", media_url=SECOND,
                              options={"browser_fallback": True, "transcribe_after_download": False})
    for _ in range(12):
        coordinator.record_browser_download_failure(item["id"], browser_error="SERVER_FORBIDDEN",
            browser_detail="https://secret.invalid/media?signature=secret")
    coordinator.store.transition(item["id"], "resolving",
        options={**coordinator.store.get(item["id"])["options"], "mode": "local", "fallback_stage": "resolver"})
    coordinator._fail(item["id"], "douyin_verification_required", "需要页面校验", False)
    failed = coordinator.store.get(item["id"])
    history = failed["error"]["diagnostic"]["failure_history"]
    assert len(history) <= 8
    assert history[0]["browser_error"] == "SERVER_FORBIDDEN"
    assert history[-1]["code"] == "douyin_verification_required"
    assert "secret" not in str(history) and "https" not in str(history)


def test_successful_local_retry_clears_previous_network_error(coordinator, monkeypatch):
    monkeypatch.setattr("app.download_coordinator.active_vpn_tunnel", lambda: None)
    item = coordinator.submit("https://www.bilibili.com/video/BV1fixtureRetry",
                              options={"transcribe_after_download": False})
    coordinator._fail(item["id"], "network_error", "网络失败", True)
    coordinator.store.transition(item["id"], "resolving")
    coordinator._run_one(coordinator.store.get(item["id"]))
    completed = coordinator.store.get(item["id"])
    assert completed["status"] == "done"
    assert completed["error"] is None


def test_legacy_completed_download_does_not_show_its_stale_retry_error(coordinator):
    item = coordinator.submit("https://www.bilibili.com/video/BV1legacyRetry",
                              options={"transcribe_after_download": False})
    coordinator.store.transition(item["id"], "resolving")
    coordinator._run_one(item)
    coordinator.store.transition(item["id"], "done", error={"code":"network_error", "message":"旧网络错误"})
    row = next(x for x in coordinator.controller.task_rows() if x["task_id"] == item["id"])
    assert row["status"] == "done" and row["error"] is None
    assert coordinator.store.get(item["id"])["error"]["code"] == "network_error"


def test_browser_completion_uses_latest_transcription_intent_and_ignores_late_failures(coordinator, tmp_path):
    item = coordinator.submit(PAGE, mode="browser", media_url=SECOND,
        options={"browser_fallback": True, "transcribe_after_download": False})
    promoted = coordinator.submit(PAGE, options={"transcribe_after_download": True})
    assert promoted["id"] == item["id"]
    path = tmp_path / "browser-complete.mp4"
    path.write_bytes(b"fixture-media" * 20)
    imported = coordinator.submit_local_file(str(path), download_id=item["id"], transcribe=False)
    assert imported["job_id"]
    linked = coordinator.get(item["id"])
    assert linked["status"] == "queued"
    assert linked["error"] is None
    assert public_download_job(linked)["download_complete"] is True
    coordinator._fail(item["id"], "download_failed", "late local failure", False)
    reported = coordinator.record_extension_failure(url=PAGE, event_id="late-browser-report",
        phase="browser_download_start", channel="browser_extension", code="extension_error",
        message="late browser failure", task_id=item["id"])
    assert reported["status"] == "queued"
    assert coordinator.record_browser_download_failure(item["id"], browser_error="SERVER_FAILED")["status"] == "queued"
    assert coordinator.record_browser_download_failure(item["id"], browser_error="BROWSER_DOWNLOAD_HISTORY_MISSING")["status"] == "queued"
    repeated = coordinator.submit_local_file(str(path), download_id=item["id"], transcribe=False)
    assert repeated == imported
    assert len(coordinator.store.list()) == 1
    assert len(coordinator.controller.queue.list_jobs()) == 1


def test_all_channels_continue_one_task_without_intermediate_notification(coordinator, monkeypatch):
    monkeypatch.setattr("app.download_coordinator.download_direct_media", failed_direct)
    item = coordinator.submit(PAGE, media_url=FIRST, options={"browser_fallback": True, "transcribe_after_download": False})
    notify = Mock(return_value=True)
    monitor = TaskNotifications(notify)
    monitor.update([])
    coordinator._run_one(coordinator.store.claim_next())
    pending = coordinator.get(item["id"])
    assert pending["status"] == "awaiting_browser"
    assert pending["error"]["diagnostic"]["fallback_pending"] == "browser"
    assert monitor.update([{"task_id": pending["id"], "kind": "download", "status": pending["status"]}]) == []
    first_browser = coordinator.continue_fallback(item["id"], stage="browser", media_candidates=[SECOND, PLAY])
    assert first_browser["id"] == item["id"]
    assert first_browser["browser_download"]["download_url"] == SECOND
    duplicate = coordinator.continue_fallback(item["id"], stage="browser", media_candidates=[SECOND, PLAY])
    assert "browser_download" not in duplicate
    interrupted = coordinator.record_browser_download_failure(item["id"], browser_error="SERVER_FORBIDDEN")
    assert interrupted["status"] == "awaiting_browser"
    second_browser = coordinator.continue_fallback(item["id"], stage="browser", media_candidates=[SECOND, PLAY])
    assert second_browser["browser_download"]["download_url"] == PLAY
    coordinator.record_browser_download_failure(item["id"], browser_error="SERVER_FORBIDDEN")
    exhausted = coordinator.continue_fallback(item["id"], stage="browser", media_candidates=[SECOND, PLAY])
    assert exhausted["error"]["diagnostic"]["fallback_pending"] == "resolver"
    resolved = coordinator.continue_fallback(item["id"], stage="resolver", session_cookies=[{
        "name": "sessionid", "value": "test-session", "domain": ".douyin.com", "path": "/", "secure": True,
    }])
    assert resolved["id"] == item["id"]
    coordinator._run_one(coordinator.store.claim_next())
    final = coordinator.get(item["id"])
    assert final["status"] == "done"
    assert len(coordinator.store.list()) == 1
    assert public_download_job(final)["download_complete"] is True
    serialized = coordinator.store._conn.execute("SELECT options,error FROM download_jobs").fetchone()
    assert "signature=one" not in str(tuple(serialized))
    assert "signature=two" not in str(tuple(serialized))
    assert "test-session" not in str(tuple(serialized))
    assert monitor.update([{"task_id": final["id"], "kind": "download", "status": "done"}])[0]["title"] == "下载成功"
    assert notify.call_count == 1


def test_cancelled_handoff_never_resumes_or_queues_asr(coordinator, monkeypatch, tmp_path):
    monkeypatch.setattr("app.download_coordinator.download_direct_media", failed_direct)
    item = coordinator.submit(PAGE, media_url=FIRST, options={"browser_fallback": True})
    coordinator._run_one(coordinator.store.claim_next())
    coordinator.cancel(item["id"])
    assert coordinator.continue_fallback(item["id"], stage="resolver")["status"] == "cancelled"
    media = tmp_path / "cancelled.mp4"
    media.write_bytes(b"media")
    coordinator.submit_local_file(str(media), download_id=item["id"], transcribe=True)
    assert not coordinator.controller.queue.list_jobs()
    assert not coordinator.controller.media.list_media()


def test_permanent_browser_import_rejection_never_restarts_download(coordinator):
    item = coordinator.submit(PAGE, mode="browser", media_url=SECOND,
        options={"browser_fallback": True, "transcribe_after_download": False})
    rejected = coordinator.record_browser_download_failure(item["id"],
        browser_error="UNSUPPORTED_MEDIA_FORMAT", phase="browser_download_import")
    assert rejected["status"] == "failed"
    assert rejected["error"]["code"] == "unsupported_media_format"
    assert rejected["error"]["retryable"] is False
    assert rejected["error"]["diagnostic"]["browser_error"] == "UNSUPPORTED_MEDIA_FORMAT"
    assert "fallback_pending" not in rejected["error"]["diagnostic"]
    repeated = coordinator.record_browser_download_failure(item["id"],
        browser_error="UNSUPPORTED_MEDIA_FORMAT", phase="browser_download_import")
    assert repeated == rejected
    assert len(coordinator.store.list()) == 1
    assert not coordinator.controller.queue.list_jobs()


def test_confirmed_browser_history_missing_is_terminal_without_automatic_fallback(coordinator):
    item = coordinator.submit(PAGE, mode="browser", media_url=SECOND,
        options={"browser_fallback": True, "transcribe_after_download": False})
    missing = coordinator.record_browser_download_failure(item["id"], browser_error="BROWSER_DOWNLOAD_HISTORY_MISSING")
    assert missing["status"] == "failed"
    assert missing["error"]["code"] == "browser_download_history_missing"
    assert missing["error"]["diagnostic"]["browser_error"] == "BROWSER_DOWNLOAD_HISTORY_MISSING"
    assert "fallback_pending" not in missing["error"]["diagnostic"]
    assert "原视频页" in missing["error"]["message"]


def test_retry_is_active_and_does_not_emit_failure(coordinator):
    item = coordinator.submit("https://www.bilibili.com/video/BV1retry123")
    claimed = coordinator.store.claim_next()
    coordinator._fail(claimed["id"], "network_error", "temporary connection", True)
    assert coordinator.store.get(item["id"])["status"] == "received"
    assert len(coordinator.store.list()) == 1


def test_restart_recovers_same_download_task(coordinator):
    item = coordinator.submit("https://www.youtube.com/watch?v=restart123")
    coordinator.store.claim_next()
    coordinator.store.transition(item["id"], "downloading")
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(coordinator, "_loop", lambda: None)
        coordinator.start()
        assert coordinator.store.get(item["id"])["status"] == "received"
        assert len(coordinator.store.list()) == 1
    finally:
        monkey.undo()


def test_network_then_combined_rendition_are_distinct_channels(monkeypatch, tmp_path):
    calls = []
    class Client:
        def __init__(self, options): self.options = dict(options)
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def extract_info(self, _url, **kwargs):
            calls.append(self.options)
            if len(calls) == 1: raise TimeoutError("connection timed out")
            if len(calls) == 2: raise RuntimeError("Requested format is not available")
            path = Path(self.options["outtmpl"]).parent / "final.mp4"
            path.write_bytes(b"valid-media")
            return {"filepath": str(path)}
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=Client))
    monkeypatch.setattr("engine.downloader.urllib.request.getproxies", lambda: {"https": "http://127.0.0.1:9"})
    final = YtDlpAdapter().download("https://www.youtube.com/watch?v=channels123", tmp_path,
        audio_only=False, rate_limit_bps=None, progress=lambda *_: None, cancel_check=lambda: False)
    assert final.is_file()
    assert len(calls) == 3
    assert "proxy" not in calls[0]
    assert calls[1]["proxy"] == ""
    assert calls[2]["format"] == "best[ext=mp4]/best"


def test_authorization_denial_never_tries_other_network_routes(monkeypatch, tmp_path):
    calls=[]
    class Client:
        def __init__(self, options): calls.append(dict(options))
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def extract_info(self, *_args, **_kwargs): raise RuntimeError("Sign in to confirm your age")
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=Client))
    monkeypatch.setattr("engine.downloader.urllib.request.getproxies", lambda: {"https":"http://127.0.0.1:9"})
    with pytest.raises(DownloadAdapterError):
        YtDlpAdapter().download("https://www.youtube.com/watch?v=restricted123",tmp_path,
            audio_only=False,rate_limit_bps=None,progress=lambda *_:None,cancel_check=lambda:False)
    assert len(calls)==1


def test_chrome_complete_error_document_never_becomes_download_success(coordinator, tmp_path):
    item = coordinator.submit(PAGE, mode="browser", media_url=SECOND, options={"browser_fallback": True, "transcribe_after_download": False})
    document = tmp_path / "not-video.mp4"
    document.write_text("<html><body>Access denied</body></html>",encoding="utf-8")
    with pytest.raises(DownloadAdapterError, match="不是可播放媒体"):
        coordinator.submit_local_file(str(document),download_id=item["id"],validate_media=True,transcribe=False)
    current = coordinator.get(item["id"])
    assert current["status"] == "awaiting_browser"
    assert not public_download_job(current)["download_complete"]
    assert current["error"]["diagnostic"]["fallback_pending"] == "browser"
    assert not coordinator.controller.media.list_media()


@pytest.mark.parametrize("direct_succeeds", [True, False])
def test_bilibili_proxy_412_tries_direct_once(monkeypatch, tmp_path, direct_succeeds):
    # system 策略下保留「首次走系统代理 → 412 → 直连救援一次」的既有行为；
    # 默认 auto 已对 B 站直连优先，无需救援（见下方 direct-first 用例）。
    calls = []
    class Client:
        def __init__(self, options): self.options = dict(options)
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def extract_info(self, _url, **_kwargs):
            calls.append(self.options)
            if len(calls) == 1 or not direct_succeeds:
                raise RuntimeError("HTTP Error 412: Precondition Failed")
            path = Path(self.options["outtmpl"]).parent / "direct.mp4"
            path.write_bytes(b"fixture-media")
            return {"filepath": str(path)}
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=Client))
    monkeypatch.setattr("engine.downloader.urllib.request.getproxies", lambda: {"https": "http://127.0.0.1:9"})
    arguments = dict(audio_only=False, rate_limit_bps=None, progress=lambda *_: None, cancel_check=lambda: False, proxy_policy="system")
    if direct_succeeds:
        result = YtDlpAdapter().download("https://www.bilibili.com/video/BV1direct123", tmp_path, **arguments)
        assert result.read_bytes() == b"fixture-media"
    else:
        with pytest.raises(DownloadAdapterError) as caught:
            YtDlpAdapter().download("https://www.bilibili.com/video/BV1direct123", tmp_path, **arguments)
        assert caught.value.code == "source_rate_limited"
    assert len(calls) == 2, "a failed direct attempt must not repeat"
    assert "proxy" not in calls[0]
    assert calls[1]["proxy"] == ""
    assert calls[1]["http_headers"] == calls[0]["http_headers"]
    assert all("cookiefile" not in call for call in calls)


@pytest.mark.parametrize("url,proxies", [
    ("https://www.bilibili.com/video/BV1direct123", {}),
    ("https://www.youtube.com/watch?v=direct123", {"https": "http://127.0.0.1:9"}),
])
def test_412_direct_fallback_requires_bilibili_and_existing_proxy(monkeypatch, tmp_path, url, proxies):
    calls = []
    class Client:
        def __init__(self, options): calls.append(dict(options))
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def extract_info(self, *_args, **_kwargs):
            raise RuntimeError("HTTP Error 412: Precondition Failed")
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=Client))
    monkeypatch.setattr("engine.downloader.urllib.request.getproxies", lambda: proxies)
    with pytest.raises(DownloadAdapterError):
        YtDlpAdapter().download(url, tmp_path, audio_only=False, rate_limit_bps=None,
            progress=lambda *_: None, cancel_check=lambda: False)
    assert len(calls) == 1


def test_cancel_after_bilibili_proxy_412_prevents_direct_request(monkeypatch, tmp_path):
    calls = []
    cancelled = False
    class Client:
        def __init__(self, options): calls.append(dict(options))
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def extract_info(self, *_args, **_kwargs):
            nonlocal cancelled
            cancelled = True
            raise RuntimeError("HTTP Error 412: Precondition Failed")
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=Client))
    monkeypatch.setattr("engine.downloader.urllib.request.getproxies", lambda: {"https": "http://127.0.0.1:9"})
    with pytest.raises(DownloadCancelled):
        YtDlpAdapter().download("https://www.bilibili.com/video/BV1direct123", tmp_path,
            audio_only=False, rate_limit_bps=None, progress=lambda *_: None, cancel_check=lambda: cancelled,
            proxy_policy="system")
    assert len(calls) == 1


def test_bilibili_412_auto_policy_is_direct_first(monkeypatch, tmp_path):
    # auto（默认）下 B 站第一跳就是直连：412 直接定性，不再有「转直连」二跳。
    calls = []
    class Client:
        def __init__(self, options): calls.append(dict(options))
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def extract_info(self, *_args, **_kwargs):
            raise RuntimeError("HTTP Error 412: Precondition Failed")
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=Client))
    monkeypatch.setattr("engine.downloader.urllib.request.getproxies", lambda: {"https": "http://127.0.0.1:9"})
    with pytest.raises(DownloadAdapterError) as caught:
        YtDlpAdapter().download("https://www.bilibili.com/video/BV1direct123", tmp_path,
            audio_only=False, rate_limit_bps=None, progress=lambda *_: None, cancel_check=lambda: False)
    assert caught.value.code == "source_rate_limited"
    assert len(calls) == 1
    assert calls[0]["proxy"] == ""


def test_receiptless_failure_report_merges_into_live_task(coordinator):
    """一次点击丢回执后的补错上报不得新建独立失败行（1.3.10）。"""
    item = coordinator.submit(PAGE, mode="browser", media_url=SECOND,
        options={"browser_fallback": True, "transcribe_after_download": False})
    reported = coordinator.record_extension_failure(url=PAGE, event_id="lost-receipt-1",
        phase="local_api_submit", channel="browser_extension", code="local_api_timeout",
        message="submit receipt lost", task_id="")
    assert reported["id"] == item["id"]
    assert reported["status"] in {"awaiting_browser", "queued"}
    assert len(coordinator.store.list()) == 1


def test_receiptless_failure_report_appends_to_failed_task_for_same_url(coordinator):
    item = coordinator.submit(PAGE, mode="browser", media_url=SECOND,
        options={"browser_fallback": True, "transcribe_after_download": False})
    terminal = coordinator.record_browser_download_failure(item["id"],
        browser_error="UNSUPPORTED_MEDIA_FORMAT", phase="browser_download_import")
    assert terminal["status"] == "failed"
    reported = coordinator.record_extension_failure(url=PAGE, event_id="late-report-2",
        phase="browser_download_start", channel="browser_extension", code="extension_error",
        message="late failure after terminal", task_id="")
    assert reported["id"] == item["id"]
    assert reported["status"] == "failed"
    incidents = reported["error"]["incident_reports"]
    assert incidents and incidents[-1]["diagnostic"]["event_id"] == "late-report-2"
    assert len(coordinator.store.list()) == 1


def test_receiptless_failure_report_for_unseen_url_keeps_one_record(coordinator):
    reported = coordinator.record_extension_failure(url="https://www.bilibili.com/video/BV1fresh456",
        event_id="fresh-url-1", phase="local_api_submit", channel="browser_extension",
        code="local_api_unavailable", message="desktop unreachable", task_id="")
    assert reported["status"] == "failed"
    assert len(coordinator.store.list()) == 1


def test_late_browser_completion_resurrects_terminally_failed_task(coordinator, tmp_path):
    """兜底链全部判死后，Chrome 原传输自动恢复完成的文件要能复活同一任务。"""
    item = coordinator.submit(PAGE, mode="browser", media_url=SECOND,
        options={"browser_fallback": True, "transcribe_after_download": False})
    terminal = coordinator.record_browser_download_failure(item["id"],
        browser_error="UNSUPPORTED_MEDIA_FORMAT", phase="browser_download_import")
    assert terminal["status"] == "failed"
    media = tmp_path / "late-resumed.mp4"
    media.write_bytes(b"late-media" * 8)
    revived = coordinator.submit_local_file(str(media), download_id=item["id"], transcribe=False)
    linked = coordinator.get(item["id"])
    assert linked["status"] == "done"
    assert linked["downloaded_path"]
    assert linked["error"] is None
    assert public_download_job(linked)["download_complete"] is True
    assert revived["media_id"]
    assert len(coordinator.store.list()) == 1
