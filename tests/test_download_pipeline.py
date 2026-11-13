# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Downloader validation and browser-to-ASR orchestration tests."""
from __future__ import annotations

import errno
import json
import sqlite3
import sys
import tempfile
import time
import types
import urllib.error
from io import BytesIO, StringIO
from pathlib import Path

import pytest

from app.controller import AppController
from app.download_coordinator import DownloadCoordinator
from engine.downloader import (
    DownloadAdapterError, YtDlpAdapter, detect_platform,
    download_direct_media, is_browser_download_url, is_direct_media_url,
    is_hls_media_url, is_play_endpoint_url, HlsRedirectError,
    normalized_download_error, sanitize_download_filename,
    sanitize_douyin_session_cookies,
    validate_supported_url,
)
from engine.settings import SettingsStore


class FakeAdapter:
    def __init__(self, filename: str = "browser-source.mp4") -> None:
        self.rate_limits: list[int | None] = []
        self.session_cookies: list[tuple[dict[str, object], ...]] = []
        self.headers: list[tuple[str, str]] = []
        self.with_cover: list[bool] = []
        self.filename = filename
        self.resolve_calls: list[tuple[str, str, str, tuple[dict[str, object], ...]]] = []
        self.resolve_url = "https://v3-web.douyinvod.com/video/tos/example.mp4?token=x"
        self.resolve_error: DownloadAdapterError | None = None

    def resolve(
        self, url, *, referer="", user_agent="", session_cookies=None,
        js_runtime_path="", proxy_policy="auto", proxy_url="",
    ) -> dict[str, str]:
        cookies = tuple(dict(item) for item in (session_cookies or ()))
        self.resolve_calls.append((url, referer, user_agent, cookies))
        if self.resolve_error:
            raise self.resolve_error
        return {
            "download_url": self.resolve_url,
            "filename": "browser-resolved.mp4",
            "headers": {"Referer": referer, "User-Agent": user_agent, "Accept": "video/*"},
        }

    def download(
        self, _url, destination, *, audio_only, rate_limit_bps, referer="", user_agent="",
        session_cookies=None, progress, cancel_check, js_runtime_path="",
        proxy_policy="auto", proxy_url="", with_cover=False,
    ):
        assert not audio_only and not cancel_check()
        self.rate_limits.append(rate_limit_bps)
        self.session_cookies.append(tuple(dict(item) for item in (session_cookies or ())))
        self.headers.append((referer, user_agent))
        self.with_cover.append(bool(with_cover))
        target = Path(destination) / self.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        progress(4, 8)
        target.write_bytes((self.filename or "browser-source.mp4").encode("utf-8") * 10)
        progress(10, 10)
        return target


def wait_for(coordinator: DownloadCoordinator, download_id: str, statuses: set[str]):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        item = coordinator.get(download_id)
        if item and item["status"] in statuses:
            return item
        time.sleep(0.02)
    raise AssertionError(f"下载任务未进入预期状态: {coordinator.get(download_id)}")


@pytest.mark.parametrize("url,platform", [
    ("https://www.bilibili.com/video/BV1x", "bilibili"),
    ("https://v.douyin.com/abc", "douyin"),
    ("https://v3-web.douyinvod.com/video/tos/example.mp4?token=short", "douyin"),
    ("https://youtu.be/abc", "youtube"),
])
def test_supported_url_allowlist(url: str, platform: str) -> None:
    assert validate_supported_url(url) == url
    assert detect_platform(url) == platform


@pytest.mark.parametrize("url", [
    "file:///C:/secret.mp4",
    "http://youtube.com.evil.example/video",
    "https://user:pass@youtube.com/watch?v=x",
    "javascript:alert(1)",
    "https://example.com/video.mp4",
])
def test_unsupported_or_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(DownloadAdapterError):
        validate_supported_url(url)


def test_platform_auth_and_invalid_page_errors_are_not_marked_retryable() -> None:
    auth = normalized_download_error(RuntimeError("Fresh cookies are needed"))
    assert auth.code == "authorization_required" and auth.retryable is False
    assert "设置 - 浏览器" in auth.message

    rejected = normalized_download_error(
        RuntimeError("Fresh cookies (not necessarily logged in) are needed"),
        browser_session_supplied=True, platform="douyin",
    )
    assert rejected.code == "douyin_verification_required"
    assert "不代表账号一定已退出" in rejected.message
    assert "Fresh cookies" in rejected.message

    invalid = normalized_download_error(RuntimeError("Unsupported URL: https://example"))
    assert invalid.code == "unsupported_source" and invalid.retryable is False
    assert "视频详情链接" in invalid.message

    invalid_filename = normalized_download_error(OSError(22, "Invalid argument"))
    assert invalid_filename.code == "invalid_filename"
    assert invalid_filename.retryable is True
    assert "Windows" in invalid_filename.message
    assert "请重试任务" in invalid_filename.message

    bili_auth = normalized_download_error(RuntimeError("This video is only available for registered users"))
    assert bili_auth.code == "authorization_required"
    assert bili_auth.retryable is False
    assert "登录" in bili_auth.message

    rate_limited = normalized_download_error(RuntimeError("HTTP Error 412: Precondition Failed"))
    assert rate_limited.code == "source_rate_limited"
    assert rate_limited.retryable is True


@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    TimeoutError(),
    ConnectionResetError("Connection reset by peer"),
    ConnectionRefusedError("Connection refused"),
])
def test_network_like_exceptions_become_retryable_network_error(exc: Exception) -> None:
    """Flaky network must not surface as a Chrome-authorization failure.

    This was the bug behind the misleading "当前 Chrome 的抖音登录态未被平台接受"
    message: a dropped connection / timeout during Douyin resolve was bucketed
    into authorization_required (retryable=False) purely by message text.
    """
    err = normalized_download_error(exc, browser_session_supplied=True)
    assert err.code == "network_error"
    assert err.retryable is True
    assert "网络" in err.message


@pytest.mark.parametrize("message", [
    "HTTPSConnectionPool timed out",
    "Connection reset by peer",
    "<urlopen error [Errno 110] Connection timed out>",
    "HTTP Error 502: Bad Gateway",
    "Temporary failure in name resolution",
])
def test_network_like_messages_become_retryable_network_error(message: str) -> None:
    err = normalized_download_error(RuntimeError(message), browser_session_supplied=True)
    assert err.code == "network_error"
    assert err.retryable is True
    assert "网络" in err.message


def test_authorization_messages_are_not_swallowed_by_network_detection() -> None:
    """403 (anti-bot) and explicit login walls must keep their own semantics."""
    auth = normalized_download_error(
        RuntimeError("Fresh cookies (not necessarily logged in) are needed"),
        browser_session_supplied=True, platform="douyin",
    )
    assert auth.code == "douyin_verification_required"
    assert auth.retryable is False
    assert "不代表账号一定已退出" in auth.message

    registered = normalized_download_error(
        RuntimeError("This video is only available for registered users"),
    )
    assert registered.code == "authorization_required"
    assert registered.retryable is False


@pytest.mark.parametrize("url, expected", [
    ("https://www.bilibili.com/video/BV1format", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"),
    ("https://www.douyin.com/video/1234567890", "best[ext=mp4]/best"),
])
def test_yt_dlp_format_selector_keeps_platform_contract(url: str, expected: str) -> None:
    assert YtDlpAdapter._format_selector(url, audio_only=False) == expected
    assert YtDlpAdapter._format_selector(url, audio_only=True) == "bestaudio/best"


def test_fake_download_imports_media_and_queues_asr() -> None:
    with tempfile.TemporaryDirectory(prefix="download_pipeline_") as temp:
        settings_store = SettingsStore(base_dir=Path(temp) / "data")
        settings_store.update(download_rate_limit_kib=512)
        controller = AppController(settings_store)
        adapter = FakeAdapter()
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            created = coordinator.submit(
                "https://www.bilibili.com/video/BV1pipeline", title="链路测试",
                options={"transcribe_after_download": True},
            )
            linked = wait_for(coordinator, created["id"], {"queued", "failed"})
            assert linked["status"] == "queued"
            assert linked["progress"] == 1.0
            assert linked["media_id"] and linked["job_id"]
            assert adapter.rate_limits == [512 * 1024]
            media = controller.media.get_media(linked["media_id"])
            assert media["filename"] == "browser-source.mp4"
            assert controller.queue.get_job(linked["job_id"])["status"] == "queued"

            claimed = controller.queue.claim_next("test-worker")
            assert claimed and claimed["id"] == linked["job_id"]
            controller.queue.update_status(
                linked["job_id"], "retryable",
                {"code": "model_load_failed", "message": "模型暂不可用"},
                worker_id="test-worker",
            )
            refreshed = coordinator.get(created["id"])
            assert refreshed and refreshed["status"] == "queued"
            assert refreshed["downloaded_path"] == linked["downloaded_path"]
            assert refreshed["error"] is None
        finally:
            coordinator.stop()
            controller.close()


def test_transcribe_click_reuses_downloaded_media_without_redownload() -> None:
    """先生 2026-10-08：下载完成后点转写，不得重复下载——复用既有媒体提升转写。"""
    with tempfile.TemporaryDirectory(prefix="download_reuse_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            first = coordinator.submit(
                "https://www.bilibili.com/video/BV1pipeline", title="复用测试",
                options={"transcribe_after_download": False},
            )
            done = wait_for(coordinator, first["id"], {"done", "failed"})
            assert done["status"] == "done"
            assert done["downloaded_path"]

            second = coordinator.submit(
                "https://www.bilibili.com/video/BV1pipeline", title="复用测试",
                options={"transcribe_after_download": True},
            )
            assert second["id"] == first["id"], "同视频转写必须复用既有任务行"
            assert second["status"] == "queued"
            assert second["job_id"], "已挂上转写队列任务"
            assert second["options"]["transcribe_after_download"] is True
            rows = [j for j in coordinator.list(limit=50) if j["url"] == first["url"]]
            assert len(rows) == 1, "不得新建第二行下载任务（复用提升，媒体不二次下载）"
        finally:
            coordinator.stop()
            controller.close()


def test_transcribe_click_flags_active_download_for_transcription() -> None:
    """下载进行中点转写：就地为活跃任务补置转写标志，不建第二行。"""
    with tempfile.TemporaryDirectory(prefix="download_flag_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        url = "https://www.douyin.com/video/7673838983419530149"
        item = controller.downloads.create(
            url, "进行中", "douyin",
            {"transcribe_after_download": False},
        )
        controller.downloads.transition(item["id"], "resolving")
        coordinator = DownloadCoordinator(controller, adapter=FakeAdapter())
        merged = coordinator.submit(url, options={"transcribe_after_download": True})
        assert merged["id"] == item["id"]
        assert merged["options"]["transcribe_after_download"] is True
        rows = [j for j in coordinator.list(limit=50) if j["url"] == url]
        assert len(rows) == 1
        controller.close()


def test_authorized_douyin_session_is_transient_and_never_persisted() -> None:
    with tempfile.TemporaryDirectory(prefix="browser_session_transient_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            session = [{
                "name": "sessionid", "value": "session-secret", "domain": ".douyin.com",
                "path": "/", "secure": True,
            }]
            created = coordinator.submit(
                "https://www.douyin.com/video/1234567890", session_cookies=session,
                options={"transcribe_after_download": True},
            )
            assert "session-secret" not in json.dumps(created, ensure_ascii=False)
            queued = wait_for(coordinator, created["id"], {"queued", "failed"})
            assert queued["status"] == "queued"
            assert adapter.session_cookies and adapter.session_cookies[0][0]["value"] == "session-secret"
            assert coordinator._transient_session(created["id"]) == ()
        finally:
            coordinator.stop()
            controller.close()


def test_douyin_local_download_prefers_captured_media_hint(monkeypatch) -> None:
    with tempfile.TemporaryDirectory(prefix="douyin_media_hint_local_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        captured: dict[str, object] = {}

        def fake_direct(url, destination, **kwargs):
            captured["url"] = url
            captured["cookies"] = tuple(dict(item) for item in (kwargs.get("session_cookies") or ()))
            target = Path(destination) / "captured-douyin.mp4"
            kwargs["progress"](4, 8)
            target.write_bytes(b"captured-media")
            kwargs["progress"](target.stat().st_size, target.stat().st_size)
            return target

        monkeypatch.setattr("app.download_coordinator.download_direct_media", fake_direct)
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            session = [{
                "name": "sessionid", "value": "session-secret", "domain": ".douyin.com",
                "path": "/", "secure": True,
            }]
            created = coordinator.submit(
                "https://www.douyin.com/video/1234567890",
                title="抖音播放器兜底",
                options={"transcribe_after_download": False},
                session_cookies=session,
                media_url="https://v3-web.douyinvod.com/video/tos/example.mp4?token=hint",
            )
            done = wait_for(coordinator, created["id"], {"done", "failed"})
            assert done["status"] == "done"
            assert captured["url"].startswith("https://v3-web.douyinvod.com/")
            assert captured["cookies"][0]["value"] == "session-secret"
            assert adapter.session_cookies == []
        finally:
            coordinator.stop()
            controller.close()


def test_douyin_hls_media_hint_routes_to_ytdlp_not_direct(monkeypatch) -> None:
    """抖音 play_addr 已转向 HLS 主清单：捕获的 .m3u8 必须走 yt-dlp 分片下载，
    绝不能进单文件直下（否则只会把播放列表文本存成假 MP4）。"""
    with tempfile.TemporaryDirectory(prefix="douyin_hls_hint_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()

        def fake_direct(url, destination, **kwargs):
            raise AssertionError(f"m3u8 主清单不允许进入单文件直下: {url}")

        monkeypatch.setattr("app.download_coordinator.download_direct_media", fake_direct)

        hls_url = "https://v3-web.douyinvod.com/video/tos/cn/tos-cn-ve-15/index.m3u8?sign=abc"
        download_urls: list[str] = []
        original_download = adapter.download

        def recording_download(url, *args, **kwargs):
            download_urls.append(str(url))
            return original_download(url, *args, **kwargs)

        adapter.download = recording_download

        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            created = coordinator.submit(
                "https://www.douyin.com/video/1234567890",
                title="抖音HLS主清单",
                options={"transcribe_after_download": False},
                media_url=hls_url,
            )
            done = wait_for(coordinator, created["id"], {"done", "failed"})
            assert done["status"] == "done"
            assert download_urls == [hls_url]
        finally:
            coordinator.stop()
            controller.close()


def test_douyin_play_endpoint_without_cookies_fails_fast_for_browser_fallback(monkeypatch) -> None:
    """/aweme/v1/play/ 是 Cookie 门禁：无会话时必须立刻 media_forbidden，
    让扩展 chrome.downloads 回退接管，而不是本地裸 GET 撞 403 三次。"""
    with tempfile.TemporaryDirectory(prefix="douyin_play_endpoint_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()

        def fake_direct(url, destination, **kwargs):
            raise AssertionError(f"无 Cookie 的 play 端点不应进入本地直下: {url}")

        monkeypatch.setattr("app.download_coordinator.download_direct_media", fake_direct)
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            created = coordinator.submit(
                "https://www.douyin.com/video/1234567890",
                title="抖音play端点",
                options={"transcribe_after_download": False},
                media_url="https://www.douyin.com/aweme/v1/play/?video_id=v0200&target=1234567890",
            )
            failed = wait_for(coordinator, created["id"], {"done", "failed"})
            assert failed["status"] == "failed"
            error = json.loads(failed["error"]) if isinstance(failed["error"], str) else failed["error"]
            assert error["code"] == "media_forbidden"
            assert error["retryable"] is False
            assert adapter.rate_limits == []
        finally:
            coordinator.stop()
            controller.close()


def test_douyin_play_endpoint_with_cookies_still_uses_direct_download(monkeypatch) -> None:
    """带浏览器会话的 play 端点仍可本地直下（Cookie 随请求带出）。"""
    with tempfile.TemporaryDirectory(prefix="douyin_play_endpoint_auth_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        captured: dict[str, object] = {}

        def fake_direct(url, destination, **kwargs):
            captured["url"] = url
            captured["cookies"] = tuple(dict(item) for item in (kwargs.get("session_cookies") or ()))
            target = Path(destination) / "play-auth.mp4"
            kwargs["progress"](2, 4)
            target.write_bytes(b"play-auth")
            kwargs["progress"](target.stat().st_size, target.stat().st_size)
            return target

        monkeypatch.setattr("app.download_coordinator.download_direct_media", fake_direct)
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            created = coordinator.submit(
                "https://www.douyin.com/video/1234567890",
                title="抖音play端点带会话",
                options={"transcribe_after_download": False},
                session_cookies=[{
                    "name": "sessionid", "value": "session-secret", "domain": ".douyin.com",
                    "path": "/", "secure": True,
                }],
                media_url="https://www.douyin.com/aweme/v1/play/?video_id=v0200&target=1234567890",
            )
            done = wait_for(coordinator, created["id"], {"done", "failed"})
            assert done["status"] == "done"
            assert "aweme/v1/play" in captured["url"]
            assert captured["cookies"] and captured["cookies"][0]["value"] == "session-secret"
        finally:
            coordinator.stop()
            controller.close()


def test_douyin_direct_403_fails_fast_as_media_forbidden(monkeypatch) -> None:
    """捕获直链 403（过期签名）：不得自动重试同 URL，应 media_forbidden
    快速收束，扩展才能立刻改走浏览器下载通道。"""
    with tempfile.TemporaryDirectory(prefix="douyin_403_fail_fast_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        attempts: list[str] = []

        def fake_direct(url, destination, **kwargs):
            attempts.append(url)
            raise DownloadAdapterError(
                "media_forbidden",
                "播放地址被拒绝（HTTP 403），可能已过期或需要浏览器登录态",
                retryable=False,
            )

        monkeypatch.setattr("app.download_coordinator.download_direct_media", fake_direct)
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            created = coordinator.submit(
                "https://www.douyin.com/video/1234567890",
                title="抖音直链过期",
                options={"transcribe_after_download": False},
                media_url="https://v3-web.douyinvod.com/video/tos/example.mp4?token=expired",
            )
            failed = wait_for(coordinator, created["id"], {"done", "failed"})
            assert failed["status"] == "failed"
            error = json.loads(failed["error"]) if isinstance(failed["error"], str) else failed["error"]
            assert error["code"] == "media_forbidden"
            assert error["retryable"] is False
            # 同一 URL 只撞一次，不做外层自动重试
            assert attempts == [
                "https://v3-web.douyinvod.com/video/tos/example.mp4?token=expired",
            ]
        finally:
            coordinator.stop()
            controller.close()


def test_douyin_candidate_pool_moves_to_next_signed_url_after_403(monkeypatch) -> None:
    """A rejected first CDN URL must not end the click when another captured
    rendition/manifest is available; candidate URLs stay out of the job row."""
    with tempfile.TemporaryDirectory(prefix="douyin_candidate_fallback_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        first = "https://v3-web.douyinvod.com/video/tos/expired.mp4?token=first"
        second = "https://v26-web.douyinvod.com/video/tos/backup.mp4?token=second"
        attempts: list[str] = []

        def fake_direct(url, destination, **kwargs):
            attempts.append(url)
            if url == first:
                raise DownloadAdapterError(
                    "media_forbidden", "媒体服务器拒绝签名地址（HTTP 403）", retryable=False,
                )
            target = Path(destination) / "backup.mp4"
            target.write_bytes(b"captured-backup")
            kwargs["progress"](target.stat().st_size, target.stat().st_size)
            return target

        monkeypatch.setattr("app.download_coordinator.download_direct_media", fake_direct)
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            created = coordinator.submit(
                "https://www.douyin.com/video/1234567890",
                title="抖音候选地址回退",
                options={"transcribe_after_download": False},
                media_url=first,
                media_candidates=[first, second],
            )
            done = wait_for(coordinator, created["id"], {"done", "failed"})
            assert done["status"] == "done"
            assert attempts == [first, second]
            assert first not in json.dumps(created["options"], ensure_ascii=False)
            assert second not in json.dumps(created["options"], ensure_ascii=False)
        finally:
            coordinator.stop()
            controller.close()


def test_play_endpoint_url_classifier() -> None:
    assert is_play_endpoint_url("https://www.douyin.com/aweme/v1/play/?video_id=1&target=2")
    assert is_play_endpoint_url("https://www.douyin.com/video/play/?video_id=1")
    assert not is_play_endpoint_url("https://v3-web.douyinvod.com/video/tos/example.mp4?token=x")
    assert not is_play_endpoint_url("https://v3-web.douyinvod.com/video/tos/index.m3u8?sign=x")
    assert not is_play_endpoint_url("https://www.douyin.com/video/1234567890")


def test_direct_media_403_raises_media_forbidden_without_retry(monkeypatch) -> None:
    """download_direct_media 对 401/403/410 立刻 media_forbidden，
    不把同一签名地址再打 attempts-1 次。"""
    monkeypatch.setattr("engine.downloader._NO_PROXY_OPENER", None)
    calls: list[str] = []

    def forbidden_urlopen(request, timeout):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("engine.downloader.urllib.request.urlopen", forbidden_urlopen)
    with tempfile.TemporaryDirectory(prefix="direct_media_403_") as temp:
        with pytest.raises(DownloadAdapterError) as exc_info:
            download_direct_media(
                "https://v3-web.douyinvod.com/video/tos/example.mp4?token=x",
                Path(temp),
                referer="https://www.douyin.com/video/123",
                user_agent="",
                rate_limit_bps=None,
                progress=lambda *_args: None,
                cancel_check=lambda: False,
                attempts=3,
            )
        assert exc_info.value.code == "media_forbidden"
        assert exc_info.value.retryable is False
        assert len(calls) == 1


def test_direct_media_only_sends_douyin_cookies_to_matching_hosts(monkeypatch) -> None:
    monkeypatch.setattr("engine.downloader._NO_PROXY_OPENER", None)
    requests = []

    def forbidden_urlopen(request, timeout):
        requests.append(request)
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("engine.downloader.urllib.request.urlopen", forbidden_urlopen)
    cookie = {
        "name": "sessionid", "value": "local-only-test", "domain": ".douyin.com",
        "path": "/", "secure": True,
    }
    with tempfile.TemporaryDirectory(prefix="direct_media_cookie_scope_") as temp:
        for url in (
            "https://www.douyin.com/aweme/v1/play/?video_id=1",
            "https://v3-web.douyinvod.com/video/tos/example.mp4?token=x",
        ):
            with pytest.raises(DownloadAdapterError):
                download_direct_media(
                    url, Path(temp), session_cookies=[cookie], rate_limit_bps=None,
                    progress=lambda *_args: None, cancel_check=lambda: False,
                )
    assert requests[0].get_header("Cookie") == "sessionid=local-only-test"
    assert requests[1].get_header("Cookie") is None


def test_douyin_play_redirect_to_hls_falls_back_to_ytdlp(monkeypatch) -> None:
    """签名播放地址（/aweme/v1/play/）302 到 HLS 主清单：直下抛 hls_redirect，
    协调器携最终 URL 转投 yt-dlp 分片下载，而不是报错弃任务。"""
    with tempfile.TemporaryDirectory(prefix="douyin_hls_redirect_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()

        final_m3u8 = "https://v3-web.douyinvod.com/video/tos/cn/index.m3u8?sign=final"

        def fake_direct(url, destination, **kwargs):
            raise HlsRedirectError(final_m3u8)

        monkeypatch.setattr("app.download_coordinator.download_direct_media", fake_direct)

        download_urls: list[str] = []
        original_download = adapter.download

        def recording_download(url, *args, **kwargs):
            download_urls.append(str(url))
            return original_download(url, *args, **kwargs)

        adapter.download = recording_download

        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            created = coordinator.submit(
                "https://www.douyin.com/video/1234567890",
                title="抖音播放地址跳HLS",
                options={"transcribe_after_download": False},
                # 带会话才会进 download_direct_media（无 Cookie 的 play 端点
                # 会走 media_forbidden 快失败，见上一用例）。
                session_cookies=[{
                    "name": "sessionid", "value": "session-secret", "domain": ".douyin.com",
                    "path": "/", "secure": True,
                }],
                media_url="https://www.douyin.com/aweme/v1/play/?video_id=v0200&target=1234567890",
            )
            done = wait_for(coordinator, created["id"], {"done", "failed"})
            assert done["status"] == "done", done
            assert download_urls == [final_m3u8]
        finally:
            coordinator.stop()
            controller.close()


def test_completed_download_can_be_promoted_to_transcription_without_redownload() -> None:
    with tempfile.TemporaryDirectory(prefix="download_promote_transcription_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            created = coordinator.submit(
                "https://www.bilibili.com/video/BV1promote", title="下载后转写",
                options={"transcribe_after_download": False},
            )
            completed = wait_for(coordinator, created["id"], {"done"})
            assert completed["job_id"] is None and completed["media_id"]
            assert len(adapter.rate_limits) == 1

            queued = controller.queue_download_for_transcription(created["id"])
            assert len(queued.created) == 1
            promoted = coordinator.get(created["id"])
            assert promoted and promoted["status"] == "queued"
            assert promoted["job_id"] == queued.created[0]
            job = controller.queue.get_job(queued.created[0])
            assert job and job["source_title"] == "下载后转写"
            assert len(adapter.rate_limits) == 1
        finally:
            coordinator.stop()
            controller.close()


def test_browser_handoff_resolves_without_local_download_and_links_import() -> None:
    with tempfile.TemporaryDirectory(prefix="browser_handoff_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            session = [{
                "name": "sessionid", "value": "session-secret", "domain": ".douyin.com",
                "path": "/", "secure": True,
            }]
            created = coordinator.submit(
                "https://www.douyin.com/video/1234567890",
                title="浏览器直提",
                options={"transcribe_after_download": True},
                session_cookies=session,
                mode="browser",
            )
            assert created["status"] == "awaiting_browser"
            assert created["browser_download"]["download_url"] == adapter.resolve_url
            assert created["browser_download"]["filename"] == "browser-resolved.mp4"
            assert created["browser_download"]["headers"] == {"Accept": "video/*"}
            assert adapter.rate_limits == []
            assert len(adapter.resolve_calls) == 1
            assert adapter.resolve_calls[0][3][0]["value"] == "session-secret"
            assert coordinator._transient_session(created["id"]) == ()
            assert "session-secret" not in json.dumps(created, ensure_ascii=False)

            source = Path(temp) / "browser-resolved.mp4"
            source.write_bytes(b"browser-download")
            imported = coordinator.submit_local_file(
                str(source),
                title="浏览器直提",
                transcribe=True,
                download_id=created["id"],
            )
            assert imported["media_id"] and imported["job_id"]
            linked = coordinator.get(created["id"])
            assert linked and linked["status"] == "queued"
            assert linked["media_id"] == imported["media_id"]
            assert linked["job_id"] == imported["job_id"]
            assert adapter.rate_limits == []
        finally:
            coordinator.stop()
            controller.close()


def test_browser_handoff_prefers_extension_media_hint_without_resolve() -> None:
    with tempfile.TemporaryDirectory(prefix="browser_media_hint_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            media_url = "https://v3-web.douyinvod.com/video/tos/example.mp4?token=hint"
            created = coordinator.submit(
                "https://www.douyin.com/video/1234567890",
                title="浏览器直提媒体地址",
                options={"transcribe_after_download": True},
                media_url=media_url,
                mode="browser",
            )
            assert created["status"] == "awaiting_browser"
            assert created["browser_download"]["download_url"] == media_url
            assert created["browser_download"]["filename"] == "浏览器直提媒体地址.mp4"
            assert created["browser_download"]["headers"] == {
                "Accept": "video/*,audio/*,application/octet-stream;q=0.9,*/*;q=0.8",
            }
            assert adapter.resolve_calls == []
            assert adapter.rate_limits == []
            assert coordinator._transient_session(created["id"]) == ()
            assert "media_url" not in created["options"]
            assert media_url not in json.dumps(created["options"], ensure_ascii=False)
        finally:
            coordinator.stop()
            controller.close()


@pytest.mark.parametrize(("browser_error", "status", "retryable"), [
    ("SERVER_FORBIDDEN", "failed", False),
    ("NETWORK_TIMEOUT", "failed", True),
    ("USER_CANCELED", "cancelled", False),
    ("https://v3-web.douyinvod.com/?token=secret", "failed", False),
])
def test_browser_download_interruption_is_diagnosed_without_signed_urls(
    browser_error: str, status: str, retryable: bool,
) -> None:
    with tempfile.TemporaryDirectory(prefix="browser_interruption_diagnostic_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = DownloadCoordinator(controller, adapter=FakeAdapter())
        try:
            created = coordinator.submit(
                "https://www.douyin.com/video/1234567890",
                title="浏览器下载中断",
                mode="browser",
            )
            assert created["status"] == "awaiting_browser"

            failed = coordinator.record_browser_download_failure(
                created["id"],
                browser_error=browser_error,
                bytes_received=0,
                total_bytes=9876,
            )

            assert failed["status"] == status
            assert failed["error"]["code"] == "browser_download_interrupted"
            assert failed["error"]["retryable"] is retryable
            assert failed["error"]["diagnostic"]["channel"] == "chrome.downloads"
            assert failed["error"]["diagnostic"]["bytes_received"] == 0
            assert failed["bytes_total"] == 9876
            serialized = json.dumps(failed, ensure_ascii=False)
            assert "token=secret" not in serialized
            assert "v3-web.douyinvod.com" not in serialized
            if browser_error == "SERVER_FORBIDDEN":
                assert failed["error"]["diagnostic"]["browser_error"] == "SERVER_FORBIDDEN"
                assert "HTTP 403" in failed["error"]["message"]
            elif browser_error.startswith("https://"):
                assert failed["error"]["diagnostic"]["browser_error"] == "BROWSER_INTERRUPTED_UNKNOWN"
        finally:
            coordinator.stop()
            controller.close()


def test_extension_failure_is_a_persistent_idempotent_download_task() -> None:
    with tempfile.TemporaryDirectory(prefix="extension_failure_record_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = DownloadCoordinator(controller, adapter=FakeAdapter())
        event_id = "a" * 32
        report = {
            "url": "https://www.douyin.com/video/1234567890",
            "title": "一次授权失败",
            "event_id": event_id,
            "phase": "extension_authorization",
            "channel": "browser_extension",
            "code": "douyin_session_unavailable",
            "message": "没有读取到当前抖音会话",
            "diagnostic": {"error_name": "TimeoutError", "http_status": 401},
        }
        try:
            first = coordinator.record_extension_failure(**report)
            again = coordinator.record_extension_failure(**report)

            assert first["status"] == "failed"
            assert first["error"]["code"] == "douyin_session_unavailable"
            assert first["error"]["diagnostic"]["phase"] == "extension_authorization"
            assert again["id"] == first["id"]
            assert len(controller.downloads.list(limit=50)) == 1
            task = next(row for row in controller.task_rows() if row["task_id"] == first["id"])
            assert task["error"]["diagnostic"]["event_id"] == event_id
        finally:
            coordinator.stop()
            controller.close()


def test_browser_handoff_rejects_unsupported_media_hint() -> None:
    with tempfile.TemporaryDirectory(prefix="browser_media_hint_invalid_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            with pytest.raises(DownloadAdapterError, match="不受支持"):
                coordinator.submit(
                    "https://www.douyin.com/video/1234567890",
                    title="坏地址",
                    media_url="https://example.com/video.mp4",
                    mode="browser",
                )
            assert adapter.resolve_calls == []
        finally:
            coordinator.stop()
            controller.close()


def test_browser_handoff_failed_resolve_retries_without_local_worker() -> None:
    with tempfile.TemporaryDirectory(prefix="browser_handoff_retry_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            adapter.resolve_error = DownloadAdapterError(
                "resolve_failed", "解析失败", retryable=True,
            )
            with pytest.raises(DownloadAdapterError, match="解析失败") as error_info:
                coordinator.submit(
                    "https://www.douyin.com/video/1234567890",
                    title="浏览器重试",
                    mode="browser",
                )
            items = coordinator.list(statuses=("failed",), limit=10)
            assert items and items[0]["status"] == "failed"
            download_id = items[0]["id"]
            assert error_info.value.download_id == download_id
            failed = coordinator.get(download_id)
            assert failed["error"]["diagnostic"] == {
                "phase": "media_resolution",
                "channel": "desktop_resolver",
                "error_type": "DownloadAdapterError",
            }

            adapter.resolve_error = None
            retried = coordinator.retry(download_id)
            assert retried["status"] == "awaiting_browser"
            assert retried["browser_download"]["download_url"] == adapter.resolve_url
            assert adapter.rate_limits == []
            assert len(adapter.resolve_calls) == 2
        finally:
            coordinator.stop()
            controller.close()


def test_yt_dlp_resolve_returns_direct_url_without_download(monkeypatch) -> None:
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, *, download):
            captured["download"] = download
            return {
                "title": "示例: 视频",
                "url": "https://v3-web.douyinvod.com/video/tos/example.mp4?token=x",
            }

    monkeypatch.setitem(
        sys.modules,
        "yt_dlp",
        types.SimpleNamespace(YoutubeDL=FakeYoutubeDL),
    )
    resolved = YtDlpAdapter().resolve(
        "https://www.douyin.com/video/1234567890",
        referer="https://www.douyin.com/video/1234567890",
        user_agent="TestAgent/1.0",
    )
    assert captured["skip_download"] is True
    assert captured["download"] is False
    assert captured["format"] == "best[ext=mp4]/best"
    assert resolved["download_url"] == "https://v3-web.douyinvod.com/video/tos/example.mp4?token=x"
    assert resolved["filename"].endswith(".mp4")
    assert ":" not in resolved["filename"]
    assert resolved["headers"]["Accept"].startswith("video/*")
    assert "Referer" not in resolved["headers"]
    assert "User-Agent" not in resolved["headers"]


def test_browser_download_url_allowlist() -> None:
    assert is_browser_download_url("https://v3-web.douyinvod.com/video.mp4?x=1")
    assert is_browser_download_url("https://www.douyin.com/aweme/v1/play/?video_id=1")
    assert is_browser_download_url("https://www.douyin.com/video/play/?video_id=1")
    assert not is_browser_download_url("https://example.com/video.mp4")
    assert not is_browser_download_url("https://www.douyin.com/aweme/v1/feed/")
    assert not is_browser_download_url("http://v3-web.douyinvod.com/video.mp4")
    assert sanitize_download_filename('A:B\\C*D?') == "A_B_C_D_"


def _recover_dangling_download_fixture(*, same_content: bool = False) -> tuple[list[dict], list[dict]]:
    with tempfile.TemporaryDirectory(prefix="download_recovery_") as temp:
        settings_store = SettingsStore(base_dir=Path(temp) / "data")
        controller = AppController(settings_store)
        class RecoveryAdapter(FakeAdapter):
            def download(self, url, destination, **kwargs):
                # Workers share a queue: any adapter may claim any URL. Media
                # identity must follow the request, not the consumer instance.
                stem = "shared-recovery" if same_content else url.rsplit("/", 1)[-1]
                self.filename = f"{stem}.mp4"
                return super().download(url, destination, **kwargs)

        adapters = [RecoveryAdapter() for _ in range(3)]
        coordinators = [DownloadCoordinator(controller, adapter=adapter) for adapter in adapters]
        for coordinator in coordinators:
            coordinator.start()
        try:
            download_ids: list[str] = []
            for index, url in enumerate((
                "https://youtu.be/recovery-a",
                "https://youtu.be/recovery-b",
                "https://youtu.be/recovery-c",
            )):
                created = coordinators[index].submit(
                    url,
                    title=f"恢复样本-{index}",
                    options={"transcribe_after_download": False},
                )
                completed = wait_for(coordinators[index], created["id"], {"done"})
                download_ids.append(completed["id"])
            # Establish the fixture's media cardinality before testing repair.
            media = controller.media.list_media()
            assert len(media) == (1 if same_content else 3)
            assert len({item["content_sha256"] for item in media}) == len(media)
            assert not controller.queue.list_jobs()
        finally:
            for coordinator in coordinators:
                coordinator.stop()
            controller.close()

        db_path = Path(temp) / "data" / "library.db"
        con = sqlite3.connect(db_path)
        options_transcribe = json.dumps({
            "quality": "best-available", "audio_only": False,
            "transcribe_after_download": True, "language": "zh",
        }, ensure_ascii=False)
        mutations = [
            ("queued", "missing-job-a", "missing-media-a", None),
            ("done", "missing-job-b", "missing-media-b", options_transcribe),
            ("transcribing", "missing-job-c", "missing-media-c", None),
        ]
        for index, (status, job_id, media_id, options) in enumerate(mutations):
            if options is None:
                con.execute(
                    "UPDATE download_jobs SET status=?, job_id=?, media_id=? WHERE id=?",
                    (status, job_id, media_id, download_ids[index]),
                )
            else:
                con.execute(
                    "UPDATE download_jobs SET status=?, job_id=?, media_id=?, options=? WHERE id=?",
                    (status, job_id, media_id, options, download_ids[index]),
                )
        con.commit()
        con.close()

        recovered: AppController | None = None
        try:
            recovered = AppController(SettingsStore(base_dir=Path(temp) / "data"))
            recovered_items = []
            for download_id in download_ids:
                item = recovered.downloads.get(download_id)
                assert item is not None and item["status"] == "queued"
                assert item["job_id"] and recovered.queue.get_job(item["job_id"])
                assert item["media_id"] and recovered.media.get_media(item["media_id"])
                recovered_items.append(item)
            jobs = recovered.queue.list_jobs()
            assert all(job["status"] == "queued" for job in jobs)
            return jobs, recovered_items
        finally:
            if recovered is not None:
                recovered.close()


def test_startup_recovers_dangling_browser_downloads() -> None:
    jobs, downloads = _recover_dangling_download_fixture()
    assert len(jobs) == 3
    assert len({item["job_id"] for item in downloads}) == 3
    assert len({item["media_id"] for item in downloads}) == 3


def test_startup_recovery_reuses_one_job_for_identical_downloaded_content() -> None:
    jobs, downloads = _recover_dangling_download_fixture(same_content=True)
    assert len(jobs) == 1
    assert {item["job_id"] for item in downloads} == {jobs[0]["id"]}
    assert len({item["media_id"] for item in downloads}) == 1


def test_douyin_download_without_current_authorization_fails_before_adapter() -> None:
    with tempfile.TemporaryDirectory(prefix="douyin_requires_authorization_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            created = coordinator.submit("https://www.douyin.com/video/1234567890")
            failed = wait_for(coordinator, created["id"], {"failed"})
            assert failed["error"]["code"] == "authorization_required"
            assert adapter.session_cookies == []
        finally:
            coordinator.stop()
            controller.close()


def test_douyin_media_hint_downloads_without_browser_session(monkeypatch) -> None:
    # A captured player URL is CDN-signed and Referer-guarded, not cookie-
    # guarded, so the extension submits it without collecting any Douyin
    # cookies. The coordinator must download it directly instead of demanding
    # a fresh authorization it cannot get.
    with tempfile.TemporaryDirectory(prefix="douyin_hint_no_session_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        captured: dict[str, object] = {}

        def fake_direct(url, destination, **kwargs):
            captured["url"] = url
            captured["cookies"] = tuple(dict(item) for item in (kwargs.get("session_cookies") or ()))
            target = Path(destination) / "hint-no-session.mp4"
            target.write_bytes(b"captured-media")
            kwargs["progress"](target.stat().st_size, target.stat().st_size)
            return target

        monkeypatch.setattr("app.download_coordinator.download_direct_media", fake_direct)
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            created = coordinator.submit(
                "https://www.douyin.com/video/1234567890",
                title="直提免授权",
                options={"transcribe_after_download": False},
                media_url="https://v3-web.douyinvod.com/video/tos/api.mp4?sign=abc",
            )
            done = wait_for(coordinator, created["id"], {"done", "failed"})
            assert done["status"] == "done"
            assert captured["url"].startswith("https://v3-web.douyinvod.com/")
            assert captured["cookies"] == ()
            assert adapter.session_cookies == []
        finally:
            coordinator.stop()
            controller.close()


def test_download_options_reject_unrecognized_sensitive_fields() -> None:
    with tempfile.TemporaryDirectory(prefix="download_options_allowlist_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = DownloadCoordinator(controller, adapter=FakeAdapter())
        try:
            with pytest.raises(DownloadAdapterError, match="未支持的字段"):
                coordinator.submit(
                    "https://youtu.be/allowed", options={"cookies": "session-secret"},
                )
        finally:
            controller.close()


def test_download_fails_before_adapter_when_disk_space_is_below_reserve(monkeypatch) -> None:
    with tempfile.TemporaryDirectory(prefix="download_disk_guard_") as temp:
        settings_store = SettingsStore(base_dir=Path(temp) / "data")
        settings_store.update(download_min_free_gib=5.0)
        controller = AppController(settings_store)
        adapter = FakeAdapter()
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        created = coordinator.submit("https://youtu.be/disk-guard")
        claimed = coordinator.store.claim_next()
        assert claimed and claimed["id"] == created["id"]
        monkeypatch.setattr(
            "app.download_coordinator.shutil.disk_usage",
            lambda _path: type("Usage", (), {"free": 4 * 1024 ** 3})(),
        )
        try:
            coordinator._run_one(claimed)
            failed = coordinator.get(created["id"])
            assert failed and failed["status"] == "failed"
            assert failed["error"] == {
                "code": "insufficient_disk_space",
                "message": "下载目录剩余空间不足，需要至少保留 5 GiB",
                "retryable": True,
            }
            assert adapter.rate_limits == []
        finally:
            controller.close()


def test_direct_media_download_uses_browser_headers_and_reports_progress(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status = 200
        headers = {
            "Content-Type": "video/mp4",
            "Content-Length": "10",
        }

        def __init__(self):
            self.stream = BytesIO(b"0123456789")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://v3-web.douyinvod.com/video/tos/example.mp4?token=x"

        def read(self, size=-1):
            return self.stream.read(size)

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {key.lower(): value for key, value in request.header_items()}
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("engine.downloader.urllib.request.urlopen", fake_urlopen)
    with tempfile.TemporaryDirectory(prefix="direct_media_") as temp:
        progress = []
        target = download_direct_media(
            "https://v3-web.douyinvod.com/video/tos/example.mp4?token=x",
            Path(temp),
            referer="https://www.douyin.com/video/123",
            user_agent="TestAgent/1.0",
            rate_limit_bps=None,
            progress=lambda done, total: progress.append((done, total)),
            cancel_check=lambda: False,
        )
        assert target.read_bytes() == b"0123456789"
        assert captured["headers"]["referer"] == "https://www.douyin.com/video/123"
        assert captured["headers"]["user-agent"] == "TestAgent/1.0"
        assert captured["headers"]["accept"].startswith("video/*")
        assert progress[-1] == (10, 10)
        assert captured["timeout"] == 30


def test_direct_media_url_rejects_stream_manifests_and_non_media_hosts() -> None:
    assert is_direct_media_url("https://v3-web.douyinvod.com/video.mp4?x=1")
    assert is_direct_media_url("https://www.douyin.com/aweme/v1/play/?video_id=1")
    assert not is_direct_media_url("https://v3-web.douyinvod.com/video.m3u8?x=1")
    assert not is_direct_media_url("https://v3-web.douyinvod.com/playlist.mpd")
    assert not is_direct_media_url("https://v3-web.douyinvod.com/cover.jpg")
    assert not is_direct_media_url("https://example.com/video.mp4")
    assert is_direct_media_url("https://api.amemv.com/aweme/v1/play/?video_id=1")
    assert not is_direct_media_url("https://api.amemv.com/aweme/v1/feed/")


def test_hls_media_url_recognizes_douyin_master_playlist_only() -> None:
    # 主清单识别：仅抖音 CDN/播放域上的 .m3u8；提交门据此放行，
    # 协调器据此路由 yt-dlp；直下语义保持拒绝。
    assert is_hls_media_url("https://v3-web.douyinvod.com/x/index.m3u8?sign=1")
    assert is_hls_media_url("https://www.douyin.com/video/tos/cn/index.m3u8")
    assert not is_hls_media_url("https://example.com/x/index.m3u8")
    assert not is_hls_media_url("http://v3-web.douyinvod.com/x/index.m3u8")
    assert not is_hls_media_url("https://v3-web.douyinvod.com/video/tos/example.mp4?token=1")
    assert not is_direct_media_url("https://v3-web.douyinvod.com/x/index.m3u8")


class _MediaResponse:
    status = 200
    headers = {"Content-Type": "video/mp4", "Content-Length": "10"}

    def __init__(self):
        self.stream = BytesIO(b"0123456789")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return "https://v3-web.douyinvod.com/video/tos/example.mp4?token=x"

    def read(self, size=-1):
        return self.stream.read(size)


def test_direct_media_falls_back_to_direct_connection_when_proxy_refuses(monkeypatch) -> None:
    # A dead local proxy that still owns the Windows proxy setting refuses
    # every loopback connection; the CDN itself stays reachable directly.
    # The downloader must rescue that state with one proxy-less retry.
    monkeypatch.setattr("engine.downloader._NO_PROXY_OPENER", None)
    opener_calls = []

    class FakeDirectOpener:
        def open(self, request, timeout):
            opener_calls.append(request.full_url)
            return _MediaResponse()

    def refused_urlopen(request, timeout):
        raise urllib.error.URLError(
            OSError(errno.ECONNREFUSED, "由于目标计算机积极拒绝，无法连接。")
        )

    monkeypatch.setattr("engine.downloader.urllib.request.urlopen", refused_urlopen)
    monkeypatch.setattr(
        "engine.downloader.urllib.request.build_opener", lambda *h: FakeDirectOpener()
    )
    with tempfile.TemporaryDirectory(prefix="direct_media_proxy_dead_") as temp:
        target = download_direct_media(
            "https://v3-web.douyinvod.com/video/tos/example.mp4?token=x",
            Path(temp),
            referer="https://www.douyin.com/video/123",
            user_agent="TestAgent/1.0",
            rate_limit_bps=None,
            progress=lambda *_args: None,
            cancel_check=lambda: False,
        )
        assert target.read_bytes() == b"0123456789"
        assert opener_calls == ["https://v3-web.douyinvod.com/video/tos/example.mp4?token=x"]


def test_direct_media_does_not_bypass_proxy_for_http_level_errors(monkeypatch) -> None:
    # The server answered (403): a proxy-less retry would hit the same CDN
    # policy, so the fallback must stay untouched for HTTP errors.
    monkeypatch.setattr("engine.downloader._NO_PROXY_OPENER", None)
    opener_calls = []

    class FakeDirectOpener:
        def open(self, request, timeout):
            opener_calls.append(request.full_url)
            return _MediaResponse()

    def forbidden_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("engine.downloader.urllib.request.urlopen", forbidden_urlopen)
    monkeypatch.setattr(
        "engine.downloader.urllib.request.build_opener", lambda *h: FakeDirectOpener()
    )
    with tempfile.TemporaryDirectory(prefix="direct_media_http_error_") as temp:
        with pytest.raises(DownloadAdapterError) as exc_info:
            download_direct_media(
                "https://v3-web.douyinvod.com/video/tos/example.mp4?token=x",
                Path(temp),
                referer="",
                user_agent="",
                rate_limit_bps=None,
                progress=lambda *_args: None,
                cancel_check=lambda: False,
                attempts=1,
            )
        assert "403" in exc_info.value.message
        assert exc_info.value.code == "media_forbidden"
        assert exc_info.value.retryable is False
        assert opener_calls == []


def test_media_candidate_selection_ignores_sidecars_and_outside_paths() -> None:
    with tempfile.TemporaryDirectory(prefix="yt_dlp_candidates_") as temp:
        root = Path(temp)
        staging = root / "stage"
        staging.mkdir()
        media = staging / "video.mp4"
        media.write_bytes(b"media")
        (staging / "video.info.json").write_bytes(b"x" * 100)
        (staging / "cover.webp").write_bytes(b"x" * 200)
        outside = root / "outside.mp4"
        outside.write_bytes(b"outside")
        found = YtDlpAdapter._media_candidates(staging, {
            "filepath": str(outside),
            "requested_downloads": [{"filepath": str(media)}],
        })
        assert found == [media.resolve()]


def test_yt_dlp_uses_windows_safe_filenames(monkeypatch) -> None:
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, *, download):
            assert download is True
            target = Path(captured["outtmpl"]).parent / "safe-video.mp4"
            target.write_bytes(b"media")
            return {"filepath": str(target)}

    monkeypatch.setitem(
        sys.modules,
        "yt_dlp",
        types.SimpleNamespace(YoutubeDL=FakeYoutubeDL),
    )
    with tempfile.TemporaryDirectory(prefix="yt_dlp_windows_names_") as temp:
        target = YtDlpAdapter().download(
            "https://www.douyin.com/video/1234567890",
            Path(temp),
            audio_only=False,
            rate_limit_bps=None,
            progress=lambda *_args: None,
            cancel_check=lambda: False,
        )
        assert target.name == "safe-video.mp4"
        assert captured["windowsfilenames"] is True
        assert captured["format"] == "best[ext=mp4]/best"
        assert captured["concurrent_fragment_downloads"] == 4


def test_yt_dlp_session_cookie_stream_stays_in_memory_and_is_erased(monkeypatch) -> None:
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = dict(options)
            captured["cookie_stream"] = self.options["cookiefile"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, *, download):
            cookie_stream = self.options["cookiefile"]
            assert isinstance(cookie_stream, StringIO)
            assert "session-secret" in cookie_stream.getvalue()
            target = Path(self.options["outtmpl"]).parent / "session-video.mp4"
            target.write_bytes(b"media")
            return {"filepath": str(target)}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    session = sanitize_douyin_session_cookies([{
        "name": "sessionid", "value": "session-secret", "domain": ".douyin.com",
        "path": "/", "secure": True,
    }])
    with tempfile.TemporaryDirectory(prefix="yt_dlp_session_cookie_") as temp:
        target = YtDlpAdapter().download(
            "https://www.douyin.com/video/1234567890", Path(temp),
            audio_only=False, rate_limit_bps=None, session_cookies=session,
            progress=lambda *_args: None, cancel_check=lambda: False,
        )
        assert target.name == "session-video.mp4"
        assert captured["cookie_stream"].closed
        assert not list(Path(temp).rglob("browser-session-cookies.txt"))
        assert all(b"session-secret" not in path.read_bytes() for path in Path(temp).rglob("*.*"))


def test_real_yt_dlp_reads_browser_session_from_memory_stream() -> None:
    import yt_dlp

    session = sanitize_douyin_session_cookies([{
        "name": "sessionid", "value": "session-secret", "domain": ".douyin.com",
        "path": "/", "secure": True,
    }])
    stream = YtDlpAdapter._write_cookie_stream(session)
    try:
        with yt_dlp.YoutubeDL({"cookiefile": stream, "quiet": True, "no_warnings": True}) as client:
            assert "session-secret" in client.cookiejar.get_cookie_header(
                "https://www.douyin.com/video/1234567890",
            )
    finally:
        stream.erase()
    assert stream.closed


def test_yt_dlp_retries_with_id_only_filename_after_windows_error(monkeypatch) -> None:
    calls = []

    class FlakyYoutubeDL:
        def __init__(self, options):
            self.options = dict(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, *, download):
            assert download is True
            calls.append(self.options["outtmpl"])
            if len(calls) == 1:
                raise OSError(22, "Invalid argument")
            target = Path(self.options["outtmpl"]).parent / "1234567890.mp4"
            target.write_bytes(b"media")
            return {"filepath": str(target)}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FlakyYoutubeDL))
    with tempfile.TemporaryDirectory(prefix="yt_dlp_filename_retry_") as temp:
        target = YtDlpAdapter().download(
            "https://www.douyin.com/video/1234567890",
            Path(temp), audio_only=False, rate_limit_bps=None,
            progress=lambda *_args: None, cancel_check=lambda: False,
        )
        assert target.name == "1234567890.mp4"
        assert len(calls) == 2
        assert "%(title)" in calls[0]
        assert "%(id)s" in calls[1] and "%(title)" not in calls[1]


def test_default_download_only_when_setting_off() -> None:
    """设置“下载后自动转写”默认关闭：下载完成即 done，不创建转写任务。"""
    with tempfile.TemporaryDirectory(prefix="download_only_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        adapter = FakeAdapter()
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            created = coordinator.submit(
                "https://www.bilibili.com/video/BV1only", title="只下载",
            )
            done = wait_for(coordinator, created["id"], {"done", "failed"})
            assert done["status"] == "done"
            assert done["media_id"]
            assert done["job_id"] is None
        finally:
            coordinator.stop()
            controller.close()


def test_global_setting_enables_default_transcribe() -> None:
    """设置“下载后自动转写”开启时，未显式传选项也默认入队转写。"""
    with tempfile.TemporaryDirectory(prefix="global_transcribe_") as temp:
        settings_store = SettingsStore(base_dir=Path(temp) / "data")
        settings_store.update(transcribe_after_download=True)
        controller = AppController(settings_store)
        adapter = FakeAdapter()
        coordinator = DownloadCoordinator(controller, adapter=adapter)
        coordinator.start()
        try:
            created = coordinator.submit(
                "https://www.bilibili.com/video/BV1global", title="全局转写",
            )
            linked = wait_for(coordinator, created["id"], {"queued", "failed"})
            assert linked["status"] == "queued"
            assert linked["job_id"]
        finally:
            coordinator.stop()
            controller.close()


# ------------------------------------------------------------- 升级维护改造

def test_allow_any_url_flag_relaxes_host_allowlist_only() -> None:
    # 实验性「任意链接」模式：放行未知域名，但不放松 scheme/凭据/长度防线。
    url = "https://example.com/some/page"
    with pytest.raises(DownloadAdapterError):
        validate_supported_url(url)
    assert validate_supported_url(url, allow_any_url=True) == url
    with pytest.raises(DownloadAdapterError):
        validate_supported_url("https://user:pass@example.com/v", allow_any_url=True)
    with pytest.raises(DownloadAdapterError):
        validate_supported_url("javascript:alert(1)", allow_any_url=True)


def test_js_runtime_options_mapping() -> None:
    from engine.downloader import js_runtime_options

    assert js_runtime_options("") == {}
    assert js_runtime_options("   ") == {}
    assert js_runtime_options(r"C:\tools\deno.exe") == {
        "js_runtimes": {"deno": {"path": r"C:\tools\deno.exe"}},
    }
    assert js_runtime_options("/usr/bin/node") == {
        "js_runtimes": {"node": {"path": "/usr/bin/node"}},
    }
    # 无法识别的可执行名按上游默认 deno 处理
    assert js_runtime_options(r"C:\bin\js.exe") == {
        "js_runtimes": {"deno": {"path": r"C:\bin\js.exe"}},
    }


def test_youtube_bot_check_and_js_runtime_errors_are_actionable() -> None:
    bot = normalized_download_error(
        RuntimeError("Sign in to confirm you're not a bot"), platform="youtube",
    )
    assert bot.code == "authorization_required" and bot.retryable is False

    missing = normalized_download_error(
        RuntimeError("Signature solving failed: some formats may be missing"),
        platform="youtube",
    )
    assert missing.code == "js_runtime_missing" and missing.retryable is True
    assert "Deno" in missing.message

    throttled = normalized_download_error(
        RuntimeError("Unable to download webpage: HTTP Error 403: Forbidden"),
        platform="youtube",
    )
    assert throttled.code == "js_runtime_missing" and throttled.retryable is True

    # 同样的 403 出现在 B 站时不应被误标为 JS 运行时问题
    other = normalized_download_error(
        RuntimeError("HTTP Error 403: Forbidden"), platform="bilibili",
    )
    assert other.code == "download_failed"


def test_youtube_uses_dash_preferring_format_selector() -> None:
    adapter = YtDlpAdapter()
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    # YouTube 高画质只有 DASH 分离轨：与 B 站同构，优先 MP4+M4A 组合。
    assert adapter._format_selector(url, audio_only=False) == (
        "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
    )
    # 转写意图仍然只要音轨。
    assert adapter._format_selector(url, audio_only=True) == "bestaudio/best"
    # 抖音维持渐进式单文件优先（避免额外合并等待）。
    assert adapter._format_selector("https://www.douyin.com/video/123", audio_only=False) == "best[ext=mp4]/best"


def test_submit_dedupes_active_job_for_same_url() -> None:
    """同一 URL 已有活跃任务时复用：按钮无反馈期的连点曾在一秒内产出
    2–3 条必败重复行（2026-09-22 18:25/18:36 实机记录）。终态后重新
    提交必须允许建新行（重试语义不变）。"""
    with tempfile.TemporaryDirectory(prefix="submit_dedup_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = DownloadCoordinator(controller, adapter=FakeAdapter())
        try:
            url = "https://www.douyin.com/video/1234567890"
            first = coordinator.submit(
                url,
                title="第一次点击",
                options={"transcribe_after_download": False},
            )
            assert first["status"] in {"received", "resolving"}
            # 连点/弹窗双入口：任务仍在处理中，必须复用同一行
            second = coordinator.submit(
                url,
                title="第二次点击",
                options={"transcribe_after_download": False},
            )
            assert second["id"] == first["id"]
            # 不同视频不受影响
            other = coordinator.submit(
                "https://www.douyin.com/video/9876543210",
                title="另一个视频",
                options={"transcribe_after_download": False},
            )
            assert other["id"] != first["id"]
            # 终态后重新提交允许建新行
            coordinator.store.transition(first["id"], "cancelled")
            third = coordinator.submit(
                url,
                title="失败后的正常重试",
                options={"transcribe_after_download": False},
            )
            assert third["id"] != first["id"]
        finally:
            coordinator.stop()
            controller.close()


@pytest.mark.parametrize("short_staging", [False, True])
def test_media_candidates_accept_real_short_aliases_and_reject_outside(tmp_path, short_staging):
    from test_queue_store import _windows_short_alias
    staging = tmp_path / "Candidate staging regression"
    staging.mkdir()
    short_root = _windows_short_alias(staging)
    media = staging / "video.mp4"
    media.write_bytes(b"media")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    sibling = tmp_path / "Candidate staging regression outside"
    sibling.mkdir()
    (sibling / "video.mp4").write_bytes(b"prefix outside")
    (staging / "video.info.json").write_bytes(b"sidecar")
    root = short_root if short_staging else staging
    hint = media if short_staging else short_root / media.name
    found = YtDlpAdapter._media_candidates(root, {
        "filepath": str(outside),
        "requested_downloads": [{"filepath": str(hint)}, {"filepath": str(sibling / media.name)}],
    })
    assert found == [media.resolve()]
    assert outside.is_file() and (sibling / media.name).is_file()


def test_media_candidates_reject_link_to_media_outside_staging(tmp_path):
    staging = tmp_path / "stage"
    staging.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    linked = staging / "linked.mp4"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("当前 Windows 策略不允许创建文件链接")
    assert YtDlpAdapter._media_candidates(staging, {"filepath": str(linked)}) == []
