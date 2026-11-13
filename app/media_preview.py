# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Session-scoped loopback playback of registered local media only."""
from __future__ import annotations

import hashlib
import mimetypes
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from engine.video_frames import extract_frame_data_urls
from engine.media_store import MediaStoreError


def byte_range(value: str, size: int) -> tuple[int, int]:
    """Accept one RFC 9110 byte range, including suffix/open-ended requests."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not size or not any(match.groups()):
        raise ValueError("invalid range")
    first, last = match.groups()
    if first:
        start, end = int(first), min(int(last) if last else size - 1, size - 1)
    else:
        count = int(last)
        if count <= 0:
            raise ValueError("invalid suffix")
        start, end = max(0, size - count), size - 1
    if start >= size or start > end:
        raise ValueError("unsatisfiable range")
    return start, end


class MediaPreviewServer:
    """Do not expose the extension token, filesystem paths, or arbitrary files."""

    def __init__(self, media_store, cache_dir: Path, *, ffmpeg_path: str = "") -> None:
        self.media = media_store
        self.cache_dir = Path(cache_dir)
        self.ffmpeg_path = ffmpeg_path
        self._token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._covers: dict[str, threading.Lock] = {}
        self._decode_slots = threading.BoundedSemaphore(2)
        self._failed_covers: set[str] = set()
        # TASK-8.17 工作台平台封面 / TASK-8.18 图集网格：仅登记桌面下载落位
        # 的图片文件（id → 文件列表），handler 只按登记表取路径，不解析任何
        # 用户输入的文件名。
        self._task_covers: dict[str, list[Path]] = {}
        self._server = None

    def _source(self, media_id: str):
        if not re.fullmatch(r"[a-f0-9]{12,64}", media_id):
            raise FileNotFoundError
        try:
            item = self.media.get_media(media_id)
        except (KeyError, ValueError, LookupError, MediaStoreError):
            raise FileNotFoundError from None
        path = Path(item["source_path"])
        if item["media_type"] != "video" or not path.is_file():
            raise FileNotFoundError
        return item, path

    def start(self) -> None:
        with self._lock:
            if self._server:
                return
            owner = self

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass  # URLs carry session capabilities; never log them.

                def do_HEAD(self):
                    self._serve(head=True)

                def do_GET(self):
                    self._serve(head=False)

                def _serve(self, *, head: bool):
                    parsed = urlsplit(self.path)
                    match = re.fullmatch(r"/(video|cover|taskcover)/([a-f0-9]{12,64})", parsed.path)
                    token = parse_qs(parsed.query).get("key", [""])[0]
                    # Reject requests routed through DNS rebinding. Only an
                    # opaque session capability handed to this UI can play.
                    expected_host = f"127.0.0.1:{self.server.server_address[1]}"
                    if self.headers.get("Host", "") != expected_host or not secrets.compare_digest(token, owner._token):
                        self.send_error(403)
                        return
                    if not match:
                        self.send_error(404)
                        return
                    try:
                        if match[1] == "taskcover":
                            registered = owner._task_covers.get(match[2]) or []
                            index = parse_qs(parsed.query).get("i", ["0"])[0]
                            path = registered[int(index)] if index.isdigit() and int(index) < len(registered) else None
                        else:
                            item, source = owner._source(match[2])
                            path = owner.cover(item, source) if match[1] == "cover" else source
                        if path is None:
                            raise FileNotFoundError
                        # Open before headers. A missing/relocated source must
                        # not leave a seemingly successful empty video response.
                        stream = path.open("rb")
                    except (OSError, ValueError, LookupError):
                        self.send_error(404)
                        return
                    with stream:
                        size = path.stat().st_size
                        start, end = 0, size - 1
                        ranged = bool(self.headers.get("Range"))
                        try:
                            if ranged:
                                start, end = byte_range(self.headers["Range"], size)
                        except ValueError:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{size}")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        self.send_response(206 if ranged else 200)
                        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Content-Length", str(max(0, end - start + 1)))
                        self.send_header("Cache-Control", "private, no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Referrer-Policy", "no-referrer")
                        if ranged:
                            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                        self.end_headers()
                        if head:
                            return
                        stream.seek(start)
                        remaining = end - start + 1
                        try:
                            while remaining > 0:
                                chunk = stream.read(min(256 * 1024, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            self._server = server
            threading.Thread(target=server.serve_forever, name="local-video-preview", daemon=True).start()

    def urls(self, media_id: str) -> dict:
        self._source(media_id)
        self.start()
        port = self._server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        return {"preview_url": f"{base}/video/{media_id}?key={self._token}",
                "thumbnail_url": f"{base}/cover/{media_id}?key={self._token}"}

    def _register_task_files(self, download_id: str, paths: list[Path]) -> str:
        if not re.fullmatch(r"[a-f0-9]{12,64}", download_id):
            raise FileNotFoundError
        covers = [Path(path) for path in paths if Path(path).is_file()]
        if not covers:
            raise FileNotFoundError
        with self._lock:
            self._task_covers[download_id] = covers
            # 会话内登记表有界：工作台一次最多展示数百任务。
            while len(self._task_covers) > 600:
                self._task_covers.pop(next(iter(self._task_covers)))
        self.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def task_cover_url(self, download_id: str, path: Path) -> str:
        """Register a task cover file (bridge-verified) and return its URL.

        路径只能来自下载库的 cover_path（桌面自己落位的文件）；这里只做
        存在性复核与登记，handler 侧无文件名解析。
        """
        base = self._register_task_files(download_id, [path])
        return f"{base}/taskcover/{download_id}?key={self._token}"

    def gallery_image_urls(self, download_id: str, paths: list[Path]) -> list[str]:
        """图集网格（TASK-8.18）：登记已落盘图片并返回带序号的 token URL。"""
        base = self._register_task_files(download_id, paths)
        return [
            f"{base}/taskcover/{download_id}?key={self._token}&i={index}"
            for index in range(len(self._task_covers[download_id]))
        ]

    def cover(self, item, source: Path) -> Path | None:
        stat = source.stat()
        fingerprint = hashlib.sha256(f"{item['id']}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
        target = self.cache_dir / f"{fingerprint}.jpg"
        with self._lock:
            lock = self._covers.setdefault(fingerprint, threading.Lock())
        with lock:
            if target.is_file():
                return target
            if fingerprint in self._failed_covers:
                return None
            with self._decode_slots:
                frames = extract_frame_data_urls(source, [min(1.0, float(item.get('duration_sec') or 0) * .05)],
                                                 ffmpeg_path=self.ffmpeg_path, timeout_sec=10)
            if not frames:
                self._failed_covers.add(fingerprint)
                return None
            import base64
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            data = base64.b64decode(str(frames[0]["data_url"]).split(",", 1)[1])
            # Keep the runtime cache bounded to 300 small thumbnails, outside
            # source/Poiesis. Only this module's opaque JPEG names are pruned.
            existing = sorted(self.cache_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
            for old in existing[:-299]:
                if re.fullmatch(r"[a-f0-9]{64}\.jpg", old.name):
                    old.unlink(missing_ok=True)
            target.write_bytes(data)
            return target

    def stop(self) -> None:
        with self._lock:
            server, self._server = self._server, None
        if server:
            server.shutdown()
            server.server_close()
