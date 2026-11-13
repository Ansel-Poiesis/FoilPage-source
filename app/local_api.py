# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Authenticated loopback API used by the MV3 browser extension."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import select
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from app.controller import AppController
from app.agent_control import AgentControl, AgentControlError
from app.client_config import ClientConfigStore
from app.download_coordinator import DownloadCoordinator, matching_submission_receipt
from engine.download_store import DownloadNotFoundError, DownloadStateError
from engine.downloader import DownloadAdapterError
from engine.media_store import MediaTypeError
from engine.account_access import AccountLoginRequired
from engine.account_access import require_account_access
from engine.agent_protocol import AGENT_MAX_BODY_BYTES, AGENT_PROTOCOL_VERSION, derive_agent_token

API_VERSION = "1"

_EXTENSION_MANIFEST_CACHE: dict[str, str] = {"version": "", "at": 0.0}


def _bundled_extension_latest() -> str:
    """安装目录扩展 manifest 版本（缓存 30s）：扩展据此自检并自动重载。"""
    now = time.time()
    if now - _EXTENSION_MANIFEST_CACHE["at"] < 30:
        return _EXTENSION_MANIFEST_CACHE["version"]
    import sys
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parents[1]
    version = ""
    try:
        manifest = json.loads((base / "browser-extension" / "manifest.json").read_text(encoding="utf-8"))
        version = str(manifest.get("version") or "")
    except (OSError, ValueError):
        version = ""
    _EXTENSION_MANIFEST_CACHE["version"] = version
    _EXTENSION_MANIFEST_CACHE["at"] = now
    return version
# A validated one-shot Douyin session can contain up to 64 cookies of 4 KiB
# each. Keep the loopback request bounded while allowing that declared limit.
MAX_BODY_BYTES = 384 * 1024
EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")
PAIRING_WINDOW_SECONDS = 120
_CAPTURE_DIAGNOSTIC_REASONS = {
    "video_id_unrecognized",
    "main_world_missing",
    "extension_stale",
    "captured_video_mismatch",
    "detail_response_invalid_json",
    "detail_payload_unrecognized",
    "detail_media_url_rejected",
    "detail_response_without_media",
    "detail_response_unavailable",
    "detail_request_not_seen",
    "player_url_not_ready",
}
_CAPTURE_URL_SOURCES = {"", "player", "captured_media", "play_endpoint"}
_DOWNLOAD_FAILURE_PHASES = {
    "desktop_dispatch", "desktop_authorization", "extension_authorization",
    "local_api_submit", "browser_download_start", "browser_download_interrupted",
    "browser_download_import", "resolver_fallback",
}
_DOWNLOAD_FAILURE_CHANNELS = {
    "desktop_bridge", "browser_extension", "chrome.downloads", "local_api",
}


def _sanitize_capture_diagnostic(body: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded capture-stage facts; never persist user/page URLs or secrets."""
    outcome = body.get("outcome")
    reason = body.get("reason", "")
    url_source = body.get("url_source", "")
    if not isinstance(outcome, str) or outcome not in {"url_missing", "url_captured"}:
        raise ValueError("采集诊断结果无效")
    if not isinstance(reason, str) or (reason and reason not in _CAPTURE_DIAGNOSTIC_REASONS):
        raise ValueError("采集诊断原因无效")
    if (outcome == "url_missing" and not reason) or (outcome == "url_captured" and reason):
        raise ValueError("采集诊断阶段不一致")
    if not isinstance(url_source, str) or url_source not in _CAPTURE_URL_SOURCES:
        raise ValueError("采集诊断来源无效")
    if (outcome == "url_missing" and url_source) or (outcome == "url_captured" and not url_source):
        raise ValueError("采集诊断地址来源不一致")

    def bounded_int(name: str, maximum: int) -> int:
        value = body.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError(f"采集诊断字段无效: {name}")
        return value

    def flag(name: str, *, nullable: bool = False) -> bool | None:
        value = body.get(name, False)
        if nullable and value is None:
            return None
        if not isinstance(value, bool):
            raise ValueError(f"采集诊断字段无效: {name}")
        return value

    version = body.get("main_world_version", "")
    if not isinstance(version, str) or len(version) > 24 or (version and not re.fullmatch(r"\d+(?:\.\d+){1,3}", version)):
        raise ValueError("采集诊断扩展版本无效")
    return {
        "outcome": outcome,
        "reason": reason,
        "elapsed_ms": bounded_int("elapsed_ms", 30000),
        "main_world_present": flag("main_world_present"),
        "main_world_stale": flag("main_world_stale"),
        "main_world_version": version,
        "detail_requests": bounded_int("detail_requests", 1000000),
        "detail_responses": bounded_int("detail_responses", 1000000),
        "detail_json_responses": bounded_int("detail_json_responses", 1000000),
        "detail_json_failures": bounded_int("detail_json_failures", 1000000),
        "media_items": bounded_int("media_items", 1000000),
        "response_aweme_nodes": bounded_int("response_aweme_nodes", 1000000),
        "response_video_nodes": bounded_int("response_video_nodes", 1000000),
        "response_play_addr_nodes": bounded_int("response_play_addr_nodes", 1000000),
        "response_url_candidates": bounded_int("response_url_candidates", 1000000),
        "response_https_candidates": bounded_int("response_https_candidates", 1000000),
        "response_allowed_urls": bounded_int("response_allowed_urls", 1000000),
        # 封面取证（2026-11-01）：扩展 1.3.7 起上送；旧扩展缺省 0 不影响。
        "response_cover_nodes": bounded_int("response_cover_nodes", 1000000),
        "response_cover_accepted": bounded_int("response_cover_accepted", 1000000),
        "captured_targets": bounded_int("captured_targets", 20),
        "target_captured": flag("target_captured"),
        "target_cover_held": flag("target_cover_held"),
        "unbound_capture": flag("unbound_capture"),
        "player_present": flag("player_present"),
        "player_ready_state": bounded_int("player_ready_state", 4),
        "player_paused": flag("player_paused", nullable=True),
        "url_source": url_source,
    }


def _safe_failure_text(value: object, *, limit: int = 500) -> str:
    """Keep actionable error text while stripping URLs and credential-like values."""
    if not isinstance(value, str):
        return ""
    text = " ".join("".join(ch for ch in value if ch >= " " or ch in "\t\n").split())
    text = re.sub(r"https?://[^\s<>\"']+", "[URL 已脱敏]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)(cookie|authorization|token|signature|x-bogus|mstoken|verifyfp)\s*[:=]\s*[^\s,;&]+",
        r"\1=[已脱敏]", text,
    )
    return text[:limit]


def _safe_failure_page_url(value: object) -> str:
    """Retain only a supported source-page URL, dropping tracking/signed queries."""
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("失败记录视频链接无效")
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("失败记录视频链接无效") from exc
    domains = ("douyin.com", "bilibili.com", "b23.tv", "youtube.com", "youtu.be")
    if (
        parsed.scheme.lower() != "https" or not host or parsed.username or parsed.password
        or port is not None
        or not any(host == domain or host.endswith("." + domain) for domain in domains)
    ):
        raise ValueError("失败记录只接受已支持平台的视频页链接")
    safe_netloc = host
    video_id = parse_qs(parsed.query).get("v", [""])[0] if host.endswith("youtube.com") else ""
    query = f"?v={video_id}" if re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id) else ""
    return urlsplit(f"https://{safe_netloc}{parsed.path}{query}").geturl()[:4096]


def _sanitize_download_failure(body: dict[str, Any]) -> dict[str, Any]:
    """Validate a failure report from the paired extension; never accept media URLs or cookies."""
    event_id = body.get("event_id", "")
    if not isinstance(event_id, str) or not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("失败记录编号无效")
    task_id = body.get("task_id", "")
    if task_id and (not isinstance(task_id, str) or not re.fullmatch(r"[a-f0-9]{12}", task_id)):
        raise ValueError("下载任务编号无效")
    url = _safe_failure_page_url(body.get("url", ""))
    title = body.get("title", "")
    if not isinstance(title, str):
        raise ValueError("失败记录标题无效")
    phase = body.get("phase", "")
    if not isinstance(phase, str) or phase not in _DOWNLOAD_FAILURE_PHASES:
        raise ValueError("失败阶段无效")
    channel = body.get("channel", "browser_extension")
    if not isinstance(channel, str) or channel not in _DOWNLOAD_FAILURE_CHANNELS:
        raise ValueError("失败通道无效")
    code = body.get("code", "extension_error")
    if not isinstance(code, str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", code):
        raise ValueError("失败代码无效")
    message = _safe_failure_text(body.get("message", "")) or "扩展未能完成视频下载。"
    retryable = body.get("retryable", False)
    if not isinstance(retryable, bool):
        raise ValueError("重试标记无效")

    raw = body.get("diagnostic", {})
    if not isinstance(raw, dict):
        raise ValueError("失败诊断必须是对象")
    diagnostic: dict[str, Any] = {"event_id": event_id, "phase": phase, "channel": channel}
    int_fields = {
        "http_status": 599, "response_status": 599, "bytes_received": 2**53 - 1,
        "total_bytes": 2**53 - 1, "elapsed_ms": 300000, "candidate_count": 8,
        "attempt": 100,
    }
    for name, maximum in int_fields.items():
        value = raw.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
            raise ValueError(f"失败诊断字段无效: {name}")
        if name in {"http_status", "response_status"} and value and value < 100:
            raise ValueError(f"失败诊断字段无效: {name}")
        diagnostic[name] = value
    for name in ("browser_error", "error_name", "resolver_code", "fallback_code", "extension_version"):
        value = raw.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > 80:
            raise ValueError(f"失败诊断字段无效: {name}")
        diagnostic[name] = _safe_failure_text(value, limit=80)
    for name in ("browser_detail", "resolver_detail", "fallback_detail"):
        value = raw.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > 1000:
            raise ValueError(f"失败诊断字段无效: {name}")
        diagnostic[name] = _safe_failure_text(value, limit=500)

    return {
        "event_id": event_id,
        "task_id": task_id,
        "url": url,
        "title": title.strip()[:240],
        "phase": phase,
        "channel": channel,
        "code": code.lower(),
        "message": message,
        "retryable": retryable,
        "diagnostic": diagnostic,
    }


def public_download_job(item: dict) -> dict:
    data = dict(item)
    data["download_complete"] = bool(data.pop("downloaded_path", None))
    data.pop("cancel_requested", None)
    data["options"] = {key: value for key, value in (data.get("options") or {}).items() if key not in {"managed_download_root", "download_content_sha256"}}
    return data


def _websocket_frame(payload: bytes, *, opcode: int = 0x1) -> bytes:
    """Encode a small, unmasked server-to-client WebSocket frame."""
    length = len(payload)
    if length < 126:
        header = bytes((0x80 | opcode, length))
    elif length <= 0xFFFF:
        header = bytes((0x80 | opcode, 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((0x80 | opcode, 127)) + length.to_bytes(8, "big")
    return header + payload


def _read_websocket_client_frame(client: socket.socket) -> bytes | None:
    """Read one bounded, masked browser frame; None means a close/bad frame."""
    def read_exact(length: int) -> bytes | None:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = client.recv(length - len(chunks))
            if not chunk:
                return None
            chunks.extend(chunk)
        return bytes(chunks)

    header = read_exact(2)
    if header is None:
        return None
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F
    if length == 126:
        extended = read_exact(2)
        if extended is None:
            return None
        length = int.from_bytes(extended, "big")
    elif length == 127:
        extended = read_exact(8)
        if extended is None:
            return None
        length = int.from_bytes(extended, "big")
    if opcode == 0x8 or not masked or length > 4096:
        return None
    mask = read_exact(4)
    payload = read_exact(length)
    if mask is None or payload is None:
        return None
    return bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


class OriginPolicy:
    def __init__(self, origin: str = "", *, pin: Callable[[str], None] | None = None) -> None:
        self._origin = origin.strip()
        self._pin = pin or (lambda _origin: None)
        self._lock = threading.Lock()

    @property
    def origin(self) -> str:
        with self._lock:
            return self._origin

    def allows_preflight(self, origin: str) -> bool:
        if not EXTENSION_ORIGIN.fullmatch(origin or ""):
            return False
        pinned = self.origin
        return not pinned or secrets.compare_digest(origin, pinned)

    def authorize(self, origin: str) -> bool:
        if not origin:
            return True
        if not EXTENSION_ORIGIN.fullmatch(origin):
            return False
        with self._lock:
            if self._origin:
                return secrets.compare_digest(origin, self._origin)
            self._origin = origin
            self._pin(origin)
            return True

    def replace(self, origin: str) -> None:
        if not EXTENSION_ORIGIN.fullmatch(origin):
            raise ValueError("扩展来源无效")
        with self._lock:
            self._origin = origin
            self._pin(origin)


class _LoopbackHttpServer(ThreadingHTTPServer):
    # The default backlog of five drops short concurrent extension/API bursts
    # on Windows before the authenticated handler gets to answer them.
    request_queue_size = 64


class LocalApiServer:
    def _require_work_account(self) -> dict:
        return require_account_access(dict(self.controller.settings), self.controller.settings_store.base_dir)

    def __init__(
        self,
        controller: AppController,
        coordinator: DownloadCoordinator | None = None,
        *,
        host: str = "127.0.0.1",
        port: int | None = None,
        setup_callback: Callable[[], dict[str, Any]] | None = None,
        show_callback: Callable[..., None] | None = None,
        quit_callback: Callable[[], None] | None = None,
        desktop_login_callback: Callable[[str], dict] | None = None,
        agent_operations=None,
        app_version: str = "",
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("本地 API 只能监听 127.0.0.1")
        self.controller = controller
        self.coordinator = coordinator or DownloadCoordinator(controller)
        self.host = host
        self.port = int(port if port is not None else controller.settings["local_api_port"])
        self.setup_callback = setup_callback or self._default_setup
        self.show_callback = show_callback
        self.quit_callback = quit_callback
        self.desktop_login_callback = desktop_login_callback
        self.token = controller.settings["local_api_token"]
        self.agent_token = derive_agent_token(self.token)
        self.agent_control = AgentControl(controller, self.coordinator, operations=agent_operations)
        self.origin_policy = OriginPolicy(
            controller.settings.get("extension_origin", ""), pin=self._pin_origin,
        )
        self.client_config = ClientConfigStore(
            controller.settings_store.base_dir,
            server_url=lambda: str(controller.settings.get("subscription_server_url") or ""),
            app_version=app_version,
        )
        # TASK-8.17：热切换配置注入协调器——covers_enabled 云开关在下载侧
        # 复核（提交侧的剥离在 /v1/downloads 处理器内）。
        self.coordinator.client_config = self.client_config
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._pairing_lock = threading.Lock()
        self._pairing_until = 0.0
        self._extension_lock = threading.Lock()
        self._extension_clients: dict[socket.socket, threading.Lock] = {}
        self._desktop_request_lock = threading.Lock()
        self._desktop_download_requests: dict[str, dict[str, Any]] = {}

    @property
    def address(self) -> tuple[str, int]:
        if self._server:
            return self._server.server_address[:2]
        return self.host, self.port

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def pairing_active(self) -> bool:
        with self._pairing_lock:
            return time.monotonic() < self._pairing_until

    def begin_pairing(self, seconds: int = PAIRING_WINDOW_SECONDS) -> int:
        duration = max(1, min(int(seconds), PAIRING_WINDOW_SECONDS))
        with self._pairing_lock:
            self._pairing_until = time.monotonic() + duration
        return duration

    def _claim_pairing(self, origin: str) -> bool:
        if not EXTENSION_ORIGIN.fullmatch(origin or ""):
            return False
        with self._pairing_lock:
            if time.monotonic() >= self._pairing_until:
                return False
            self._pairing_until = 0.0
        self.origin_policy.replace(origin)
        return True

    def start(self) -> None:
        if self._server:
            return
        handler = self._handler_type()
        # Bind before starting background work: a second instance/occupied port
        # must not leave an unowned downloader running after startup fails.
        server = _LoopbackHttpServer((self.host, self.port), handler)
        server.daemon_threads = True
        try:
            self.coordinator.start()
        except Exception:
            server.server_close()
            self.coordinator.stop()
            raise
        self._server = server
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="local-browser-api", daemon=True,
        )
        self._thread.start()
        self.client_config.start()

    def stop(self, timeout: float = 3.0) -> None:
        server = self._server
        self._server = None
        self.client_config.stop()
        with self._desktop_request_lock:
            pending = list(self._desktop_download_requests.values())
            self._desktop_download_requests.clear()
        for request in pending:
            request["result"] = {"ok": False, "code": "service_stopped", "error": "FoilPage 本地服务已停止"}
            request["event"].set()
        with self._extension_lock:
            clients = list(self._extension_clients)
            self._extension_clients.clear()
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass
        if server:
            server.shutdown()
            server.server_close()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.coordinator.stop(timeout=timeout)

    def submit_douyin_link_from_desktop(
        self, url: str, *, title: str = "", transcribe: bool = True,
        timeout: float = 45.0,
    ) -> dict[str, Any]:
        """Ask the paired extension for a one-click, in-memory Douyin session."""
        try:
            self._require_work_account()
        except AccountLoginRequired as exc:
            raise DownloadAdapterError(exc.code, str(exc)) from None
        if not self.running:
            raise DownloadAdapterError("service_unavailable", "FoilPage 本地服务未运行")
        request_id = secrets.token_hex(16)
        pending = {
            "id": request_id,
            "url": str(url)[:4096],
            "title": str(title or "").strip()[:240],
            "transcribe": bool(transcribe),
            "event": threading.Event(),
            "result": None,
            "status": "waiting",
            "created_at": time.monotonic(),
        }
        with self._desktop_request_lock:
            # Stale requests should not survive a caller that was interrupted.
            cutoff = time.monotonic() - 90
            for old_id, old in tuple(self._desktop_download_requests.items()):
                if old["created_at"] < cutoff:
                    self._desktop_download_requests.pop(old_id, None)
            self._desktop_download_requests[request_id] = pending

        sent = self._notify_extension_clients({
            "type": "desktop-douyin-download",
            "request_id": request_id,
            "url": pending["url"],
            "title": pending["title"],
            "transcribe": pending["transcribe"],
        })
        if not sent:
            with self._desktop_request_lock:
                self._desktop_download_requests.pop(request_id, None)
            failure = self._persist_desktop_download_failure(
                request_id, pending, "browser_extension_unavailable",
                "未发现已连接的 FoilPage 浏览器扩展。请打开已配对的 Chrome/Edge 后重试。",
                phase="desktop_dispatch",
            )
            error = DownloadAdapterError(
                "browser_extension_unavailable",
                "未发现已连接的 FoilPage 浏览器扩展。请打开已配对的 Chrome/Edge 后重试。",
            )
            if failure:
                error.download_id = failure.get("id", "")
            raise error
        if not pending["event"].wait(max(1.0, min(float(timeout), 90.0))):
            with self._desktop_request_lock:
                self._desktop_download_requests.pop(request_id, None)
            failure = self._persist_desktop_download_failure(
                request_id, pending, "browser_extension_timeout",
                "等待浏览器扩展授权超时；请确认扩展已连接并且已登录抖音。",
                phase="desktop_authorization",
            )
            error = DownloadAdapterError(
                "browser_extension_timeout",
                "等待浏览器授权超时。请确认已配对扩展可用、浏览器已登录抖音后重试。",
            )
            if failure:
                error.download_id = failure.get("id", "")
            raise error
        with self._desktop_request_lock:
            self._desktop_download_requests.pop(request_id, None)
        result = pending.get("result") or {}
        if not result.get("ok"):
            error = DownloadAdapterError(
                str(result.get("code") or "browser_extension_failed"),
                str(result.get("error") or "浏览器未能授权抖音下载"),
            )
            error.download_id = str((result.get("job") or {}).get("id") or "")
            raise error
        return result["job"]

    def _persist_desktop_download_failure(
        self,
        event_id: str,
        pending: dict[str, Any],
        code: str,
        message: str,
        *,
        phase: str,
        diagnostic: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Keep desktop-paste failures in task history, even before a job exists."""
        recorder = getattr(self.coordinator, "record_extension_failure", None)
        if not callable(recorder):
            return None
        try:
            return recorder(
                url=_safe_failure_page_url(pending.get("url", "")), title=pending.get("title", ""),
                event_id=event_id, phase=phase, channel="desktop_bridge",
                code=code, message=_safe_failure_text(message) or "桌面链接下载未能启动。",
                diagnostic=diagnostic or {}, retryable=False,
            )
        except Exception:
            import logging
            logging.getLogger("shengjian.desktop").exception(
                "desktop Douyin failure could not be persisted",
            )
            return None

    def _notify_extension_clients(self, message: dict[str, Any]) -> int:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        frame = _websocket_frame(payload)
        with self._extension_lock:
            clients = tuple(self._extension_clients.items())
        sent = 0
        for client, write_lock in clients:
            try:
                with write_lock:
                    client.sendall(frame)
                sent += 1
            except OSError:
                self._remove_extension_client(client)
        return sent

    def _register_extension_client(self, client: socket.socket) -> threading.Lock:
        write_lock = threading.Lock()
        with self._extension_lock:
            self._extension_clients[client] = write_lock
        with self._desktop_request_lock:
            pending = [item.copy() for item in self._desktop_download_requests.values()
                       if item["status"] == "waiting"]
        for item in pending:
            message = {
                "type": "desktop-douyin-download",
                "request_id": item["id"],
                "url": item["url"],
                "title": item["title"],
                "transcribe": item["transcribe"],
            }
            try:
                frame = _websocket_frame(json.dumps(
                    message, ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8"))
                with write_lock:
                    client.sendall(frame)
            except OSError:
                self._remove_extension_client(client)
                break
        return write_lock

    def _remove_extension_client(self, client: socket.socket) -> None:
        with self._extension_lock:
            self._extension_clients.pop(client, None)

    def _complete_desktop_download(
        self, request_id: str, body: dict[str, Any], *, failed: bool = False,
    ) -> dict[str, Any]:
        with self._desktop_request_lock:
            pending = self._desktop_download_requests.get(request_id)
            allowed_statuses = {"waiting", "claimed"} if failed else {"claimed"}
            if pending is None or pending["status"] not in allowed_statuses:
                raise DownloadNotFoundError(request_id)
            pending["status"] = "processing"

        if failed:
            code = body.get("code")
            messages = {
                "douyin_session_unavailable": "未取得抖音登录授权；请在已配对浏览器登录抖音后重试。",
                "extension_error": "浏览器扩展未能完成抖音授权，请刷新扩展后重试。",
            }
            safe_code = code if code in messages else "extension_error"
            detail = _safe_failure_text(body.get("detail", ""))
            phase = body.get("phase")
            if not isinstance(phase, str) or phase not in _DOWNLOAD_FAILURE_PHASES:
                phase = "desktop_authorization"
            failure_message = messages[safe_code]
            if detail and detail not in failure_message:
                failure_message = f"{failure_message} 扩展详情：{detail}"
            report = _sanitize_download_failure({
                "event_id": request_id,
                "url": pending["url"],
                "title": pending["title"],
                "phase": phase,
                "channel": "browser_extension",
                "code": safe_code,
                "message": failure_message,
                "retryable": False,
                "diagnostic": {
                    "error_name": str(body.get("error_name") or "")[:80],
                    "browser_detail": detail,
                    "extension_version": str(body.get("extension_version") or "")[:80],
                },
            })
            job = None
            recorder = getattr(self.coordinator, "record_extension_failure", None)
            if callable(recorder):
                try:
                    job = recorder(**report)
                except Exception:
                    import logging
                    logging.getLogger("shengjian.desktop").exception(
                        "desktop extension failure could not be persisted",
                    )
            result = {
                "ok": False, "code": safe_code, "error": failure_message,
                **({"job": public_download_job(job)} if job else {}),
            }
            with self._desktop_request_lock:
                pending["result"] = result
                pending["status"] = "done"
                pending["event"].set()
            return result

        try:
            user_agent = body.get("user_agent", "")
            if not isinstance(user_agent, str):
                raise ValueError("浏览器信息无效")
            options = {
                "quality": "best-available",
                "audio_only": pending["transcribe"],
                "browser_fallback": True,
                "transcribe_after_download": pending["transcribe"],
                "language": "zh",
                "referer": pending["url"],
                "user_agent": user_agent[:512],
            }
            job = self.coordinator.submit(
                pending["url"], title=pending["title"], options=options,
                session_cookies=body.get("browser_session"), mode="local",
            )
            result = {"ok": True, "job": public_download_job(job)}
        except (DownloadAdapterError, DownloadStateError, ValueError) as exc:
            result = {
                "ok": False,
                "code": getattr(exc, "code", "invalid_request"),
                "error": str(exc)[:500],
            }
        except Exception:
            result = {"ok": False, "code": "internal_error", "error": "抖音下载任务未能创建"}
        finally:
            # Drop the request-body's references as soon as the coordinator has
            # copied validated cookies into its one-task transient session map.
            if isinstance(body.get("browser_session"), list):
                body["browser_session"].clear()
            body.pop("browser_session", None)
        with self._desktop_request_lock:
            pending["result"] = result
            pending["status"] = "done"
            pending["event"].set()
        return result

    def _claim_desktop_download(self, request_id: str) -> bool:
        with self._desktop_request_lock:
            pending = self._desktop_download_requests.get(request_id)
            if pending is None or pending["status"] != "waiting":
                return False
            pending["status"] = "claimed"
            pending["claimed_at"] = time.monotonic()
            return True

    def _pin_origin(self, origin: str) -> None:
        self.controller.save_settings({"extension_origin": origin})

    def _default_setup(self) -> dict[str, Any]:
        self.controller.save_settings({
            "launch_at_startup": True,
            "worker_auto_start": True,
            "worker_auto_restart": True,
        })
        return {
            "launch_at_startup": True,
            "background_mode": True,
            "worker_auto_start": True,
        }

    def _handler_type(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            # "VideoToArticle" 是扩展 api.js 校验的握手协议标识（wire identifier），
            # 与磁盘品牌名无关；改名会拒绝所有未重载的旧扩展页面，保持不变。
            server_version = "VideoToArticleLocal/1"

            def log_message(self, _format: str, *_args) -> None:
                return

            def _desktop_login_origin(self) -> bool:
                return (self.headers.get_all('Origin') == ['https://foilpage.early-snipe-7202.chatgpt.site']
                        and self.headers.get_all('Host') == [f'127.0.0.1:{owner.address[1]}']
                        and not urlsplit(self.path).query)

            def do_OPTIONS(self) -> None:
                origin = self.headers.get("Origin", "")
                path = urlsplit(self.path).path
                if path == '/v1/desktop-login/proof':
                    if not self._desktop_login_origin():
                        self._error(403,'origin_denied','来源未授权')
                        return
                    self.send_response(204)
                    self._cors(origin)
                    self.send_header('Access-Control-Allow-Methods','POST, OPTIONS')
                    self.send_header('Access-Control-Allow-Headers','Content-Type')
                    self.send_header('Access-Control-Allow-Private-Network','true')
                    self.end_headers()
                    return
                if path == "/v1/agent/call":
                    self._error(403, "browser_denied", "Agent 接口仅供本机原生客户端使用")
                    return
                pairing_allowed = (
                    path == "/v1/pair" and EXTENSION_ORIGIN.fullmatch(origin or "")
                )
                if not pairing_allowed and not owner.origin_policy.allows_preflight(origin):
                    self._json(403, {"error": {"code": "origin_denied", "message": "扩展来源未授权"}})
                    return
                self.send_response(204)
                self._cors(origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Max-Age", "600")
                self.end_headers()

            def do_GET(self) -> None:
                path = urlsplit(self.path)
                if path.path == "/health":
                    self._json(200, {
                        "ok": True, "service": "VideoToArticle",
                        "api_version": API_VERSION, "auth_required": True,
                        "configured": bool(owner.origin_policy.origin),
                        "extension_latest": _bundled_extension_latest(),
                        "client_config": owner.client_config.snapshot(),
                        "agent_control": {"protocol_version": AGENT_PROTOCOL_VERSION,
                                          "endpoint": "/v1/agent/call"},
                    })
                    return
                if path.path == "/v1/extension-events":
                    self._extension_events()
                    return
                if not self._authorized():
                    return
                if path.path == "/v1/downloads":
                    query = parse_qs(path.query)
                    statuses = tuple(filter(None, query.get("status", [""])[0].split(",")))
                    try:
                        limit = int(query.get("limit", ["100"])[0])
                        if not 1 <= limit <= 500:
                            raise ValueError
                        jobs = owner.coordinator.list(
                            statuses=statuses or None, limit=limit,
                        )
                        # 扩展提交前预检（先生 2026-10-08）：按规范 URL 精确
                        # 过滤，供「同视频重复下载先确认」判定 active/done。
                        url_filter = (query.get("url", [""])[0] or "").strip()
                        if url_filter:
                            jobs = [item for item in jobs if item.get("url") == url_filter]
                    except ValueError:
                        self._error(400, "invalid_limit", "limit 必须是 1 到 500 的整数")
                        return
                    except DownloadStateError as exc:
                        self._error(400, "invalid_status", str(exc))
                        return
                    self._json(200, {"jobs": [
                        public_download_job(item) for item in jobs
                    ]})
                    return
                match = re.fullmatch(r"/v1/downloads/([a-f0-9]{12})(?:/(result))?", path.path)
                if match:
                    item = owner.coordinator.get(match.group(1))
                    if item is None:
                        self._error(404, "not_found", "下载任务不存在")
                        return
                    data = public_download_job(item)
                    if match.group(2):
                        data = {
                            "id": item["id"], "status": item["status"],
                            "media_id": item.get("media_id"), "job_id": item.get("job_id"),
                        }
                    self._json(200, {"job": data})
                    return
                self._error(404, "not_found", "接口不存在")

            def do_POST(self) -> None:
                path = urlsplit(self.path).path
                if path == '/v1/desktop-login/proof':
                    if not self._desktop_login_origin():
                        self._error(403,'origin_denied','来源未授权')
                        return
                    try:
                        lengths = self.headers.get_all('Content-Length',[])
                        if len(lengths)!=1 or not re.fullmatch(r'[0-9]{1,4}',lengths[0]) or int(lengths[0])>1024:
                            raise ValueError('body size')
                        body = self._agent_body()
                        if set(body) != {'request_id'} or not re.fullmatch(r'desktop-[a-f0-9]{64}',str(body['request_id'])):
                            raise ValueError('request')
                        result = owner.desktop_login_callback(body['request_id']) if owner.desktop_login_callback else {'ok':False}
                        self._json(200 if result.get('ok') else 409,result)
                    except (ValueError,AgentControlError):
                        self._error(400,'invalid_request','本机登录请求无效')
                    return
                if path == "/v1/agent/call":
                    self._agent_call()
                    return
                if path == "/v1/show":
                    # 唤起窗口允许未配对调用（先生要求插件能唤起应用直达连接页）：
                    # 本机无 Origin 的调用（manage.ps1 等工具）只弹窗不带页面；
                    # 带页面参数必须来自钉扎的浏览器扩展 Origin。
                    page = ""
                    origin = self.headers.get("Origin", "")
                    if origin and EXTENSION_ORIGIN.fullmatch(origin):
                        try:
                            page = str(self._body().get("page") or "")[:32]
                        except Exception:
                            page = ""
                    if owner.show_callback is not None:
                        owner.show_callback(page)
                    self._json(200, {"shown": owner.show_callback is not None})
                    return
                if path == "/v1/pair":
                    origin = self.headers.get("Origin", "")
                    if not owner._claim_pairing(origin):
                        self._error(403, "pairing_closed", "请先在 FoilPage 设置中允许连接")
                        return
                    self._json(200, {"token": owner.token, "api_version": API_VERSION})
                    return
                if path == "/v1/setup":
                    origin = self.headers.get("Origin", "")
                    if not EXTENSION_ORIGIN.fullmatch(origin or ""):
                        self._error(403, "origin_denied", "只接受 Chrome 扩展配置请求")
                        return
                    # Setup shares the same atomic one-shot window as pair;
                    # a separate active check and later reset can admit two callers.
                    pinned = owner.origin_policy.origin
                    if pinned and not secrets.compare_digest(origin, pinned):
                        self._error(403, "origin_denied", "已有其他扩展与 FoilPage 连接")
                        return
                    if not owner._claim_pairing(origin):
                        self._error(403, "pairing_closed", "请先在 FoilPage 设置中允许连接")
                        return
                    setup = owner.setup_callback()
                    self._json(200, {
                        "token": owner.token,
                        "api_version": API_VERSION,
                        "setup": setup,
                    })
                    return
                if not self._authorized():
                    return
                try:
                    body = self._body()
                    desktop_request = re.fullmatch(
                        r"/v1/desktop-downloads/([a-f0-9]{32})(?:/(claim|failure))?", path,
                    )
                    if desktop_request:
                        action = desktop_request.group(2)
                        if action == "claim":
                            if not owner._claim_desktop_download(desktop_request.group(1)):
                                raise DownloadNotFoundError(desktop_request.group(1))
                            self._json(200, {"accepted": True})
                            return
                        result = owner._complete_desktop_download(
                            desktop_request.group(1), body,
                            failed=action == "failure",
                        )
                        if result.get("ok"):
                            self._json(202, {"job": result["job"]})
                        elif action == "failure" and result.get("job"):
                            # This is a successful write of a failed task. Return
                            # 202 so the extension does not retry the same report
                            # as an unrelated second incident.
                            self._json(202, {
                                "recorded": True,
                                "job": result["job"],
                                "failure": {"code": result.get("code"), "message": result.get("error")},
                            })
                        else:
                            # 提交被拒必须留痕（4xx 不进 ERROR 日志）：扩展侧
                            # 只见"未能创建"时，据此定位真实拒绝码（2026-10-14）。
                            import logging
                            logging.getLogger("shengjian.desktop").info(
                                "download_submit_rejected code=%s url=%s",
                                str(result.get("code") or "invalid_request"),
                                str(body.get("url", ""))[:120])
                            self._error(400, str(result.get("code") or "invalid_request"),
                                        str(result.get("error") or "抖音下载任务未能创建"))
                        return
                    if path == "/v1/diagnostics/douyin-capture":
                        diagnostic = _sanitize_capture_diagnostic(body)
                        import logging
                        logging.getLogger("shengjian.desktop").info(
                            "douyin_capture_diagnostic %s",
                            json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":")),
                        )
                        self._json(202, {"recorded": True})
                        return
                    if path == "/v1/downloads/failures":
                        report = _sanitize_download_failure(body)
                        if report["code"] in {"login_required"} and not report.get("task_id"):
                            self._json(202, {"recorded": False, "reason": report["code"]})
                            return
                        item = owner.coordinator.record_extension_failure(**report)
                        self._json(202, {"recorded": True, "job": public_download_job(item)})
                        return
                    if path == "/v1/downloads":
                        if "cancel_request_id" in body:
                            cancelled = owner.coordinator.cancel_submission(body["cancel_request_id"])
                            self._json(202, {"cancelled": True, "job": public_download_job(cancelled) if cancelled else None})
                            return
                        download_options = body.get("options")
                        if download_options is not None and not isinstance(download_options, dict):
                            raise ValueError("下载选项必须是对象")
                        if body.get("client_request_id") is not None:
                            download_options = {**(download_options or {}), "client_request_id": body["client_request_id"]}
                        request_id = (download_options or {}).get("client_request_id", "")
                        if not isinstance(request_id, str) or (request_id and not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", request_id)):
                            raise DownloadAdapterError("invalid_client_request_id", "提交凭据格式无效")
                        receipt = owner.controller.downloads.find_by_client_request_id(request_id)
                        if receipt is not None:
                            matching_submission_receipt(receipt, body.get("url", ""),
                                (download_options or {}).get("transcribe_after_download", bool(owner.controller.settings.get("transcribe_after_download", False))),
                                allow_any_url=bool(owner.controller.settings.get("allow_any_url", False)))
                            item = owner.coordinator.get(receipt["id"]) or receipt
                            self._json(202, {"job": public_download_job(item)})
                            return
                        owner._require_work_account()
                        # TASK-8.17 封面云开关（client-config covers_enabled）：
                        # 关闭时不收扩展上送的封面地址，任务与现状完全一致。
                        submitted_cover_url = ""
                        if owner.client_config.covers_enabled():
                            submitted_cover_url = str(body.get("cover_url") or "")
                        # TASK-8.18 图集云开关：关闭时整组载荷就地拒收（不透传）。
                        submitted_gallery = None
                        if owner.client_config.gallery_enabled():
                            submitted_gallery = body.get("gallery_images")
                            if submitted_gallery is not None:
                                if (
                                    not isinstance(submitted_gallery, list)
                                    or not 1 <= len(submitted_gallery) <= 60
                                    or any(
                                        not isinstance(row, list) or not 1 <= len(row) <= 8
                                        or any(
                                            not isinstance(url, str) or not url or len(url) > 4096
                                            for url in row
                                        )
                                        for row in submitted_gallery
                                    )
                                ):
                                    raise DownloadAdapterError("invalid_gallery", "图集地址列表无效")
                        item = owner.coordinator.submit(
                            body.get("url", ""), title=body.get("title", ""),
                            options=download_options,
                            session_cookies=body.get("browser_session"),
                            media_url=body.get("media_url", ""),
                            media_candidates=body.get("media_candidates"),
                            mode=str(body.get("mode") or "local"),
                            cover_url=submitted_cover_url,
                            gallery_images=submitted_gallery,
                        )
                        self._json(202, {"job": public_download_job(item)})
                        return
                    if path == "/v1/imports":
                        download_id = str(body.get("download_id") or "")
                        linked = owner.coordinator.get(download_id) if re.fullmatch(r"[a-f0-9]{12}", download_id) else None
                        continuation = (
                            body.get("browser_download") is True and linked is not None
                            and linked["status"] in {"awaiting_browser", "done", "cancelled", "failed"}
                            and linked["options"].get("mode") == "browser"
                        )
                        authorized = True
                        try:
                            owner._require_work_account()
                        except AccountLoginRequired:
                            if not continuation:
                                raise
                            authorized = False
                        raw_path = body.get("path", "")
                        if not isinstance(raw_path, str) or not raw_path.strip():
                            raise ValueError("本地媒体路径不能为空")
                        candidate = Path(raw_path).expanduser().resolve(strict=True)
                        roots = [
                            Path(owner.controller.settings["download_dir"]).expanduser().resolve(),
                            Path(owner.controller.settings["browser_download_dir"]).expanduser().resolve(),
                            (Path.home() / "Downloads").resolve(),
                        ]
                        # Chrome can use a user-selected download root. Only our
                        # paired extension's own transfer — live or late-completed
                        # onto a terminally failed task (1.3.10 resurrection) —
                        # may import outside configured roots.
                        owned_browser_file = (
                            body.get("browser_download") is True and linked is not None
                            and linked["status"] in {"awaiting_browser", "failed"}
                            and linked["options"].get("mode") == "browser"
                            and candidate.parent.name in {"FoilPage", "声笺"}
                            and candidate.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".wav", ".ogg"}
                        )
                        if not owned_browser_file and not any(candidate == root or root in candidate.parents for root in roots):
                            raise ValueError("本地媒体路径不在允许的下载目录内")
                        transcribe_flag = body.get("transcribe")
                        if transcribe_flag is None:
                            transcribe_flag = bool(
                                owner.controller.settings.get("transcribe_after_download", False)
                            )
                        if not authorized:
                            # A completion may finish work accepted before logout,
                            # but cannot upgrade that original download-only request.
                            transcribe_flag = bool(linked["options"].get("transcribe_after_download", False))
                        try:
                            imported = owner.coordinator.submit_local_file(
                                str(candidate),
                                title=str(body.get("title", "")),
                                transcribe=bool(transcribe_flag),
                                download_id=str(body.get("download_id") or "") or None,
                                validate_media=body.get("browser_download") is True,
                            )
                        except MediaTypeError as exc:
                            # 无扩展名/不支持的文件是永久性失败：结构化 415 让
                            # 扩展停止重试（2026-10 实机：超长标题截断丢后缀的
                            # 无后缀残留曾在待导入队列里无限重试）。
                            self._error(415, "unsupported_media_format", str(exc))
                            return
                        self._json(202, {"job": imported})
                        return
                    if path == "/v1/quit":
                        # Defer so the HTTP response is flushed before the
                        # owner tears down this server (shutdown() would
                        # otherwise deadlock on the active handler).
                        if owner.quit_callback is not None:
                            _quit_timer = threading.Timer(0.3, owner.quit_callback)
                            _quit_timer.daemon = True
                            _quit_timer.start()
                        self._json(200, {"quitting": owner.quit_callback is not None})
                        return
                    if path == "/v1/downloads/batch":
                        owner._require_work_account()
                        entries = body.get("items")
                        if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
                            self._error(400, "invalid_batch", "批量任务数量必须为 1 到 50")
                            return
                        accepted, rejected = [], []
                        for index, entry in enumerate(entries):
                            try:
                                item = owner.coordinator.submit(
                                    entry.get("url", ""), title=entry.get("title", ""),
                                    options=entry.get("options"),
                                )
                                accepted.append(public_download_job(item))
                            except Exception as exc:
                                rejected.append({"index": index, "message": str(exc)[:300]})
                        self._json(202, {"accepted": accepted, "rejected": rejected})
                        return
                    match = re.fullmatch(
                        r"/v1/downloads/([a-f0-9]{12})/(cancel|retry|browser-failure|fallback|finish-fallback)",
                        path,
                    )
                    if match:
                        action = match.group(2)
                        if action == "cancel":
                            item = owner.coordinator.cancel(match.group(1))
                        elif action == "retry":
                            owner._require_work_account()
                            item = owner.coordinator.retry(match.group(1))
                        elif action == "fallback":
                            try:
                                item = owner.coordinator.continue_fallback(
                                    match.group(1), stage=str(body.get("stage") or ""),
                                    media_url=body.get("media_url", ""),
                                    media_candidates=body.get("media_candidates"),
                                    session_cookies=body.get("browser_session"),
                                )
                            finally:
                                body.pop("browser_session", None)
                        elif action == "finish-fallback":
                            item = owner.coordinator.finish_fallback(match.group(1), message=_safe_failure_text(body.get("message", "")))
                        else:
                            failure_phase = body.get("phase", "browser_download_interrupted")
                            if not isinstance(failure_phase, str) or failure_phase not in _DOWNLOAD_FAILURE_PHASES:
                                failure_phase = "browser_download_interrupted"
                            item = owner.coordinator.record_browser_download_failure(
                                match.group(1),
                                browser_error=body.get("browser_error"),
                                bytes_received=body.get("bytes_received"),
                                total_bytes=body.get("total_bytes"),
                                phase=failure_phase,
                                browser_detail=_safe_failure_text(body.get("browser_detail", "")),
                            )
                        self._json(200, {"job": public_download_job(item)})
                        return
                    self._error(404, "not_found", "接口不存在")
                except AccountLoginRequired as exc:
                    self._error(403, exc.code, str(exc))
                except DownloadNotFoundError:
                    self._error(404, "not_found", "下载任务不存在")
                except (DownloadAdapterError, DownloadStateError, ValueError) as exc:
                    code = getattr(exc, "code", "invalid_request")
                    # Browser resolver failures already have a durable download
                    # row. Return that row to the extension so it can display
                    # the actual failure and avoid creating a duplicate report.
                    download_id = str(getattr(exc, "download_id", "") or "")
                    if path == "/v1/downloads" and re.fullmatch(r"[a-f0-9]{12}", download_id):
                        try:
                            failed_job = owner.coordinator.get(download_id)
                        except Exception:
                            failed_job = None
                        if failed_job and failed_job.get("status") == "failed":
                            self._json(202, {"job": public_download_job(failed_job)})
                            return
                    self._error(400, code, str(exc))
                except OSError as exc:
                    # R12：导入路径不存在/不可访问应统一为 400 客户端错误
                    self._error(400, "invalid_path", f"路径不可用: {type(exc).__name__}")
                except json.JSONDecodeError:
                    self._error(400, "invalid_json", "请求体不是有效 JSON")
                except Exception as exc:
                    import logging
                    logging.getLogger("shengjian.desktop").exception("local api handler failed")
                    self._error(500, "internal_error", f"本地服务处理失败: {type(exc).__name__}")

            def _agent_call(self) -> None:
                if not self._agent_authorized():
                    return
                try:
                    if self.path != "/v1/agent/call":
                        raise AgentControlError(400, "invalid_request", "Agent 接口不接受查询参数")
                    body = self._agent_body()
                    if set(body) != {"method", "params"}:
                        raise AgentControlError(400, "invalid_request", "Agent 请求参数无效")
                    result = owner.agent_control.call(body["method"], body["params"])
                    self._json(200, {"ok": True, "result": result})
                except AgentControlError as exc:
                    self._error(exc.status, exc.code, str(exc))
                except Exception:
                    self._error(500, "internal_error", "Agent 操作未完成，请检查本地服务")

            def _agent_authorized(self) -> bool:
                hosts = self.headers.get_all("Host", [])
                if hosts != [f"127.0.0.1:{owner.address[1]}"]:
                    self._error(403, "host_denied", "Agent 接口仅接受本机地址")
                    return False
                if self.headers.get_all("Origin") is not None:
                    self._error(403, "origin_denied", "Agent 接口不接受浏览器来源")
                    return False
                if any(key.lower().startswith("sec-fetch-") for key in self.headers):
                    self._error(403, "browser_denied", "Agent 接口仅供本机原生客户端使用")
                    return False
                supplied = self.headers.get_all("Authorization", [])
                if len(supplied) != 1 or not secrets.compare_digest(
                    supplied[0].encode("utf-8"), f"Bearer {owner.agent_token}".encode("ascii"),
                ):
                    self._error(401, "unauthorized", "Agent 连接令牌无效")
                    return False
                return True

            def _agent_body(self) -> dict:
                if self.headers.get_all("Transfer-Encoding") is not None:
                    raise AgentControlError(400, "invalid_request", "Agent 请求体格式无效")
                lengths = self.headers.get_all("Content-Length", [])
                content_types = self.headers.get_all("Content-Type", [])
                if (len(lengths) != 1 or len(content_types) != 1
                        or content_types[0].split(";", 1)[0].strip().lower() != "application/json"):
                    raise AgentControlError(400, "invalid_request", "Agent 请求体格式无效")
                try:
                    if not re.fullmatch(r"[0-9]+", lengths[0]):
                        raise ValueError
                    length = int(lengths[0])
                except ValueError:
                    raise AgentControlError(400, "invalid_request", "Agent 请求体长度无效")
                if not 0 < length <= AGENT_MAX_BODY_BYTES:
                    raise AgentControlError(413, "request_too_large", "Agent 请求体超过限制")
                def unique_object(pairs):
                    value = {}
                    for key, item in pairs:
                        if key in value:
                            raise ValueError("duplicate key")
                        value[key] = item
                    return value
                def reject_constant(_value):
                    raise ValueError("nonfinite number")
                self._body_read = True
                previous_timeout = self.connection.gettimeout()
                try:
                    self.connection.settimeout(5.0)
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("incomplete body")
                    body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                                      parse_constant=reject_constant)
                    if not isinstance(body, dict):
                        raise ValueError("not object")
                    return body
                except (ValueError, UnicodeError, OSError, RecursionError) as exc:
                    raise AgentControlError(400, "invalid_request", "Agent 请求体格式无效") from exc
                finally:
                    self.connection.settimeout(previous_timeout)

            def _authorized(self) -> bool:
                expected = f"Bearer {owner.token}"
                supplied = self.headers.get("Authorization", "")
                if not secrets.compare_digest(supplied, expected):
                    self._error(401, "unauthorized", "连接令牌无效")
                    return False
                origin = self.headers.get("Origin", "")
                if not owner.origin_policy.authorize(origin):
                    self._error(403, "origin_denied", "扩展来源未授权")
                    return False
                return True

            def _extension_events(self) -> None:
                """Keep the paired MV3 worker awake and push desktop link requests."""
                origin = self.headers.get("Origin", "")
                pinned = owner.origin_policy.origin
                protocols = [part.strip() for part in self.headers.get(
                    "Sec-WebSocket-Protocol", "",
                ).split(",") if part.strip()]
                supplied_token = protocols[1] if len(protocols) == 2 and protocols[0] == "shengjian-v1" else ""
                if (not pinned or not secrets.compare_digest(origin, pinned)
                        or not secrets.compare_digest(supplied_token, owner.token)):
                    self._error(401, "unauthorized", "浏览器扩展连接未授权")
                    return
                if self.headers.get("Upgrade", "").lower() != "websocket" \
                        or self.headers.get("Sec-WebSocket-Version") != "13":
                    self._error(400, "invalid_websocket", "浏览器扩展连接格式无效")
                    return
                key = self.headers.get("Sec-WebSocket-Key", "")
                try:
                    if len(base64.b64decode(key, validate=True)) != 16:
                        raise ValueError
                except (ValueError, TypeError):
                    self._error(400, "invalid_websocket", "浏览器扩展连接格式无效")
                    return
                accept = base64.b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
                ).digest()).decode("ascii")
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.send_header("Sec-WebSocket-Protocol", "shengjian-v1")
                self.end_headers()
                client = self.connection
                write_lock = owner._register_extension_client(client)
                try:
                    # Text heartbeats dispatch a JS message event often enough
                    # to keep the MV3 worker alive; task events are pushed over
                    # the same authenticated socket immediately.
                    while owner.running:
                        ready, _, _ = select.select([client], [], [], 20)
                        if not owner.running:
                            break
                        if ready:
                            if _read_websocket_client_frame(client) is None:
                                break
                        else:
                            with write_lock:
                                client.sendall(_websocket_frame(b'{"type":"heartbeat"}'))
                except OSError:
                    pass
                finally:
                    owner._remove_extension_client(client)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > MAX_BODY_BYTES:
                    raise ValueError("请求体过大")
                self._body_read = True
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(data, dict):
                    raise ValueError("请求体必须是 JSON object")
                return data

            def _discard_unread_body(self) -> None:
                # Authentication and pairing can answer without using the body.
                # Leaving an already-sent body unread when closing the socket
                # resets Windows clients, hiding the actual 401/403 response.
                if self.command != "POST" or getattr(self, "_body_read", False):
                    return
                self._body_read = True
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except (TypeError, ValueError):
                    return
                maximum = AGENT_MAX_BODY_BYTES if urlsplit(self.path).path == "/v1/agent/call" else MAX_BODY_BYTES
                if not 0 < length <= maximum:
                    return
                previous_timeout = self.connection.gettimeout()
                try:
                    self.connection.settimeout(0.25)
                    self.rfile.read(length)
                except (OSError, TimeoutError):
                    pass
                finally:
                    try:
                        self.connection.settimeout(previous_timeout)
                    except OSError:
                        pass  # Graceful shutdown may have closed this connection.

            def _error(self, status: int, code: str, message: str) -> None:
                self._json(status, {"error": {"code": code, "message": message[:500]}})

            def _json(self, status: int, payload: dict) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                origin = self.headers.get("Origin", "")
                pairing_origin = (
                    urlsplit(self.path).path in ("/v1/pair", "/v1/setup")
                    and EXTENSION_ORIGIN.fullmatch(origin or "")
                )
                login_origin = (urlsplit(self.path).path == '/v1/desktop-login/proof'
                                and self._desktop_login_origin())
                if login_origin or (urlsplit(self.path).path != "/v1/agent/call" and origin
                        and (pairing_origin or owner.origin_policy.allows_preflight(origin))):
                    self._cors(origin)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()
                self._discard_unread_body()

            def _cors(self, origin: str) -> None:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        return Handler
