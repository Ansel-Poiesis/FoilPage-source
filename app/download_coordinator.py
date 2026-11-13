# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Bridge browser download jobs into the existing media and ASR queues."""
from __future__ import annotations

import logging
import re
import hashlib
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from app.controller import AppController
from app.network_diagnostics import active_vpn_tunnel
from app.task_notifications import TaskNotifications
from engine.audio import validate_media_streams
from engine.contracts import DownloadJob, DownloadOptions, EngineError
from engine.download_store import DownloadNotFoundError, DownloadStateError
from engine.downloader import (
    DownloadAdapter,
    DownloadAdapterError,
    DownloadCancelled,
    YtDlpAdapter,
    download_cover_image,
    download_direct_media,
    is_browser_download_url,
    is_direct_media_url,
    is_douyin_cover_url,
    is_hls_media_url,
    is_play_endpoint_url,
    proxy_route_for_platform,
    sanitize_download_filename,
    sanitize_douyin_session_cookies,
    detect_platform,
    validate_supported_url,
)
from engine.power import keep_awake


log = logging.getLogger("shengjian.desktop")


def _failure_options(current: dict, error: dict, channel: str) -> dict:
    """Keep bounded channel classifications across handoffs, without URLs/text."""
    options = dict(current.get("options") or {})
    fields = ("code", "channel", "phase", "browser_error")
    history = []
    for item in (options.get("failure_history") or [])[-8:]:
        if isinstance(item, dict):
            history.append({key: value for key, value in item.items() if key in fields
                            and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value)})
    diagnostic = error.get("diagnostic") or {}
    entry = {"code": error.get("code", "download_failed"), "channel": channel}
    entry.update({key: diagnostic[key] for key in ("phase", "browser_error") if key in diagnostic
                  and isinstance(diagnostic[key], str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", diagnostic[key])})
    history.append(entry)
    history = [history[0], *history[-7:]] if len(history) > 8 else history
    options["failure_history"] = history
    # A first local failure (for example disk space) keeps its established
    # public shape. Expose history once channels/candidates have accumulated.
    if channel == "chrome.downloads" or len(history) > 1:
        error.setdefault("diagnostic", {})["failure_history"] = history
    return options

DEFAULT_DOWNLOAD_OPTIONS: DownloadOptions = {
    "quality": "best-available",
    "audio_only": False,
    "transcribe_after_download": True,
    "language": "zh",
}


def matching_submission_receipt(receipt: DownloadJob, url: str, transcribe: bool, *, allow_any_url: bool = False) -> DownloadJob:
    """A receipt can replay its accepted request, never change its identity."""
    if not isinstance(transcribe, bool):
        raise DownloadAdapterError("invalid_options", "transcribe_after_download 必须是布尔值")
    normalized = validate_supported_url(url, allow_any_url=allow_any_url)
    if receipt["url"] != normalized or bool(receipt["options"].get("transcribe_after_download")) != transcribe:
        raise DownloadAdapterError("invalid_client_request_id", "提交凭据与原任务不一致")
    return receipt

# 提交去重认定的“仍在处理中”状态：received → … → imported/queued/
# transcribing 链上的任务都算（与 store.DOWNLOAD_STATUSES 对齐）。
_DEDUP_ACTIVE_STATUSES = frozenset({
    "received", "resolving", "downloading", "awaiting_browser",
    "imported", "queued", "transcribing",
})

_CHROME_DOWNLOAD_ERRORS = {
    "FILE_FAILED", "FILE_ACCESS_DENIED", "FILE_NO_SPACE", "FILE_NAME_TOO_LONG",
    "FILE_TOO_LARGE", "FILE_VIRUS_INFECTED", "FILE_TRANSIENT_ERROR", "FILE_BLOCKED",
    "FILE_SECURITY_CHECK_FAILED", "FILE_TOO_SHORT", "FILE_HASH_MISMATCH",
    "FILE_SAME_AS_SOURCE", "NETWORK_FAILED", "NETWORK_TIMEOUT", "NETWORK_DISCONNECTED",
    "NETWORK_SERVER_DOWN", "NETWORK_INVALID_REQUEST", "SERVER_FAILED", "SERVER_NO_RANGE",
    "SERVER_BAD_CONTENT", "SERVER_UNAUTHORIZED", "SERVER_CERT_PROBLEM", "SERVER_FORBIDDEN",
    "SERVER_UNREACHABLE", "SERVER_CONTENT_LENGTH_MISMATCH", "SERVER_CROSS_ORIGIN_REDIRECT",
    "USER_CANCELED", "USER_SHUTDOWN", "CRASH",
    # A completed browser transfer can be permanently rejected at import.
    "UNSUPPORTED_MEDIA_FORMAT",
    "BROWSER_DOWNLOAD_HISTORY_MISSING",
}


def _safe_resolver_error(value: object, *, limit: int = 500) -> str:
    """Retain useful resolver detail while removing URLs and credential values."""
    text = " ".join(str(value or "").split())
    text = re.sub(r"https?://[^\s<>\"']+", "[URL 已脱敏]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)(cookie|authorization|token|signature|x-bogus|mstoken|verifyfp)\s*[:=]\s*[^\s,;&]+",
        r"\1=[已脱敏]", text,
    )
    return text[:limit]


_CHROME_DOWNLOAD_ERROR_MESSAGES = {
    "FILE_ACCESS_DENIED": "Chrome 无法写入下载文件，请检查下载目录权限。",
    "FILE_NO_SPACE": "磁盘空间不足，Chrome 无法保存视频。",
    "FILE_NAME_TOO_LONG": "Chrome 生成的文件名过长。",
    "FILE_TOO_LARGE": "视频超过浏览器允许的文件大小。",
    "FILE_VIRUS_INFECTED": "Chrome 安全扫描判定该文件存在风险。",
    "FILE_BLOCKED": "Chrome 安全策略阻止了该文件。",
    "NETWORK_FAILED": "下载连接失败。",
    "NETWORK_TIMEOUT": "下载连接超时。",
    "NETWORK_DISCONNECTED": "网络连接中断。",
    "NETWORK_SERVER_DOWN": "媒体服务器暂时不可达。",
    "NETWORK_INVALID_REQUEST": "浏览器发出的下载请求无效。",
    "SERVER_FAILED": "媒体服务器拒绝或中止了下载请求。",
    "SERVER_NO_RANGE": "媒体服务器不支持浏览器续传请求。",
    "SERVER_BAD_CONTENT": "媒体服务器返回了无法识别的视频内容。",
    "SERVER_UNAUTHORIZED": "媒体服务器要求有效的登录授权。",
    "SERVER_FORBIDDEN": "媒体服务器返回 HTTP 403，当前播放地址或请求上下文不被接受。",
    "SERVER_CERT_PROBLEM": "媒体服务器的 TLS 证书验证失败。",
    "SERVER_UNREACHABLE": "媒体服务器不可达。",
    "SERVER_CONTENT_LENGTH_MISMATCH": "媒体服务器返回的文件长度与声明不一致。",
    "SERVER_CROSS_ORIGIN_REDIRECT": "媒体服务器跳转到了浏览器禁止的跨站地址。",
    "USER_CANCELED": "浏览器下载被用户取消。",
    "USER_SHUTDOWN": "浏览器关闭时下载被中断。",
    "CRASH": "浏览器下载进程异常退出。",
    "UNSUPPORTED_MEDIA_FORMAT": "已下载文件的媒体格式不受支持，请回原视频页重新下载。",
    "BROWSER_DOWNLOAD_HISTORY_MISSING": "浏览器已找不到该下载记录，请回原视频页重新下载。",
}

_RETRYABLE_CHROME_DOWNLOAD_ERRORS = frozenset({
    "FILE_TRANSIENT_ERROR", "NETWORK_FAILED", "NETWORK_TIMEOUT", "NETWORK_DISCONNECTED",
    "NETWORK_SERVER_DOWN", "SERVER_FAILED", "SERVER_UNREACHABLE",
    "BROWSER_DOWNLOAD_HISTORY_MISSING",
})


class DownloadCoordinator:
    def __init__(
        self,
        controller: AppController,
        *,
        adapter: DownloadAdapter | None = None,
    ) -> None:
        self.controller = controller
        self.store = controller.downloads
        self.adapter = adapter or YtDlpAdapter()
        self.download_dir = Path(controller.settings["download_dir"]).expanduser().resolve()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._submission_lock = threading.RLock()
        self._session_lock = threading.RLock()
        # Browser credentials are deliberately process-memory only. They are
        # never added to DownloadJob, SQLite, settings, or diagnostics.
        self._transient_sessions: dict[str, tuple[dict[str, object], ...]] = {}
        # Signed player URLs are also one-shot process-memory data. They let
        # the local Worker download a captured Douyin media stream when the
        # page extractor returns a fresh-cookie challenge.
        self._transient_media_hints: dict[str, tuple[str, ...]] = {}
        # TASK-8.17：热切换配置由 LocalApiServer 装配时注入（云开关
        # covers_enabled 门住整条封面能力）；缺省视为关闭（功能随云配置开）。
        self.client_config = None

    def _covers_cloud_enabled(self) -> bool:
        client_config = getattr(self, "client_config", None)
        enabled = getattr(client_config, "covers_enabled", None)
        return callable(enabled) and bool(enabled())

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.download_dir.mkdir(parents=True, exist_ok=True)
        # A process restart loses only ephemeral URLs and credentials. Keep the
        # durable task and resume public-page resolution or its browser handoff.
        for item in self.store.list(("resolving", "downloading"), limit=500):
            self.store.transition(item["id"], "received", progress=0.0)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="browser-download-worker", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        with self._session_lock:
            sessions = tuple(self._transient_sessions.values())
            self._transient_sessions.clear()
            self._transient_media_hints.clear()
        for session in sessions:
            self._erase_session(session)

    def submit(
        self,
        url: str,
        *,
        title: str = "",
        options: dict | None = None,
        session_cookies: object | None = None,
        media_url: str = "",
        media_candidates: object | None = None,
        mode: str = "local",
        cover_url: str = "",
        gallery_images: list[list[str]] | None = None,
    ) -> DownloadJob:
        normalized = validate_supported_url(
            url,
            allow_any_url=bool(self.controller.settings.get("allow_any_url", False)),
        )
        platform = detect_platform(normalized)
        transient_session: tuple[dict[str, object], ...] = ()
        if session_cookies is not None:
            if platform != "douyin":
                raise DownloadAdapterError(
                    "invalid_browser_session",
                    "浏览器授权仅支持当前抖音下载任务。",
                )
            transient_session = sanitize_douyin_session_cookies(session_cookies)
        if options is not None and not isinstance(options, dict):
            raise DownloadAdapterError("invalid_options", "下载选项必须是对象")
        merged: DownloadOptions = dict(DEFAULT_DOWNLOAD_OPTIONS)
        # 全局默认“下载后自动转写”由设置控制；显式传入的选项优先。
        merged["transcribe_after_download"] = bool(
            self.controller.settings.get("transcribe_after_download", False)
        )
        merged.update(options or {})
        if mode not in ("local", "browser"):
            raise DownloadAdapterError("invalid_mode", "下载模式仅支持 local/browser")
        merged["mode"] = mode
        # TASK-8.17 封面：非法地址直接丢弃而不是拒绝任务——封面只是增强项。
        cover_hint = str(cover_url or "").strip()
        if cover_hint and not is_douyin_cover_url(cover_hint):
            cover_hint = ""
            log.info("cover_url dropped: not a douyin image CDN address")
        merged["cover_url"] = cover_hint
        # TASK-8.18 图集：非法条目剔除、全空整组拒绝；图文无音轨，绝不转写。
        if gallery_images is not None:
            cleaned_gallery = self._sanitize_gallery_images(gallery_images)
            merged["gallery_images"] = cleaned_gallery
            merged["transcribe_after_download"] = False
            merged["browser_fallback"] = False
        self._validate_options(merged)
        # Server-owned snapshot: changing the folder affects new submissions,
        # while an already queued transfer keeps its original destination.
        merged["managed_download_root"] = str(Path(self.controller.settings["download_dir"]).expanduser().resolve())
        media_hint = ""
        media_hints: list[str] = []
        if media_url:
            if not isinstance(media_url, str) or len(media_url) > 4096:
                raise DownloadAdapterError("invalid_media_url", "浏览器媒体地址无效")
            media_hint = media_url.strip()
            if media_hint and not (
                is_browser_download_url(media_hint) or is_hls_media_url(media_hint)
            ):
                raise DownloadAdapterError(
                    "invalid_media_url",
                    "浏览器提供的媒体地址不受支持，请刷新页面后重试",
                    retryable=False,
                )
        if media_candidates is not None:
            if not isinstance(media_candidates, (list, tuple)) or len(media_candidates) > 8:
                raise DownloadAdapterError("invalid_media_url", "媒体候选地址数量无效")
            for candidate in media_candidates:
                if not isinstance(candidate, str) or len(candidate) > 4096:
                    raise DownloadAdapterError("invalid_media_url", "浏览器媒体候选地址无效")
                value = candidate.strip()
                if not value:
                    continue
                if not (is_browser_download_url(value) or is_hls_media_url(value)):
                    raise DownloadAdapterError(
                        "invalid_media_url",
                        "浏览器提供的媒体候选地址不受支持，请刷新页面后重试",
                        retryable=False,
                    )
                if value not in media_hints:
                    media_hints.append(value)
        if media_hint and media_hint not in media_hints:
            media_hints.insert(0, media_hint)
        elif media_hints and not media_hint:
            media_hint = media_hints[0]
        with self._submission_lock:
            request_id = merged.get("client_request_id", "")
            if request_id and self.store.client_request_cancelled(request_id):
                self._erase_session(transient_session)
                raise DownloadAdapterError("cancelled", "任务已取消")
            receipt = self.store.find_by_client_request_id(request_id)
            if receipt is not None:
                matching_submission_receipt(receipt, normalized, bool(merged.get("transcribe_after_download")),
                    allow_any_url=bool(self.controller.settings.get("allow_any_url", False)))
                self._erase_session(transient_session)
                return receipt
            # 先生 2026-10-08：下载后再点转写，同一视频不得重复下载。
            # ① 活跃任务未带转写 → 就地补置转写标志（下载完成后自然接转写）；
            # ② 已完成（done）且媒体文件在 → 复用既有媒体提升转写
            #    （enqueue_media + done→queued，0.12.3 起的既有提升通路），
            #    不再创建新下载行。
            if merged.get("transcribe_after_download"):
                for existing in self.store.list(None, limit=500):
                    if existing.get("url") != normalized:
                        continue
                    status = str(existing.get("status") or "")
                    opts = dict(existing.get("options") or {})
                    if status in _DEDUP_ACTIVE_STATUSES:
                        if not opts.get("transcribe_after_download"):
                            opts["transcribe_after_download"] = True
                            promoted = self.store.transition(existing["id"], status, options=opts)
                            self.store.remember_client_request(request_id, existing["id"])
                            self._erase_session(transient_session)
                            return promoted
                        continue
                    if status == "done" and existing.get("media_id") and existing.get("downloaded_path"):
                        media_path = Path(str(existing["downloaded_path"]))
                        if not media_path.is_file():
                            continue
                        queued = self.controller.enqueue_media([existing["media_id"]])
                        linked = queued.created[0] if queued.created else self._active_job_for_media(existing["media_id"])
                        if not linked:
                            continue
                        opts["transcribe_after_download"] = True
                        promoted = self.store.transition(existing["id"], "queued", job_id=linked, options=opts)
                        self.store.remember_client_request(request_id, existing["id"])
                        self._erase_session(transient_session)
                        self._wake.set()
                        return promoted
            # 同一 URL 已有活跃任务时复用：按钮无反馈期的连点、弹窗与页面
            # 双入口会 otherwise 在数秒内产生 2–3 条必败重复行（2026-09-22
            # library.db 18:25/18:36 同秒重复为证）。终态（done/failed/
            # cancelled）不拦，重试语义不受影响。
            for existing in self.store.list(_DEDUP_ACTIVE_STATUSES, limit=500):
                if existing.get("url") == normalized and bool(existing["options"].get("transcribe_after_download")) == bool(merged.get("transcribe_after_download")):
                    self.store.remember_client_request(request_id, existing["id"])
                    self._erase_session(transient_session)
                    return existing
            item = self.store.create(normalized, title, platform, merged)
            if transient_session:
                with self._session_lock:
                    self._transient_sessions[item["id"]] = transient_session
            if media_hints and mode == "local":
                with self._session_lock:
                    self._transient_media_hints[item["id"]] = tuple(media_hints)
        if mode == "browser":
            return self._resolve_browser_job(item, media_url=media_hint)
        self._wake.set()
        return item

    def submit_local_file(
        self,
        path: str,
        *,
        title: str = "",
        transcribe: bool = True,
        download_id: str | None = None,
        validate_media: bool = False,
    ) -> dict:
        """Import a file already downloaded by Chrome and optionally queue ASR."""
        # done/cancelled/downloaded_path rows are complete — a repeat import is a
        # no-op. A terminal "failed" row is deliberately NOT in this set: the
        # original Chrome transfer can still finish after every fallback channel
        # was exhausted, and its late completion resurrects this task (1.3.10).
        if download_id:
            existing = self.store.get(download_id)
            if existing and (existing.get("downloaded_path") or existing["status"] in {"done", "cancelled"}):
                return {"media_id": existing.get("media_id"), "job_id": existing.get("job_id"), "path": existing.get("downloaded_path")}
        if validate_media:
            try:
                validate_media_streams(str(path), str(self.controller.settings.get("ffmpeg_path") or ""))
            except EngineError as exc:
                if exc.code == "corrupt_media":
                    if download_id:
                        self.record_browser_download_failure(download_id, browser_error="SERVER_BAD_CONTENT")
                    raise DownloadAdapterError("media_not_video", "浏览器返回的文件不是可播放媒体，正在尝试其他下载通道。") from exc
                # The transfer completed. Keep it pending import instead of
                # blaming the CDN and starting another network download.
                with self.controller.media_action_lock:
                    current = self.store.get(download_id) if download_id else None
                    if current and current["status"] == "awaiting_browser" and not current.get("downloaded_path"):
                        self.store.transition(download_id, "awaiting_browser", error={
                            "code": exc.code, "message": exc.message, "retryable": True,
                            "diagnostic": {"channel": "local_api", "phase": "browser_import"},
                        })
                raise DownloadAdapterError(exc.code, exc.message, retryable=True) from exc
        # Paired browser downloads are retained by Chrome; the application
        # stores its own managed copy in the selected folder.
        current = self.store.get(download_id) if download_id else None
        if validate_media and current and current["status"] == "awaiting_browser" and current["options"].get("mode") == "browser":
            source = Path(path).expanduser().resolve(strict=True)
            root = Path(current["options"].get("managed_download_root") or self.controller.settings["download_dir"]).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            destination = root / (current["id"] + "-" + source.name)
            if source != destination:
                from engine.media_store import _sha256
                if destination.exists():
                    if not destination.is_file() or _sha256(destination) != _sha256(source):
                        raise DownloadAdapterError("file_conflict", "保存目录内已有同名文件，请更改目录后重试。")
                else:
                    created = False
                    try:
                        with source.open("rb") as incoming, destination.open("xb") as outgoing:
                            created = True
                            shutil.copyfileobj(incoming, outgoing)
                    except Exception:
                        if created:
                            destination.unlink(missing_ok=True)
                        raise
                path = str(destination)
        # 封面在网络侧取回（不占媒体锁）；Chrome 导入轨与 worker 轨同款落位。
        browser_cover = (
            self._fetch_task_cover(dict(current.get("options") or {}), Path(path))
            if current is not None else None
        )
        with self.controller.media_action_lock:
            current = self.store.get(download_id) if download_id else None
            if current and (current.get("downloaded_path") or current["status"] in {"done", "cancelled"}):
                return {"media_id": current.get("media_id"), "job_id": current.get("job_id"), "path": current.get("downloaded_path")}
            # A later click can promote this same download to ASR while Chrome
            # is transferring it. The durable intent wins over old bridge data.
            if current and current["options"].get("mode") == "browser":
                transcribe = bool(current["options"].get("transcribe_after_download"))
            imported = self.controller.import_files([path], display_names=[title])[0]
            media_id = imported.item["id"]
            job_id = None
            if transcribe:
                queued = self.controller.enqueue_media([media_id])
                job_id = queued.created[0] if queued.created else self._active_job_for_media(media_id)
            result = {
                "media_id": media_id,
                "job_id": job_id,
                "path": imported.item["source_path"],
            }
            if download_id:
                current = self.store.get(download_id)
                # "failed" here means terminally failed after all fallbacks —
                # the Chrome transfer that just delivered this very file may be
                # the one that auto-resumed and outlived them. Resurrect the
                # same row instead of leaving the download unrecorded.
                if current and current["status"] in {"awaiting_browser", "failed"}:
                    self.store.transition(
                        download_id, "imported", progress=1.0,
                        downloaded_path=result["path"], media_id=media_id, error=None,
                        options={**current["options"], "download_content_sha256": imported.item["content_sha256"]},
                        cover_path=browser_cover,
                    )
                    if job_id:
                        self.store.transition(download_id, "queued", job_id=job_id)
                    else:
                        self.store.transition(download_id, "done")
            return result

    def list(self, *, statuses=None, limit: int = 100) -> list[DownloadJob]:
        self.refresh_linked_jobs()
        return [self._decorate_linked_job(item) for item in self.store.list(statuses, limit=limit)]

    def get(self, download_id: str) -> DownloadJob | None:
        self.refresh_linked_jobs(download_id)
        item = self.store.get(download_id)
        return self._decorate_linked_job(item) if item else None

    def _decorate_linked_job(self, item: DownloadJob) -> DownloadJob:
        """Expose stage snapshots without copying private queue paths or persisting them."""
        linked = self.controller.queue.get_job(item["job_id"]) if item.get("job_id") else None
        if not linked:
            return item
        decorated = dict(item)
        for name in ("body_enabled_snapshot", "title_enabled_snapshot"):
            if isinstance(linked.get(name), bool):
                decorated[name] = linked[name]
        for name in ("body_status", "title_status"):
            if linked.get(name) in {"pending", "running", "done", "skipped", "disabled", "cancelled", "failed"}:
                decorated[name] = linked[name]
        # 配额耗尽标记直达任务呈现层：用户必须知道整理未做的原因与去处。
        for name in ("body_quota_exceeded", "title_quota_exceeded"):
            if linked.get(name) is True:
                decorated[name] = True
        if linked.get("status") in {"done", "failed"}:
            label, failed = TaskNotifications.terminal_label(linked)
            decorated["transcription_label"] = label
            decorated["transcription_error"] = failed
        return decorated

    def cancel_submission(self, request_id: object) -> DownloadJob | None:
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", request_id):
            raise DownloadAdapterError("invalid_client_request_id", "提交凭据格式无效")
        with self._submission_lock:
            self.store.cancel_client_request(request_id)
            item = self.store.find_by_client_request_id(request_id)
            return self.cancel(item["id"]) if item else None

    def cancel(self, download_id: str) -> DownloadJob:
        item = self.store.get(download_id)
        if item and item.get("job_id"):
            self.controller.cancel_jobs([item["job_id"]])
        updated = self.store.request_cancel(download_id)
        if updated["status"] == "cancelled":
            self._discard_transient_session(download_id)
            self._discard_transient_media_hint(download_id)
        return updated

    def record_browser_download_failure(
        self,
        download_id: str,
        *,
        browser_error: object = "",
        bytes_received: object = None,
        total_bytes: object = None,
        phase: object = "browser_download_interrupted",
        browser_detail: object = "",
    ) -> DownloadJob:
        with self.controller.media_action_lock:
            return self._record_browser_download_failure(
                download_id, browser_error=browser_error,
                bytes_received=bytes_received, total_bytes=total_bytes,
                phase=phase, browser_detail=browser_detail,
            )

    def _record_browser_download_failure(
        self,
        download_id: str,
        *,
        browser_error: object = "",
        bytes_received: object = None,
        total_bytes: object = None,
        phase: object = "browser_download_interrupted",
        browser_detail: object = "",
    ) -> DownloadJob:
        """Persist Chrome's interruption reason without retaining signed URLs."""
        current = self.store.get(download_id)
        if current is None:
            raise DownloadNotFoundError(download_id)
        if current.get("downloaded_path") or current["status"] in ("done", "failed", "cancelled"):
            return current
        if current["options"].get("mode") != "browser" or current["status"] != "awaiting_browser":
            raise DownloadStateError("该任务不在等待浏览器下载")

        raw_code = str(browser_error or "").strip().upper()
        code = raw_code if raw_code in _CHROME_DOWNLOAD_ERRORS else "BROWSER_INTERRUPTED_UNKNOWN"
        message = _CHROME_DOWNLOAD_ERROR_MESSAGES.get(
            code, "Chrome 中断了下载，但没有返回可识别的原因。",
        )
        retryable = code in _RETRYABLE_CHROME_DOWNLOAD_ERRORS
        can_fallback = (
            current["options"].get("browser_fallback")
            and current["options"].get("fallback_stage") != "resolver"
            and code in {
                "NETWORK_FAILED", "NETWORK_TIMEOUT", "NETWORK_DISCONNECTED", "NETWORK_SERVER_DOWN",
                "SERVER_FAILED", "SERVER_NO_RANGE", "SERVER_BAD_CONTENT", "SERVER_UNAUTHORIZED",
                "SERVER_FORBIDDEN", "SERVER_UNREACHABLE", "SERVER_CONTENT_LENGTH_MISMATCH",
                "SERVER_CROSS_ORIGIN_REDIRECT", "BROWSER_INTERRUPTED_UNKNOWN",
            }
        )
        target_status = "cancelled" if code == "USER_CANCELED" else "awaiting_browser" if can_fallback else "failed"

        def safe_size(value: object) -> int | None:
            if isinstance(value, bool):
                return None
            try:
                number = int(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return number if 0 <= number <= 2**53 - 1 else None

        received = safe_size(bytes_received)
        total = safe_size(total_bytes)
        phase_value = str(phase or "browser_download_interrupted")[:64]
        detail = str(browser_detail or "").strip()[:500]
        start_failure = phase_value == "browser_download_start"
        specific_failure = {
            "UNSUPPORTED_MEDIA_FORMAT": "unsupported_media_format",
            "BROWSER_DOWNLOAD_HISTORY_MISSING": "browser_download_history_missing",
        }.get(code)
        error = {
            "code": specific_failure or ("browser_download_start_failed" if start_failure else "browser_download_interrupted"),
            "message": (
                message if specific_failure else f"Chrome 未能启动下载：{detail or message}"
                if start_failure else f"Chrome 下载错误 {code}：{message}"
            ),
            "retryable": retryable,
            "diagnostic": {
                "channel": "chrome.downloads",
                "phase": phase_value,
                "browser_error": code,
                "browser_detail": detail,
                "bytes_received": received,
                "total_bytes": total,
            },
        }
        if can_fallback:
            error["diagnostic"]["fallback_pending"] = "browser" if len(current["options"].get("browser_attempted") or []) < 8 else "resolver"
        return self.store.transition(
            download_id, target_status, error=error, cancel_requested=False,
            bytes_done=received or 0, bytes_total=total,
            options=_failure_options(current, error, "chrome.downloads"),
        )

    def record_extension_failure(
        self,
        *,
        url: str,
        title: str = "",
        event_id: str,
        phase: str,
        channel: str,
        code: str,
        message: str,
        diagnostic: dict | None = None,
        retryable: bool = False,
        task_id: str = "",
    ) -> DownloadJob:
        """Write extension-side failures into the same persistent task history.

        ``event_id`` makes extension retries idempotent. URL and error details are
        validated at the API boundary; this method retains only the submitted
        video page URL, never media URLs or browser credentials.
        """
        normalized = validate_supported_url(
            url,
            allow_any_url=bool(self.controller.settings.get("allow_any_url", False)),
        )
        if not isinstance(diagnostic, dict):
            diagnostic = {}
        error_diagnostic = dict(diagnostic)
        error_diagnostic.update({
            "event_id": event_id,
            "phase": phase,
            "channel": channel,
        })
        report = {
            "code": str(code or "extension_error")[:64],
            "message": str(message or "扩展未能完成视频下载。")[:500],
            "retryable": bool(retryable),
            "diagnostic": error_diagnostic,
        }

        with self._submission_lock, self.controller.media_action_lock:
            # A timed-out HTTP response may be retried after the server already
            # committed the first report. Find its durable event id before making
            # another row. The same pass resolves the URL match for receipt-less
            # reports (task_id empty or unknown): those arrive when the submit
            # response was lost while the task actually lives on, and must be
            # attached to that task instead of minting standalone failed rows —
            # one click can never grow extra failure records.
            matched_live = matched_finished = matched_failed = None
            for existing in self.store.list(limit=500):
                existing_error = existing.get("error") or {}
                existing_diag = existing_error.get("diagnostic") or {}
                if existing_diag.get("event_id") == event_id:
                    return existing
                if any(
                    isinstance(item, dict) and item.get("event_id") == event_id
                    for item in existing_error.get("incident_reports", [])
                ):
                    return existing
                if task_id or existing.get("url") != normalized:
                    continue
                existing_status = existing.get("status")
                if existing_status in {"done", "cancelled"}:
                    matched_finished = matched_finished or existing
                elif existing_status == "failed":
                    matched_failed = matched_failed or existing
                else:
                    # list() is newest-first: the first live task for this URL wins.
                    matched_live = existing
                    break

            current = self.store.get(task_id) if task_id else None
            if current and (current.get("downloaded_path") or current.get("status") in {"done", "cancelled"}):
                return current
            if current and current.get("status") == "failed":
                previous = current.get("error") or {}
                reports = list(previous.get("incident_reports") or [])
                reports.append(report)
                previous["incident_reports"] = reports[-10:]
                previous_diag = dict(previous.get("diagnostic") or {})
                previous_diag["additional_report_count"] = len(previous["incident_reports"])
                previous["diagnostic"] = previous_diag
                return self.store.transition(task_id, "failed", error=previous)

            if current is None:
                if matched_live:
                    # The live task owns its own failure path; a receipt-less
                    # report describes the same submission and must not kill it.
                    return matched_live
                if matched_finished:
                    return matched_finished
                if matched_failed:
                    previous = matched_failed.get("error") or {}
                    reports = list(previous.get("incident_reports") or [])
                    reports.append(report)
                    previous["incident_reports"] = reports[-10:]
                    previous_diag = dict(previous.get("diagnostic") or {})
                    previous_diag["additional_report_count"] = len(previous["incident_reports"])
                    previous["diagnostic"] = previous_diag
                    return self.store.transition(matched_failed["id"], "failed", error=previous)
                platform = detect_platform(normalized)
                item = self.store.create(
                    normalized,
                    str(title or "")[:240],
                    platform,
                    {
                        "quality": "best-available",
                        "audio_only": False,
                        "transcribe_after_download": False,
                        "language": "zh",
                        "mode": "browser",
                    },
                )
                task_id = item["id"]
                current = self.store.transition(task_id, "resolving")
            return self.store.transition(task_id, "failed", error=report, cancel_requested=False)

    def continue_fallback(
        self, download_id: str, *, stage: str, media_url: str = "",
        media_candidates: object | None = None, session_cookies: object | None = None,
    ) -> DownloadJob:
        """Continue an authorized browser handoff without creating another row."""
        with self._submission_lock:
            item = self.store.get(download_id)
            if item is None:
                raise DownloadNotFoundError(download_id)
            pending = (item.get("error") or {}).get("diagnostic", {}).get("fallback_pending")
            if item["status"] in {"done", "cancelled"} or (item["status"] == "awaiting_browser" and not pending):
                return item  # A duplicate handoff never restarts Chrome.
            if item["status"] != "awaiting_browser" or pending not in {"browser", "resolver"}:
                raise DownloadStateError("该任务没有待接续的下载通道")
            if stage not in {"browser", "resolver"} or (pending == "resolver" and stage != "resolver"):
                raise DownloadStateError("下载通道顺序无效")
            options = dict(item["options"])
            if stage == "browser":
                candidates = media_candidates if media_candidates is not None else [media_url]
                if not isinstance(candidates, list) or len(candidates) > 8:
                    raise DownloadAdapterError("invalid_media_url", "浏览器媒体候选地址数量无效")
                attempted = set(options.get("browser_attempted") or [])
                rejected = set(options.get("rejected_media") or [])
                selected = ""
                for candidate in candidates:
                    if not isinstance(candidate, str) or len(candidate) > 4096 or not is_browser_download_url(candidate):
                        raise DownloadAdapterError("invalid_media_url", "浏览器媒体候选地址无效")
                    if hashlib.sha256(candidate.encode("utf-8")).hexdigest() not in attempted | rejected and not selected:
                        selected = candidate
                if not selected or len(attempted) >= 8:
                    error = dict(item.get("error") or {})
                    diagnostic = dict(error.get("diagnostic") or {})
                    diagnostic["fallback_pending"] = "resolver"
                    error["diagnostic"] = diagnostic
                    return self.store.transition(download_id, "awaiting_browser", error=error)
                media_url = selected
            options.update(mode="browser" if stage == "browser" else "local", fallback_stage=stage)
            self._discard_transient_session(download_id)
            if session_cookies is not None:
                session = sanitize_douyin_session_cookies(session_cookies)
                with self._session_lock:
                    self._transient_sessions[download_id] = session
            self._discard_transient_media_hint(download_id)
            item = self.store.transition(
                download_id, "resolving" if stage == "browser" else "received",
                options=options, error=None, progress=0.0, bytes_done=0, bytes_total=None,
            )
            if stage == "browser":
                return self._resolve_browser_job(item, media_url=media_url)
            self._wake.set()
            return item

    def finish_fallback(self, download_id: str, *, message: str = "") -> DownloadJob:
        item = self.store.get(download_id)
        if item is None:
            raise DownloadNotFoundError(download_id)
        if item["status"] != "awaiting_browser" or not (item.get("error") or {}).get("diagnostic", {}).get("fallback_pending"):
            return item
        error = dict(item.get("error") or {})
        diagnostic = dict(error.get("diagnostic") or {})
        diagnostic.pop("fallback_pending", None)
        error.update(code="download_channels_exhausted", message=_safe_resolver_error(message or error.get("message") or "可用下载通道均未成功，请在任务详情查看原因。"), retryable=True, diagnostic=diagnostic)
        self._discard_transient_session(download_id)
        self._discard_transient_media_hint(download_id)
        return self.store.transition(download_id, "failed", error=error)

    def retry(self, download_id: str) -> DownloadJob:
        current = self.store.get(download_id)
        if current is None:
            raise DownloadNotFoundError(download_id)
        if current["options"].get("mode") == "browser":
            if current["status"] != "failed":
                raise DownloadStateError("只有可重试的失败下载才能重试")
            retried = self.store.retry(download_id)
            return self._resolve_browser_job(retried)
        item = self.store.retry(download_id)
        self._wake.set()
        return item

    def _resolve_browser_job(self, item: DownloadJob, *, media_url: str = "") -> DownloadJob:
        """Resolve a direct URL synchronously and wait for the browser download."""
        download_id = item["id"]
        if item["platform_hint"] != "douyin":
            self._fail(
                download_id, "unsupported_browser_download",
                "浏览器直提目前仅支持抖音当前视频", retryable=False,
            )
            error = DownloadAdapterError(
                "unsupported_browser_download",
                "浏览器直提目前仅支持抖音当前视频",
            )
            error.download_id = download_id
            raise error
        self.store.transition(download_id, "resolving")
        try:
            if media_url:
                resolved = self._browser_media_hint(item, media_url)
            else:
                resolved = self.adapter.resolve(
                    item["url"],
                    referer=str(item["options"].get("referer", "")),
                    user_agent=str(item["options"].get("user_agent", "")),
                    session_cookies=self._transient_session(download_id),
                    js_runtime_path=str(
                        self.controller.settings.get("js_runtime_path") or ""
                    ),
                    proxy_policy=str(
                        self.controller.settings.get("download_proxy_policy") or "auto"
                    ),
                    proxy_url=str(
                        self.controller.settings.get("download_proxy_url") or ""
                    ),
                )
            if not isinstance(resolved, dict):
                raise DownloadAdapterError(
                    "resolve_failed", "解析结果无效", retryable=True,
                )
            download_url = str(resolved.get("download_url") or "")
            if not is_browser_download_url(download_url):
                raise DownloadAdapterError(
                    "resolve_failed",
                    "解析结果不是可直提的媒体地址；请回到视频页面重新提交",
                    retryable=True,
                )
            filename = sanitize_download_filename(
                str(resolved.get("filename") or ""), fallback="douyin-video",
            )
            raw_headers = resolved.get("headers")
            safe_headers: dict[str, str] = {}
            if isinstance(raw_headers, dict):
                for name, value in raw_headers.items():
                    if str(name).lower() == "accept":
                        safe_headers[str(name)] = str(value)[:2048]
            current = self.store.get(download_id)
            if current is None or current["status"] == "cancelled" or current.get("cancel_requested"):
                raise DownloadCancelled()
            options = dict(current["options"])
            attempted = list(options.get("browser_attempted") or [])
            fingerprint = hashlib.sha256(download_url.encode("utf-8")).hexdigest()
            if fingerprint not in attempted:
                attempted.append(fingerprint)
            options["browser_attempted"] = attempted[-8:]
            self.store.transition(download_id, "awaiting_browser", options=options)
            current = self.store.get(download_id)
            if current is None:
                raise DownloadAdapterError(
                    "resolve_failed", "任务状态丢失", retryable=True,
                )
            current["browser_download"] = {
                "download_url": download_url,
                "filename": filename,
                "headers": safe_headers,
            }
            return current
        except DownloadCancelled:
            raise
        except DownloadAdapterError as exc:
            if item["options"].get("browser_fallback") and item["options"].get("fallback_stage") != "resolver":
                return self.store.transition(download_id, "awaiting_browser", error={
                    "code": exc.code, "message": _safe_resolver_error(exc.message), "retryable": exc.retryable,
                    "diagnostic": {"fallback_pending": "resolver", "channel": "desktop_resolver", "phase": "media_resolution"},
                })
            self.store.transition(download_id, "failed", error={
                "code": exc.code, "message": exc.message[:500],
                "retryable": exc.retryable,
                "diagnostic": {
                    "phase": "media_resolution",
                    "channel": "desktop_resolver",
                    "error_type": type(exc).__name__,
                },
            })
            exc.download_id = download_id
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {_safe_resolver_error(exc)}"
            if item["options"].get("browser_fallback") and item["options"].get("fallback_stage") != "resolver":
                return self.store.transition(download_id, "awaiting_browser", error={
                    "code": "resolve_failed", "message": message, "retryable": True,
                    "diagnostic": {"fallback_pending": "resolver", "phase": "media_resolution", "channel": "desktop_resolver"},
                })
            self.store.transition(download_id, "failed", error={
                "code": "resolve_failed", "message": message[:500],
                "retryable": True,
                "diagnostic": {
                    "phase": "media_resolution",
                    "channel": "desktop_resolver",
                    "error_type": type(exc).__name__,
                },
            })
            error = DownloadAdapterError("resolve_failed", message, retryable=True)
            error.download_id = download_id
            raise error from exc
        finally:
            self._discard_transient_session(download_id)

    @staticmethod
    def _browser_media_hint(item: DownloadJob, media_url: str) -> dict[str, str]:
        """Turn an extension-observed player URL into the browser-handoff contract."""
        if not is_browser_download_url(media_url):
            raise DownloadAdapterError(
                "invalid_media_url",
                "浏览器提供的媒体地址不受支持，请刷新页面后重试",
                retryable=False,
            )
        return {
            "download_url": media_url,
            "filename": f"{sanitize_download_filename(str(item.get('title') or ''), fallback='douyin-video')}.mp4",
            "headers": {"Accept": "video/*,audio/*,application/octet-stream;q=0.9,*/*;q=0.8"},
        }

    def refresh_linked_jobs(self, only_id: str | None = None) -> None:
        items = [self.store.get(only_id)] if only_id else self.store.list(
            ("queued", "transcribing"), limit=500
        )
        status_map = {
            "queued": "queued", "retryable": "queued", "running": "transcribing",
            "done": "done", "failed": "failed", "cancelled": "cancelled",
        }
        for item in items:
            if not item or not item.get("job_id") or item["status"] not in ("queued", "transcribing"):
                continue
            linked = self.controller.queue.get_job(item["job_id"])
            if not linked:
                continue
            target = status_map.get(linked["status"])
            if target and target != item["status"]:
                changes = {}
                if target == "failed":
                    error = linked.get("error") or {}
                    changes["error"] = {
                        "code": error.get("code", "transcription_failed"),
                        "message": error.get("message", "转写失败")[:500],
                        "retryable": linked["status"] == "retryable",
                    }
                self.store.transition(item["id"], target, **changes)

    def sweep_stale_pending(self) -> list[str]:
        """悬挂在浏览器等待态的任务按设置限期自动判失败（先生 2026-10-25）。

        页面采集窗口约两小时，超龄的媒体地址必然失效；与其让任务无限
        悬挂到用户手动过问，不如到期给出带指引的失败（可重试）。时限由
        设置 download_pending_timeout_minutes 控制，默认 120 分钟。
        """
        try:
            minutes = int(self.controller.settings.get("download_pending_timeout_minutes") or 120)
        except (TypeError, ValueError):
            minutes = 120
        minutes = min(max(minutes, 5), 1440)
        cutoff = datetime.now() - timedelta(minutes=minutes)
        swept: list[str] = []
        for item in self.store.list(("awaiting_browser",), limit=500):
            try:
                updated = datetime.fromisoformat(str(item.get("updated_at") or ""))
            except ValueError:
                continue
            if updated >= cutoff:
                continue
            self.store.transition(item["id"], "failed", cancel_requested=False, error={
                "code": "download_pending_timeout",
                "message": (
                    f"任务悬挂超过 {minutes} 分钟，已自动停止：视频页的下载地址有时效，"
                    "放置过久会失效。请收藏该视频，刷新页面等画面加载后重新下载；"
                    "悬挂时限可在 设置 → 下载 中调整。"
                ),
                "retryable": True,
            })
            log.info("download %s swept: awaiting_browser older than %s minutes", item["id"], minutes)
            swept.append(item["id"])
        return swept

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Handoff waits for a paired extension, not for a fixed wall-clock
            # deadline. Browser channels are bounded by attempted candidate hashes;
            # a temporary offline browser/service must not consume those attempts.
            try:
                self.sweep_stale_pending()
            except Exception:
                log.exception("stale pending sweep failed")
            with self._submission_lock:
                item = self.store.claim_next()
            if item is None:
                self._wake.wait(0.8)
                self._wake.clear()
                continue
            # 下载期间机器休眠会让任务无声挂起（用户视角=软件卡死）；
            # 持线程级执行锁直到本任务收敛（含导入与转写入队）。
            with keep_awake(f"download:{item['id']}"):
                self._run_one(item)

    def _run_one(self, item: DownloadJob) -> None:
        download_id = item["id"]
        download_dir = Path(item["options"].get("managed_download_root") or self.controller.settings["download_dir"]).expanduser().resolve()
        try:
            download_dir.mkdir(parents=True, exist_ok=True)
            # TASK-8.18：图集任务——签名图片直链逐张下载（取证④：无 Referer/
            # Cookie 门禁），不经媒体提示与浏览器授权链，须在授权检查前分流。
            if item["options"].get("gallery_images"):
                self.store.transition(download_id, "downloading", progress=0.0)

                def cancelled() -> bool:
                    return self._stop.is_set() or self.store.is_cancel_requested(download_id)

                def progress(done: int, total: int | None) -> None:
                    value = min(1.0, done / total) if total else 0.0
                    self.store.transition(
                        download_id, "downloading", progress=value,
                        bytes_done=max(0, done), bytes_total=total,
                    )

                self._run_gallery_images(download_id, item, download_dir, cancelled, progress)
                return
            session_cookies = self._transient_session(download_id)
            media_hints = self._transient_media_hints_for(download_id)
            # A captured player URL is CDN-signed (Referer-guarded, not cookie-
            # guarded — verified against douyinvod directly), so it downloads
            # without any browser login state. Douyin cookies are only demanded
            # when yt-dlp must resolve the page itself.
            if item["platform_hint"] == "douyin" and not session_cookies and not media_hints:
                raise DownloadAdapterError(
                    "authorization_required",
                    "抖音下载需要在视频页面重新授权；FoilPage 不会保存浏览器登录态。",
                )
            settings = self.controller.settings
            proxy_policy = str(settings.get("download_proxy_policy") or "auto")
            proxy_url = str(settings.get("download_proxy_url") or "")
            minimum_free = float(settings.get("download_min_free_gib", 5.0))
            free_bytes = shutil.disk_usage(download_dir).free
            if free_bytes < minimum_free * (1024 ** 3):
                raise DownloadAdapterError(
                    "insufficient_disk_space",
                    f"下载目录剩余空间不足，需要至少保留 {minimum_free:g} GiB",
                    retryable=True,
                )
            self.store.transition(download_id, "downloading", progress=0.0)

            def cancelled() -> bool:
                return self._stop.is_set() or self.store.is_cancel_requested(download_id)

            def progress(done: int, total: int | None) -> None:
                value = min(1.0, done / total) if total else 0.0
                self.store.transition(
                    download_id, "downloading", progress=value,
                    bytes_done=max(0, done), bytes_total=total,
                )

            # TASK-8.17：云开关（client-config covers_enabled）与本地设置
            # 双门控；抖音任务的专用封面地址优先于 extractor 缩略图。
            wants_cover = (
                bool(settings.get("download_covers", True))
                and self._covers_cloud_enabled()
                and not str(item["options"].get("cover_url") or "")
            )

            def adapter_download(target_url: str) -> Path:
                return self.adapter.download(
                    target_url, download_dir,
                    audio_only=bool(item["options"]["audio_only"]),
                    rate_limit_bps=int(settings.get("download_rate_limit_kib", 0)) * 1024 or None,
                    referer=str(item["options"].get("referer") or item["url"]),
                    user_agent=str(item["options"].get("user_agent", "")),
                    session_cookies=session_cookies,
                    progress=progress,
                    cancel_check=cancelled,
                    js_runtime_path=str(settings.get("js_runtime_path") or ""),
                    proxy_policy=proxy_policy,
                    proxy_url=proxy_url,
                    with_cover=wants_cover,
                )

            if media_hints:
                def download_media_hint(hint: str) -> Path:
                    if is_hls_media_url(hint):
                        # HLS 主清单交给 yt-dlp 分片下载，不能当单文件保存。
                        return adapter_download(hint)
                    if is_play_endpoint_url(hint) and not session_cookies:
                        raise DownloadAdapterError(
                            "media_forbidden",
                            "该播放地址需要浏览器登录态",
                            retryable=False,
                        )
                    try:
                        return download_direct_media(
                            hint, download_dir,
                            referer=str(item["options"].get("referer") or item["url"]),
                            user_agent=str(item["options"].get("user_agent", "")),
                            session_cookies=session_cookies,
                            rate_limit_bps=int(settings.get("download_rate_limit_kib", 0)) * 1024 or None,
                            progress=progress,
                            cancel_check=cancelled,
                            proxy_route=proxy_route_for_platform(
                                "douyin", policy=proxy_policy, custom_url=proxy_url,
                            ),
                        )
                    except DownloadAdapterError as exc:
                        # 签名播放地址可能跳转到 HLS；清单与分片交给 yt-dlp。
                        if exc.code != "hls_redirect":
                            raise
                        return adapter_download(getattr(exc, "final_url", "") or hint)

                last_candidate_error: DownloadAdapterError | None = None
                media_path = None
                for hint in media_hints:
                    if cancelled():
                        raise DownloadCancelled()
                    try:
                        media_path = download_media_hint(hint)
                        break
                    except DownloadAdapterError as exc:
                        if exc.code not in {
                            "media_forbidden", "media_http_error", "media_not_video",
                            "media_empty", "media_download_failed", "media_redirect_denied",
                        }:
                            raise
                        last_candidate_error = exc
                if media_path is None:
                    # A valid one-shot session opens a distinct page-extractor
                    # channel after every observed stream has been exhausted.
                    if session_cookies:
                        media_path = adapter_download(item["url"])
                    elif last_candidate_error is not None:
                        raise last_candidate_error
                    if media_path is None:
                        raise DownloadAdapterError(
                            "media_download_failed", "所有抖音媒体候选地址均不可用",
                        )
            elif is_direct_media_url(item["url"]):
                media_path = download_direct_media(
                    item["url"], download_dir,
                    referer=str(item["options"].get("referer", "")),
                    user_agent=str(item["options"].get("user_agent", "")),
                    session_cookies=session_cookies,
                    rate_limit_bps=int(settings.get("download_rate_limit_kib", 0)) * 1024 or None,
                    progress=progress,
                    cancel_check=cancelled,
                    proxy_route=proxy_route_for_platform(
                        detect_platform(item["url"]), policy=proxy_policy, custom_url=proxy_url,
                    ),
                )
            else:
                media_path = adapter_download(item["url"])
            # The extractor has finished using the one-shot session. Drop it
            # before importing or queuing, which cannot require credentials.
            self._discard_transient_session(download_id)
            if cancelled():
                raise DownloadCancelled()
            # 封面取回/落位在媒体锁外完成（网络动作不占锁），失败不阻断。
            cover_path = self._fetch_task_cover(dict(item["options"]), media_path)
            with self.controller.media_action_lock:
                if cancelled():
                    raise DownloadCancelled()
                imported = self.controller.import_files([str(media_path)])[0]
                media_id = imported.item["id"]
                self.store.transition(
                    download_id, "imported", progress=1.0,
                    bytes_done=media_path.stat().st_size,
                    bytes_total=media_path.stat().st_size,
                    downloaded_path=str(media_path), media_id=media_id,
                    options={**item["options"], "download_content_sha256": imported.item["content_sha256"]},
                    cover_path=cover_path, error=None,
                )
                if not item["options"]["transcribe_after_download"]:
                    self.store.transition(download_id, "done")
                    return
                queued = self.controller.enqueue_media([media_id])
                linked_id = queued.created[0] if queued.created else self._active_job_for_media(media_id)
                if not linked_id:
                    raise DownloadAdapterError("queue_conflict", "媒体已存在但无法关联转写任务")
                self.store.transition(download_id, "queued", job_id=linked_id)
        except DownloadCancelled:
            current = self.store.get(download_id)
            if current and current["status"] != "cancelled":
                if self._stop.is_set() and not current.get("cancel_requested"):
                    self.store.transition(download_id, "received", progress=0.0)
                else:
                    self.store.transition(download_id, "cancelled", cancel_requested=False)
        except DownloadAdapterError as exc:
            self._fail(download_id, exc.code, exc.message, exc.retryable)
        except Exception as exc:
            self._fail(download_id, "download_failed", f"{type(exc).__name__}: {_safe_resolver_error(exc)}", True)
        finally:
            current = self.store.get(download_id)
            if not current or current["status"] in ("done", "failed", "cancelled"):
                self._discard_transient_session(download_id)
                self._discard_transient_media_hint(download_id)

    GALLERY_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})

    def _sanitize_gallery_images(self, gallery_images: object) -> list[list[str]]:
        """图集载荷清洗：形状硬校验＋图片域白名单（与封面同口径）。"""
        if not isinstance(gallery_images, list) or not 1 <= len(gallery_images) <= 60:
            raise DownloadAdapterError("invalid_gallery", "图集地址数量无效（1-60 张）")
        cleaned: list[list[str]] = []
        for row in gallery_images:
            if not isinstance(row, list) or not 1 <= len(row) <= 8:
                raise DownloadAdapterError("invalid_gallery", "图集候选地址数量无效")
            urls = [
                url.strip() for url in row
                if isinstance(url, str) and url.strip() and len(url) <= 4096
                and is_douyin_cover_url(url.strip())
            ]
            if urls:
                cleaned.append(urls[:8])
        if not cleaned:
            raise DownloadAdapterError("invalid_gallery", "图集地址均不受支持")
        return cleaned

    def _run_gallery_images(
        self,
        download_id: str,
        item: DownloadJob,
        download_dir: Path,
        cancelled: Callable[[], bool],
        progress: Callable[[int, int | None], None],
    ) -> None:
        """图集下载引擎：并发 3、逐图候选回退、断点续传（文件在即跳过）。

        产物落 ``<下载目录>/<标题>-<note_id>/001.<ext>…``；部分失败持久化
        ``gallery_done``，重试只补缺图。图文无音轨：完成即 done，不进转写。
        """
        options = dict(item["options"])
        images = options["gallery_images"]
        total = len(images)
        note_url = str(item["url"])
        video_id = note_url.rstrip("/").rsplit("/", 1)[-1]
        stem = sanitize_download_filename(str(item.get("title") or ""), fallback="douyin-gallery")
        gallery_dir = download_dir / f"{stem}-{video_id[:24]}"
        gallery_dir.mkdir(parents=True, exist_ok=True)
        settings = self.controller.settings
        proxy_route = proxy_route_for_platform(
            "douyin",
            policy=str(settings.get("download_proxy_policy") or "auto"),
            custom_url=str(settings.get("download_proxy_url") or ""),
        )
        user_agent = str(options.get("user_agent", ""))

        def image_suffix(urls: list[str]) -> str:
            suffix = Path(urlparse(urls[0]).path).suffix.lower()
            return suffix if suffix in self.GALLERY_IMAGE_EXTENSIONS else ".jpeg"

        def fetch_one(index: int) -> bool:
            target = gallery_dir / f"{index + 1:03d}{image_suffix(images[index])}"
            if target.is_file() and target.stat().st_size > 0:
                return True  # 断点续传：已落盘的图直接跳过
            for url in images[index]:
                if cancelled():
                    raise DownloadCancelled()
                try:
                    download_cover_image(
                        url, target, referer=note_url,
                        user_agent=user_agent, proxy_route=proxy_route,
                    )
                    return True
                except Exception as exc:
                    log.info("gallery image %d candidate failed: %s", index + 1, str(exc)[:120])
            return False

        completed_indexes: list[int] = []
        failed_indexes: list[int] = []
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="gallery-img") as pool:
            for index, ok in zip(range(total), pool.map(fetch_one, range(total))):
                (completed_indexes if ok else failed_indexes).append(index)
                progress(len(completed_indexes), total)

        if failed_indexes:
            # 部分失败：持久化已完成序号，重试只补缺图。
            current = self.store.get(download_id) or {}
            merged_options = dict(current.get("options") or options)
            merged_options["gallery_done"] = completed_indexes
            self.store.transition(download_id, "downloading", options=merged_options)
            raise DownloadAdapterError(
                "gallery_partial",
                f"图集有 {len(failed_indexes)}/{total} 张图片下载失败，已保留成功部分；重试将只补缺图。",
                retryable=True,
            )

        cover_path = None
        cover_url = str(options.get("cover_url") or "")
        if cover_url and is_douyin_cover_url(cover_url):
            try:
                cover_path = str(download_cover_image(
                    cover_url, gallery_dir / "cover.jpg",
                    referer=note_url, user_agent=user_agent, proxy_route=proxy_route,
                ))
            except Exception as exc:
                log.info("gallery cover skipped: %s", str(exc)[:120])
        final_options = {**options, "gallery_done": completed_indexes}
        self.store.transition(
            download_id, "imported", progress=1.0,
            downloaded_path=str(gallery_dir), media_id=None,
            cover_path=cover_path, options=final_options,
        )
        self.store.transition(download_id, "done")

    def _transient_session(self, download_id: str) -> tuple[dict[str, object], ...]:
        with self._session_lock:
            return self._transient_sessions.get(download_id, ())

    def _fetch_task_cover(self, options: dict, media_path: Path) -> str | None:
        """TASK-8.17 封面落位：与媒体同名（<stem>.jpg）。

        抖音走任务携带的已验签白名单封面地址；yt-dlp 轨道的封面由适配器
        在下载时同步写好，这里只确认存在。封面是增强项：设置关闭、地址
        非法或下载失败都只记日志并返回 None，绝不影响主任务。
        """
        if not bool(self.controller.settings.get("download_covers", True)):
            return None
        if not self._covers_cloud_enabled():
            return None
        target = media_path.with_name(f"{media_path.stem}.jpg")
        cover_url = str(options.get("cover_url") or "")
        if not cover_url:
            # 封面取证（2026-11-01）：实机 455 个完成任务封面全空。抖音直下
            # 轨道没有 extractor 缩略图，此处置空意味着扩展端封面从未送达
            # （捕获字段漂移或 CDN 换域）——必须留痕，配合扩展诊断的
            # response_cover_* 计数定位，不再静默吞掉。
            if not target.is_file():
                log.info(
                    "task cover missing: no cover_url supplied and no thumbnail landed (%s)",
                    media_path.name,
                )
            return str(target) if target.is_file() else None
        if not is_douyin_cover_url(cover_url):
            return None
        try:
            download_cover_image(
                cover_url, target,
                referer=str(options.get("referer") or ""),
                user_agent=str(options.get("user_agent", "")),
                proxy_route=proxy_route_for_platform(
                    "douyin",
                    policy=str(self.controller.settings.get("download_proxy_policy") or "auto"),
                    custom_url=str(self.controller.settings.get("download_proxy_url") or ""),
                ),
            )
            log.info("task cover saved: %s", target.name)
            return str(target)
        except Exception as exc:  # 增强项失败不冒泡
            log.info("task cover skipped (%s): %s", type(exc).__name__, str(exc)[:160])
            return None

    def _discard_transient_session(self, download_id: str) -> None:
        with self._session_lock:
            session = self._transient_sessions.pop(download_id, ())
        self._erase_session(session)

    @staticmethod
    def _erase_session(session: tuple[dict[str, object], ...]) -> None:
        for cookie in session:
            cookie["value"] = ""
            cookie.clear()

    def _transient_media_hints_for(self, download_id: str) -> tuple[str, ...]:
        with self._session_lock:
            return self._transient_media_hints.get(download_id, ())

    def _discard_transient_media_hint(self, download_id: str) -> None:
        with self._session_lock:
            self._transient_media_hints.pop(download_id, None)
    def _active_job_for_media(self, media_id: str) -> str | None:
        media = self.controller.media.get_media(media_id)
        for job in reversed(self.controller.queue.list_jobs()):
            if job["source_path"] == media["source_path"] and job["status"] in (
                "queued", "running", "retryable", "done",
            ):
                return job["id"]
        return None

    def _fail(self, download_id: str, code: str, message: str, retryable: bool) -> None:
        with self.controller.media_action_lock:
            current = self.store.get(download_id)
            if not current or current.get("downloaded_path") or current["status"] in {"failed", "cancelled", "done"}:
                return
            error = {"code": code, "message": _safe_resolver_error(message), "retryable": retryable}
            # 412/网络错误与「系统级 VPN 隧道把国内平台流量绕到境外出口」高度
            # 同源（2026-11-01 实机取证：Windscribe 下 B 站 412 秒拒、抖音
            # 0.4 MiB/s）。应用代理策略改不了路由表，必须让用户在任务诊断里
            # 直接看到隧道证据，而不是误判为平台抽风或网络抖动。
            if code in ("source_rate_limited", "network_error"):
                tunnel = active_vpn_tunnel()
                if tunnel:
                    error["message"] += (
                        f" 检测到活动 VPN 隧道（{tunnel}）：国内平台会拒绝境外"
                        "出口，请断开 VPN 或将其配置为放行国内域名后重试。"
                    )
                    error["diagnostic"] = {"vpn_tunnel": tunnel, "phase": "download_failure"}
            max_attempts = int(self.controller.settings.get("max_attempts", 3))
            failure_options = _failure_options(current, error, "local_download")
            # Retry inside the same active task; intermediate errors are never a
            # terminal state and cannot trigger a completion notification.
            if (retryable and code != "insufficient_disk_space"
                    and current["options"].get("mode") != "browser"
                    and int(current.get("attempts", 0)) < max_attempts):
                self.store.transition(download_id, "received", error=error, progress=0.0, options=failure_options)
                self._wake.set()
                return
            fallback_codes = {
                "network_error", "download_failed", "source_rate_limited", "invalid_filename",
                "media_download_failed", "media_forbidden", "media_http_error", "media_not_video",
                "media_empty", "media_redirect_denied", "invalid_media_url", "authorization_required",
                "douyin_verification_required",
            }
            if (current["platform_hint"] == "douyin" and current["options"].get("browser_fallback")
                    and current["options"].get("fallback_stage") != "resolver" and code in fallback_codes):
                stage = "resolver" if current["options"].get("mode") == "browser" else "browser"
                fallback_diagnostic = {"fallback_pending": stage, "phase": "download_handoff", "channel": "local_download"}
                # 合并而非覆盖：上面网络诊断附加的 vpn_tunnel 证据要留到终态。
                fallback_diagnostic.update(error.get("diagnostic") or {})
                error["diagnostic"] = fallback_diagnostic
                options = failure_options
                if code in {"media_forbidden", "douyin_verification_required"}:
                    options["rejected_media"] = [hashlib.sha256(value.encode("utf-8")).hexdigest() for value in self._transient_media_hints_for(download_id)][:8]
                self.store.transition(download_id, "awaiting_browser", error=error, options=options)
                self._discard_transient_session(download_id)
                return
            self.store.transition(download_id, "failed", error=error, options=failure_options)

    @staticmethod
    def _validate_options(options: DownloadOptions) -> None:
        request_id = options.get("client_request_id", "")
        if not isinstance(request_id, str) or (request_id and not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", request_id)):
            raise DownloadAdapterError("invalid_client_request_id", "提交凭据格式无效")
        allowed = {
            "quality", "audio_only", "transcribe_after_download", "language", "client_request_id",
            "share_body", "share_hashtags", "share_raw", "referer", "user_agent",
            "mode", "browser_fallback", "fallback_stage", "browser_attempted", "rejected_media",
            "cover_url", "gallery_images", "gallery_done",
        }
        unknown = set(options) - allowed
        if unknown:
            raise DownloadAdapterError("invalid_options", "下载选项包含未支持的字段")
        if "cover_url" in options and (
            not isinstance(options["cover_url"], str) or len(options["cover_url"]) > 4096
        ):
            raise DownloadAdapterError("invalid_options", "封面地址无效")
        for gallery_key in ("gallery_images", "gallery_done"):
            if gallery_key in options and not isinstance(options[gallery_key], list):
                raise DownloadAdapterError("invalid_options", "图集载荷无效")
        for field in ("browser_attempted", "rejected_media"):
            if field not in options:
                continue
            values = options[field]
            if not isinstance(values, list) or len(values) > 8 or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in values):
                raise DownloadAdapterError("invalid_options", "下载候选记录无效")
        if "browser_fallback" in options and not isinstance(options["browser_fallback"], bool):
            raise DownloadAdapterError("invalid_options", "browser_fallback 必须是布尔值")
        if options.get("fallback_stage") not in (None, "browser", "resolver"):
            raise DownloadAdapterError("invalid_options", "下载通道无效")
        if options.get("quality") != "best-available":
            raise DownloadAdapterError("invalid_options", "暂只支持最佳可用质量")
        if options.get("mode") not in ("local", "browser"):
            raise DownloadAdapterError("invalid_options", "mode 必须是 local/browser")
        if not isinstance(options.get("audio_only"), bool):
            raise DownloadAdapterError("invalid_options", "audio_only 必须是布尔值")
        if not isinstance(options.get("transcribe_after_download"), bool):
            raise DownloadAdapterError("invalid_options", "transcribe_after_download 必须是布尔值")
        if not isinstance(options.get("language"), str) or options["language"] not in ("zh", "en", "auto"):
            raise DownloadAdapterError("invalid_options", "language 必须是 zh/en/auto")
        for field in ("referer", "user_agent"):
            if field in options and (not isinstance(options[field], str) or len(options[field]) > 2048):
                raise DownloadAdapterError("invalid_options", f"{field} 必须是短字符串")
