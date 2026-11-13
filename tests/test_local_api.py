# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Loopback API authentication, CORS and request-boundary tests."""
from __future__ import annotations

import http.client
import urllib.parse
import base64
import json
import socket
import threading
import tempfile
import time
from pathlib import Path
from unittest import mock
import pytest

from app.controller import AppController
from app.download_coordinator import DownloadCoordinator
from app.local_api import LocalApiServer, MAX_BODY_BYTES
from engine.downloader import DownloadAdapterError
from engine.media_store import MediaTypeError
from engine.settings import SettingsStore
from account_auth_fixtures import grant_account


ORIGIN = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_ORIGIN = "chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def logged_in_controller(settings_store):
    controller = AppController(settings_store)
    controller.settings = grant_account(settings_store, settings_store.base_dir)
    return controller


class StubCoordinator:
    def __init__(self) -> None:
        self.jobs = []
        self.started = False
        self.sessions = []
        self.media_urls = []
        self.media_candidates = []
        self.cover_urls = []
        self.gallery_images = []

    def start(self):
        self.started = True

    def stop(self, timeout=3.0):
        self.started = False

    def submit(self, url, *, title="", options=None, session_cookies=None, media_url="", media_candidates=None, mode="local", cover_url="", gallery_images=None):
        item = {
            "id": f"{len(self.jobs) + 1:012x}", "url": url, "title": title,
            "source": "browser", "platform_hint": "youtube",
            "requested_at": "2026-07-24T12:00:00",
            "updated_at": "2026-07-24T12:00:00",
            "options": options or {}, "status": "received", "progress": 0.0,
            "bytes_done": 0, "downloaded_path": "C:/private/video.mp4",
            "cancel_requested": False, "attempts": 0,
        }
        if mode == "browser":
            item["status"] = "awaiting_browser"
            item["browser_download"] = {
                "download_url": "https://v3-web.douyinvod.com/video/tos/example.mp4?token=x",
                "filename": "example.mp4",
                "headers": {"Accept": "video/*"},
            }
        self.jobs.append(item)
        self.sessions.append([dict(cookie) for cookie in session_cookies] if session_cookies else None)
        self.media_urls.append(media_url)
        self.media_candidates.append(media_candidates)
        self.cover_urls.append(cover_url)
        self.gallery_images.append(gallery_images)
        return item

    def submit_local_file(self, path, *, title="", transcribe=True, download_id=None, validate_media=False):
        return {
            "media_id": "media-local",
            "job_id": "job-local" if transcribe else None,
            "path": path,
            "download_id": download_id,
        }

    def list(self, *, statuses=None, limit=100):
        if statuses and any(status == "invalid" for status in statuses):
            from engine.download_store import DownloadStateError
            raise DownloadStateError("下载状态非法")
        return self.jobs[-limit:]

    def get(self, download_id):
        return next((item for item in self.jobs if item["id"] == download_id), None)

    def cancel(self, download_id):
        item = self.get(download_id)
        if item is None:
            from engine.download_store import DownloadNotFoundError
            raise DownloadNotFoundError(download_id)
        item["status"] = "cancelled"
        return item

    def retry(self, download_id):
        item = self.get(download_id)
        if item is None:
            from engine.download_store import DownloadNotFoundError
            raise DownloadNotFoundError(download_id)
        item["status"] = "received"
        return item

    def record_browser_download_failure(
        self, download_id, *, browser_error="", bytes_received=None, total_bytes=None,
        phase="browser_download_interrupted", browser_detail="",
    ):
        item = self.get(download_id)
        if item is None:
            from engine.download_store import DownloadNotFoundError
            raise DownloadNotFoundError(download_id)
        item["status"] = "failed"
        item["bytes_done"] = bytes_received or 0
        item["bytes_total"] = total_bytes
        item["error"] = {
            "code": "browser_download_interrupted",
            "message": f"Chrome 下载错误 {browser_error}",
            "retryable": False,
            "diagnostic": {
                "channel": "chrome.downloads",
                "phase": phase,
                "browser_error": browser_error,
                "browser_detail": browser_detail,
                "bytes_received": bytes_received,
                "total_bytes": total_bytes,
            },
        }
        return item


def call(server, method, path, *, token="", origin="", body=None, headers=None):
    host, port = server.address
    connection = http.client.HTTPConnection(host, port, timeout=3)
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    if origin:
        request_headers["Origin"] = origin
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=request_headers)
    response = connection.getresponse()
    raw = response.read()
    result = json.loads(raw.decode("utf-8")) if raw else None
    headers_out = dict(response.getheaders())
    connection.close()
    return response.status, result, headers_out


def test_downloads_list_filters_by_url_for_extension_precheck() -> None:
    """扩展提交前预检（先生 2026-10-08）：按规范 URL 过滤任务列表。"""
    with tempfile.TemporaryDirectory(prefix="local_api_urlfilter_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = StubCoordinator()
        coordinator.jobs = []
        for url, status in (
            ("https://www.douyin.com/video/1111111111111111111", "done"),
            ("https://www.douyin.com/video/2222222222222222222", "downloading"),
            ("https://www.bilibili.com/video/BV1other", "done"),
        ):
            row = dict(coordinator.submit(url))
            row["status"] = status
            coordinator.jobs[-1] = row
        server = LocalApiServer(controller, coordinator, port=0)
        server.start()
        token = controller.settings["local_api_token"]
        try:
            status, data, _ = call(
                server, "GET",
                "/v1/downloads?limit=50&url=" + urllib.parse.quote("https://www.douyin.com/video/1111111111111111111", safe=""),
                token=token, origin=ORIGIN,
            )
            assert status == 200
            assert [job["url"] for job in data["jobs"]] == ["https://www.douyin.com/video/1111111111111111111"]
            assert data["jobs"][0]["status"] == "done"
        finally:
            server.stop()
            controller.close()


def test_api_auth_cors_origin_pin_and_request_limits() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = StubCoordinator()
        server = LocalApiServer(controller, coordinator, port=0)
        server.start()
        token = controller.settings["local_api_token"]
        try:
            status, data, _ = call(server, "GET", "/health")
            assert status == 200 and data["auth_required"] is True
            assert call(server, "GET", "/v1/downloads")[0] == 401

            status, _, headers = call(
                server, "OPTIONS", "/v1/downloads", origin=ORIGIN,
                headers={"Access-Control-Request-Method": "GET"},
            )
            assert status == 204 and headers["Access-Control-Allow-Origin"] == ORIGIN

            status, data, headers = call(
                server, "POST", "/v1/downloads", token=token, origin=ORIGIN,
                body={"url": "https://youtu.be/abc", "title": "示例"},
            )
            assert status == 202 and data["job"]["status"] == "received"
            assert "downloaded_path" not in data["job"]
            assert headers["Access-Control-Allow-Origin"] == ORIGIN
            assert controller.settings_store.load()["extension_origin"] == ORIGIN
            assert call(
                server, "GET", "/v1/downloads", token=token, origin=OTHER_ORIGIN,
            )[0] == 403

            for value in ("abc", "0", "501"):
                status, data, _ = call(
                    server, "GET", f"/v1/downloads?limit={value}", token=token,
                    origin=ORIGIN,
                )
                assert status == 400 and data["error"]["code"] == "invalid_limit"
            status, data, _ = call(
                server, "GET", "/v1/downloads?status=invalid", token=token,
                origin=ORIGIN,
            )
            assert status == 400 and data["error"]["code"] == "invalid_status"

            status, data, _ = call(
                server, "POST", "/v1/downloads/batch", token=token, origin=ORIGIN,
                body={"items": [{}] * 51},
            )
            assert status == 400 and data["error"]["code"] == "invalid_batch"

            too_large = b"x" * (MAX_BODY_BYTES + 1)
            status, data, _ = call(
                server, "POST", "/v1/downloads", token=token, origin=ORIGIN,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(too_large)),
                },
            )
            assert status == 400 and data["error"]["code"] == "invalid_request"
        finally:
            server.stop()
            controller.close()


def test_douyin_capture_diagnostics_are_authenticated_and_privacy_safe(caplog) -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_capture_diagnostic_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        server = LocalApiServer(controller, StubCoordinator(), port=0)
        server.start()
        token = controller.settings["local_api_token"]
        body = {
            "outcome": "url_missing",
            "reason": "detail_response_without_media",
            "elapsed_ms": 8100,
            "main_world_present": True,
            "main_world_stale": False,
            "main_world_version": "0.12.19",
            "detail_requests": 2,
            "detail_responses": 2,
            "detail_json_responses": 2,
            "detail_json_failures": 0,
            "media_items": 0,
            "response_aweme_nodes": 2,
            "response_video_nodes": 2,
            "response_play_addr_nodes": 2,
            "response_url_candidates": 4,
            "response_https_candidates": 4,
            "response_allowed_urls": 0,
            # 封面取证（2026-11-01）：cover_nodes>0 而 cover_accepted=0 即白名单漂移。
            "response_cover_nodes": 3,
            "response_cover_accepted": 0,
            "captured_targets": 0,
            "target_captured": False,
            "target_cover_held": False,
            "unbound_capture": False,
            "player_present": True,
            "player_ready_state": 4,
            "player_paused": False,
            # Unknown fields are ignored and must never enter the diagnostics log.
            "page_url": "https://www.douyin.com/video/secret-id?token=secret-url",
            "title": "secret title",
            "cookie": "secret-cookie",
        }
        try:
            assert call(server, "POST", "/v1/diagnostics/douyin-capture", origin=ORIGIN, body=body)[0] == 401
            with caplog.at_level("INFO", logger="shengjian.desktop"):
                status, response, _ = call(
                    server, "POST", "/v1/diagnostics/douyin-capture",
                    token=token, origin=ORIGIN, body=body,
                )
            assert status == 202 and response == {"recorded": True}
            assert "douyin_capture_diagnostic" in caplog.text
            assert '"detail_requests":2' in caplog.text
            assert '"response_url_candidates":4' in caplog.text
            assert '"response_cover_nodes":3' in caplog.text
            assert '"target_cover_held":false' in caplog.text
            assert "secret-id" not in caplog.text
            assert "secret-url" not in caplog.text
            assert "secret title" not in caplog.text
            assert "secret-cookie" not in caplog.text

            body["reason"] = "arbitrary user supplied text"
            status, response, _ = call(
                server, "POST", "/v1/diagnostics/douyin-capture",
                token=token, origin=ORIGIN, body=body,
            )
            assert status == 400 and response["error"]["code"] == "invalid_request"
        finally:
            server.stop()
            controller.close()


def test_authorized_douyin_session_is_forwarded_without_echoing_secret() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_browser_session_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = StubCoordinator()
        server = LocalApiServer(controller, coordinator, port=0)
        server.start()
        token = controller.settings["local_api_token"]
        try:
            status, data, _ = call(
                server, "GET", "/v1/browser/douyin-session", token=token, origin=ORIGIN,
            )
            assert status == 404
            status, data, _ = call(
                server, "POST", "/v1/downloads", token=token, origin=ORIGIN,
                body={
                    "url": "https://www.douyin.com/video/1234567890",
                    "browser_session": [{
                        "name": "sessionid", "value": "secret", "domain": ".douyin.com",
                        "path": "/", "secure": True,
                    }],
                },
            )
            assert status == 202 and data["job"]["status"] == "received"
            assert "secret" not in json.dumps(data, ensure_ascii=False)
            assert coordinator.sessions[-1][0]["value"] == "secret"
        finally:
            server.stop()
            controller.close()


def test_desktop_douyin_link_is_pushed_to_paired_extension_for_one_shot_auth() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_desktop_douyin_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = StubCoordinator()
        server = LocalApiServer(controller, coordinator, port=0)
        server.start()
        token = controller.settings["local_api_token"]
        server.origin_policy.authorize(ORIGIN)
        client = socket.create_connection(server.address, timeout=3)
        client_file = client.makefile("rb")
        outcome = {}
        try:
            key = base64.b64encode(b"sixteen-byte-key").decode("ascii")
            client.sendall((
                "GET /v1/extension-events HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{server.address[1]}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Protocol: shengjian-v1, {token}\r\n"
                f"Origin: {ORIGIN}\r\n\r\n"
            ).encode("ascii"))
            response_headers = []
            while True:
                line = client_file.readline()
                response_headers.append(line)
                if line in (b"\r\n", b""):
                    break
            assert response_headers[0].startswith(b"HTTP/1.0 101")
            assert any(line.lower().startswith(b"sec-websocket-protocol: shengjian-v1")
                       for line in response_headers)

            thread = threading.Thread(target=lambda: outcome.setdefault("job", server.submit_douyin_link_from_desktop(
                "https://v.douyin.com/short-id", title="桌面粘贴", transcribe=False, timeout=5,
            )), daemon=True)
            thread.start()
            deadline = time.monotonic() + 2
            request_id = ""
            while time.monotonic() < deadline:
                with server._desktop_request_lock:
                    if server._desktop_download_requests:
                        request_id = next(iter(server._desktop_download_requests))
                        break
                time.sleep(0.01)
            assert request_id
            frame_header = client_file.read(2)
            assert frame_header[0] == 0x81
            frame_length = frame_header[1] & 0x7F
            if frame_length == 126:
                frame_length = int.from_bytes(client_file.read(2), "big")
            elif frame_length == 127:
                frame_length = int.from_bytes(client_file.read(8), "big")
            pushed = json.loads(client_file.read(frame_length).decode("utf-8"))
            assert pushed["type"] == "desktop-douyin-download"
            assert pushed["request_id"] == request_id
            assert pushed["url"] == "https://v.douyin.com/short-id"

            status, claimed, _ = call(
                server, "POST", f"/v1/desktop-downloads/{request_id}/claim",
                token=token, origin=ORIGIN, body={},
            )
            assert status == 200 and claimed["accepted"] is True
            status, submitted, _ = call(
                server, "POST", f"/v1/desktop-downloads/{request_id}",
                token=token, origin=ORIGIN,
                body={
                    "browser_session": [{
                        "name": "sessionid", "value": "secret", "domain": ".douyin.com",
                        "path": "/", "secure": True,
                    }],
                    "user_agent": "test-agent",
                },
            )
            thread.join(timeout=2)
            assert status == 202 and submitted["job"]["status"] == "received"
            assert outcome["job"]["id"] == submitted["job"]["id"]
            assert coordinator.jobs[0]["url"] == "https://v.douyin.com/short-id"
            assert coordinator.jobs[0]["title"] == "桌面粘贴"
            assert coordinator.jobs[0]["options"]["transcribe_after_download"] is False
            assert coordinator.sessions[0][0]["value"] == "secret"
            assert "secret" not in json.dumps(submitted, ensure_ascii=False)

            status, duplicate, _ = call(
                server, "POST", f"/v1/desktop-downloads/{request_id}/claim",
                token=token, origin=ORIGIN, body={},
            )
            assert status == 404 and duplicate["error"]["code"] == "not_found"
        finally:
            client.close()
            server.stop()
            controller.close()


def test_browser_media_hint_is_forwarded_without_being_persisted_in_options() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_media_hint_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = StubCoordinator()
        server = LocalApiServer(controller, coordinator, port=0)
        server.start()
        token = controller.settings["local_api_token"]
        media_url = "https://v3-web.douyinvod.com/video/tos/example.mp4?token=hint"
        media_candidates = [
            media_url,
            "https://v26-web.douyinvod.com/video/tos/backup.mp4?token=hint2",
            "https://v3-web.douyinvod.com/video/tos/index.m3u8?token=hls",
        ]
        try:
            status, data, _ = call(
                server, "POST", "/v1/downloads", token=token, origin=ORIGIN,
                body={
                    "url": "https://www.douyin.com/video/1234567890",
                    "mode": "browser",
                    "media_url": media_url,
                    "media_candidates": media_candidates,
                },
            )
            assert status == 202
            assert coordinator.media_urls[-1] == media_url
            assert coordinator.media_candidates[-1] == media_candidates
            assert media_url not in json.dumps(data["job"]["options"], ensure_ascii=False)
            assert all(candidate not in json.dumps(data["job"]["options"], ensure_ascii=False) for candidate in media_candidates)
        finally:
            server.stop()
            controller.close()


def test_api_cancel_retry_and_batch_partial_acceptance() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_actions_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = StubCoordinator()
        server = LocalApiServer(controller, coordinator, port=0)
        server.start()
        token = controller.settings["local_api_token"]
        try:
            status, data, _ = call(
                server, "POST", "/v1/downloads/batch", token=token,
                body={"items": [
                    {"url": "https://youtu.be/a", "title": "A"},
                    {"url": "https://youtu.be/b", "title": "B"},
                ]},
            )
            assert status == 202 and len(data["accepted"]) == 2
            first_id = data["accepted"][0]["id"]
            assert call(
                server, "POST", f"/v1/downloads/{first_id}/cancel", token=token,
                body={},
            )[1]["job"]["status"] == "cancelled"
            assert call(
                server, "POST", f"/v1/downloads/{first_id}/retry", token=token,
                body={},
            )[1]["job"]["status"] == "received"
            assert call(
                server, "POST", "/v1/downloads/ffffffffffff/cancel", token=token,
                body={},
            )[0] == 404
        finally:
            server.stop()
            controller.close()


def test_api_imports_browser_download_and_queues_transcription() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_import_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = StubCoordinator()
        server = LocalApiServer(controller, coordinator, port=0)
        download_dir = Path(temp) / "ChromeDownloads"
        controller.save_settings({"browser_download_dir": str(download_dir)})
        download_dir.mkdir(parents=True, exist_ok=True)
        media = download_dir / "当前视频标题.mp4"
        media.write_bytes(b"browser-download")
        server.start()
        token = controller.settings["local_api_token"]
        try:
            status, data, _ = call(
                server, "POST", "/v1/imports", token=token, origin=ORIGIN,
                body={"path": str(media), "transcribe": True},
            )
            assert status == 202
            assert data["job"]["job_id"] == "job-local"
            assert data["job"]["path"] == str(media.resolve())

            outside = Path(temp) / "outside.mp4"
            outside.write_bytes(b"outside")
            status, data, _ = call(
                server, "POST", "/v1/imports", token=token, origin=ORIGIN,
                body={"path": str(outside), "transcribe": True},
            )
            assert status == 400
            assert data["error"]["code"] == "invalid_request"
        finally:
            server.stop()
            controller.close()


def test_api_browser_handoff_mode_returns_direct_url_and_links_import() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_browser_handoff_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = StubCoordinator()
        server = LocalApiServer(controller, coordinator, port=0)
        download_dir = Path(temp) / "ChromeDownloads"
        controller.save_settings({"browser_download_dir": str(download_dir)})
        download_dir.mkdir(parents=True, exist_ok=True)
        media = download_dir / "media.mp4"
        media.write_bytes(b"browser-download")
        server.start()
        token = controller.settings["local_api_token"]
        try:
            status, data, _ = call(
                server, "POST", "/v1/downloads", token=token, origin=ORIGIN,
                body={
                    "url": "https://www.douyin.com/video/1234567890",
                    "mode": "browser",
                    "title": "浏览器直提",
                },
            )
            assert status == 202
            assert data["job"]["status"] == "awaiting_browser"
            assert data["job"]["browser_download"]["download_url"].startswith(
                "https://v3-web.douyinvod.com/",
            )
            assert "downloaded_path" not in data["job"]

            status, data, _ = call(
                server, "POST", "/v1/imports", token=token, origin=ORIGIN,
                body={
                    "path": str(media),
                    "title": "浏览器直提",
                    "download_id": data["job"]["id"],
                },
            )
            assert status == 202
            assert data["job"]["download_id"] == coordinator.jobs[-1]["id"]
        finally:
            server.stop()
            controller.close()


def test_browser_resolver_failure_returns_its_persisted_task_to_extension() -> None:
    class FailingResolver:
        def resolve(self, *_args, **_kwargs):
            raise DownloadAdapterError("authorization_required", "需要有效的抖音登录状态")

    with tempfile.TemporaryDirectory(prefix="local_api_browser_resolver_failure_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = DownloadCoordinator(controller, adapter=FailingResolver())
        server = LocalApiServer(controller, coordinator, port=0)
        server.start()
        token = controller.settings["local_api_token"]
        try:
            status, data, _ = call(
                server, "POST", "/v1/downloads", token=token, origin=ORIGIN,
                body={
                    "url": "https://www.douyin.com/video/1234567890",
                    "mode": "browser",
                    "title": "解析授权失败",
                },
            )
            assert status == 202
            assert data["job"]["status"] == "failed"
            assert data["job"]["error"]["code"] == "authorization_required"
            assert data["job"]["error"]["diagnostic"]["phase"] == "media_resolution"
            persisted = controller.downloads.list(statuses=("failed",), limit=10)
            assert len(persisted) == 1
            assert persisted[0]["id"] == data["job"]["id"]
        finally:
            server.stop()
            controller.close()


def test_api_persists_browser_download_interruption_reason() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_browser_interruption_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = StubCoordinator()
        server = LocalApiServer(controller, coordinator, port=0)
        server.start()
        token = controller.settings["local_api_token"]
        try:
            status, created, _ = call(
                server, "POST", "/v1/downloads", token=token, origin=ORIGIN,
                body={
                    "url": "https://www.douyin.com/video/1234567890",
                    "mode": "browser",
                    "title": "浏览器下载中断",
                },
            )
            assert status == 202
            download_id = created["job"]["id"]

            status, data, _ = call(
                server, "POST", f"/v1/downloads/{download_id}/browser-failure",
                token=token, origin=ORIGIN,
                body={
                    "browser_error": "SERVER_FORBIDDEN",
                    "bytes_received": 0,
                    "total_bytes": 12345,
                },
            )

            assert status == 200
            assert data["job"]["status"] == "failed"
            assert data["job"]["bytes_total"] == 12345
            assert data["job"]["error"]["diagnostic"]["browser_error"] == "SERVER_FORBIDDEN"
            assert data["job"]["error"]["diagnostic"]["bytes_received"] == 0
            assert data["job"]["error"]["code"] == "browser_download_interrupted"
        finally:
            server.stop()
            controller.close()


def test_api_persists_extension_failure_reports_idempotently_and_redacts_secrets() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_extension_failure_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = DownloadCoordinator(controller)
        server = LocalApiServer(controller, coordinator, port=0)
        server.start()
        token = controller.settings["local_api_token"]
        body = {
            "event_id": "c" * 32,
            "url": "https://www.douyin.com/video/1234567890?share_token=source-secret",
            "title": "页面授权失败",
            "phase": "extension_authorization",
            "channel": "browser_extension",
            "code": "douyin_session_unavailable",
            "message": "读取 Cookie=private-value 失败；媒体地址 https://cdn.example/video?token=media-secret",
            "retryable": False,
            "diagnostic": {
                "error_name": "TimeoutError",
                "http_status": 403,
                "browser_detail": "GET https://cdn.example/video?token=media-secret",
            },
        }
        try:
            status, first, _ = call(
                server, "POST", "/v1/downloads/failures", token=token, origin=ORIGIN, body=body,
            )
            status_again, again, _ = call(
                server, "POST", "/v1/downloads/failures", token=token, origin=ORIGIN, body=body,
            )

            assert status == status_again == 202
            assert first["job"]["status"] == "failed"
            assert first["job"]["id"] == again["job"]["id"]
            assert first["job"]["url"] == "https://www.douyin.com/video/1234567890"
            assert first["job"]["error"]["diagnostic"]["browser_detail"] == "GET [URL 已脱敏]"
            serialized = json.dumps(first, ensure_ascii=False)
            assert "source-secret" not in serialized
            assert "private-value" not in serialized
            assert "media-secret" not in serialized
            assert len(coordinator.list(limit=50)) == 1
        finally:
            server.stop()
            controller.close()


def test_desktop_cookie_failure_is_written_as_a_download_task() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_desktop_failure_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = DownloadCoordinator(controller)
        server = LocalApiServer(controller, coordinator, port=0)
        server.start()
        token = controller.settings["local_api_token"]
        request_id = "d" * 32
        with server._desktop_request_lock:
            server._desktop_download_requests[request_id] = {
                "id": request_id,
                "url": "https://v.douyin.com/short-id?share_token=source-secret",
                "title": "桌面粘贴失败",
                "transcribe": True,
                "event": threading.Event(),
                "result": None,
                "status": "claimed",
                "created_at": time.monotonic(),
            }
        try:
            status, data, _ = call(
                server, "POST", f"/v1/desktop-downloads/{request_id}/failure",
                token=token, origin=ORIGIN,
                body={
                    "code": "douyin_session_unavailable",
                    "phase": "extension_authorization",
                    "detail": "没有读取到当前会话 Cookie=secret",
                    "error_name": "TimeoutError",
                    "extension_version": "0.12.24",
                },
            )

            assert status == 202 and data["recorded"] is True
            assert data["job"]["status"] == "failed"
            assert data["job"]["title"] == "桌面粘贴失败"
            assert data["job"]["error"]["diagnostic"]["phase"] == "extension_authorization"
            assert "secret" not in json.dumps(data, ensure_ascii=False)
            rows = controller.task_rows()
            assert len(rows) == 1 and rows[0]["kind"] == "download"
        finally:
            server.stop()
            controller.close()


def test_desktop_extension_unavailable_failure_is_written_as_a_download_task() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_desktop_unavailable_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        coordinator = DownloadCoordinator(controller)
        server = LocalApiServer(controller, coordinator, port=0)
        server.start()
        server._notify_extension_clients = lambda _message: 0
        try:
            with pytest.raises(DownloadAdapterError) as raised:
                server.submit_douyin_link_from_desktop(
                    "https://v.douyin.com/short-id?campaign=private",
                    title="扩展离线", transcribe=False,
                )

            download_id = raised.value.download_id
            task = controller.downloads.get(download_id)
            assert task["status"] == "failed"
            assert task["error"]["code"] == "browser_extension_unavailable"
            assert task["error"]["diagnostic"]["phase"] == "desktop_dispatch"
            assert task["url"] == "https://v.douyin.com/short-id"
        finally:
            server.stop()
            controller.close()


def test_pairing_window_issues_token_once_and_replaces_pinned_origin() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_pairing_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        server = LocalApiServer(controller, StubCoordinator(), port=0)
        server.start()
        try:
            status, data, headers = call(
                server, "POST", "/v1/pair", origin=OTHER_ORIGIN, body={},
            )
            assert status == 403 and data["error"]["code"] == "pairing_closed"
            assert headers["Access-Control-Allow-Origin"] == OTHER_ORIGIN

            assert server.begin_pairing(60) == 60
            status, _, headers = call(
                server, "OPTIONS", "/v1/pair", origin=OTHER_ORIGIN,
                headers={"Access-Control-Request-Method": "POST"},
            )
            assert status == 204 and headers["Access-Control-Allow-Origin"] == OTHER_ORIGIN

            status, data, headers = call(
                server, "POST", "/v1/pair", origin=OTHER_ORIGIN, body={},
            )
            assert status == 200
            assert data["token"] == controller.settings["local_api_token"]
            assert headers["Access-Control-Allow-Origin"] == OTHER_ORIGIN
            assert controller.settings_store.load()["extension_origin"] == OTHER_ORIGIN
            assert server.pairing_active is False

            assert call(
                server, "POST", "/v1/pair", origin=OTHER_ORIGIN, body={},
            )[0] == 403
            assert call(
                server, "GET", "/v1/downloads",
                token=data["token"], origin=OTHER_ORIGIN,
            )[0] == 200
            assert call(
                server, "GET", "/v1/downloads",
                token=data["token"], origin=ORIGIN,
            )[0] == 403
        finally:
            server.stop()
            controller.close()


def test_pairing_window_expires() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_pairing_expiry_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        server = LocalApiServer(controller, StubCoordinator(), port=0)
        server.start()
        try:
            with mock.patch("app.local_api.time.monotonic", return_value=100.0):
                assert server.begin_pairing(1) == 1
            with mock.patch("app.local_api.time.monotonic", return_value=102.0):
                status, data, _ = call(
                    server, "POST", "/v1/pair", origin=ORIGIN, body={},
                )
            assert status == 403 and data["error"]["code"] == "pairing_closed"
        finally:
            server.stop()
            controller.close()


def test_one_click_setup_pins_origin_and_returns_runtime_configuration() -> None:
    with tempfile.TemporaryDirectory(prefix="local_api_setup_") as temp:
        controller = logged_in_controller(SettingsStore(base_dir=Path(temp) / "data"))
        configured = []

        def setup():
            configured.append(True)
            return {
                "launch_at_startup": True,
                "background_mode": True,
                "worker_auto_start": True,
            }

        server = LocalApiServer(
            controller, StubCoordinator(), port=0, setup_callback=setup,
        )
        server.start()
        try:
            # R8：/v1/setup 与 pair 一样要求配对窗口已开启
            assert call(server, "POST", "/v1/setup", origin=ORIGIN)[0] == 403
            server.begin_pairing(1)
            status, data, headers = call(
                server, "POST", "/v1/setup", origin=ORIGIN,
                body={"launch_at_startup": True},
            )
            assert status == 200
            assert data["token"] == controller.settings["local_api_token"]
            assert data["setup"]["launch_at_startup"] is True
            assert configured == [True]
            assert headers["Access-Control-Allow-Origin"] == ORIGIN
            assert controller.settings_store.load()["extension_origin"] == ORIGIN
            assert call(
                server, "POST", "/v1/setup", origin=OTHER_ORIGIN, body={},
            )[0] == 403
        finally:
            server.stop()
            controller.close()


def test_imports_reject_unsupported_media_format_as_terminal_415(tmp_path):
    """无扩展名/不支持的文件是永久失败：415 结构化错误让扩展停止重试。

    2026-10 实机：超长标题截断丢掉 ".mp4" 后缀的浏览器下载残留，在扩展
    待导入队列里无限重试，每次失败都向页面弹错误提示。
    """
    controller = logged_in_controller(SettingsStore(base_dir=tmp_path / "data"))

    class UnsupportedFormatCoordinator(StubCoordinator):
        def submit_local_file(self, path, *, title="", transcribe=True, download_id=None, validate_media=False):
            raise MediaTypeError("不支持的媒体格式: <无扩展名>")

    coordinator = UnsupportedFormatCoordinator()
    server = LocalApiServer(controller, coordinator, port=0)
    server.start()
    token = controller.settings["local_api_token"]
    try:
        item = coordinator.submit("https://www.douyin.com/video/1234567890123456", mode="browser", options={"mode": "browser"})
        # 允许根目录内的无后缀文件（实机形态：Downloads\FoilPage\ 下的无后缀
        # 残留经 browser_download_dir 根检查直达导入）：download_dir 在测试
        # 沙箱内，避免污染真实 Downloads。
        stub_file = Path(controller.settings["download_dir"]) / "无后缀残留"
        stub_file.write_bytes(b"not-a-video")
        status, result, _ = call(
            server, "POST", "/v1/imports", token=token, origin=ORIGIN,
            body={"path": str(stub_file), "download_id": item["id"], "browser_download": True, "transcribe": False},
        )
        assert status == 415
        assert result["error"]["code"] == "unsupported_media_format"
    finally:
        server.stop()
        controller.close()


def test_paired_browser_import_accepts_its_custom_output_root_only(tmp_path):
    controller = logged_in_controller(SettingsStore(base_dir=tmp_path / "data"))
    coordinator = StubCoordinator()
    server = LocalApiServer(controller, coordinator, port=0)
    server.start()
    token = controller.settings["local_api_token"]
    try:
        item = coordinator.submit("https://www.douyin.com/video/1234567890123456", mode="browser", options={"mode": "browser"})
        root = tmp_path / "custom-browser-root" / "FoilPage"
        root.mkdir(parents=True)
        media = root / "owned.mp4"
        media.write_bytes(b"media")
        payload = {"path": str(media), "download_id": item["id"], "browser_download": True, "transcribe": False}
        assert call(server,"POST","/v1/imports",token=token,origin=ORIGIN,body=payload)[0] == 202
        assert call(server,"POST","/v1/imports",token=token,origin=ORIGIN,body={**payload,"browser_download":False})[0] == 400
        coordinator.cancel(item["id"])
        assert call(server,"POST","/v1/imports",token=token,origin=ORIGIN,body=payload)[0] == 400
        assert call(server,"POST","/v1/imports",token=token,origin=OTHER_ORIGIN,body=payload)[0] == 403
    finally:
        server.stop();controller.close()


def test_covers_cloud_flag_gates_cover_url_submission(tmp_path):
    """TASK-8.17：covers_enabled 云开关关闭时，扩展上送的封面地址在提交侧剥离。"""
    controller = logged_in_controller(SettingsStore(base_dir=tmp_path / "data"))
    coordinator = StubCoordinator()
    server = LocalApiServer(controller, coordinator, port=0)
    server.start()
    token = controller.settings["local_api_token"]
    cover = "https://p3-sign.douyinpic.com/tos-cn/oQcover.jpeg"
    try:
        status, data, _ = call(
            server, "POST", "/v1/downloads", token=token, origin=ORIGIN,
            body={"url": "https://www.douyin.com/video/1234567890123456",
                  "cover_url": cover, "options": {"mode": "browser"}},
        )
        assert status == 202
        assert coordinator.cover_urls == [""], "内置默认 covers_enabled=False 必须剥离封面地址"

        with mock.patch.object(server.client_config, "covers_enabled", return_value=True):
            status, data, _ = call(
                server, "POST", "/v1/downloads", token=token, origin=ORIGIN,
                body={"url": "https://www.douyin.com/video/2234567890123456",
                      "cover_url": cover, "options": {"mode": "browser"}},
            )
        assert status == 202
        assert coordinator.cover_urls[-1] == cover, "云开关打开后封面地址原样送达协调器"
    finally:
        server.stop()
        controller.close()
