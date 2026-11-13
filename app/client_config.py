# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""桌面端热切换配置存储：拉取、验签、落盘缓存与 /health 透传。

engine.client_config 负责验签与 schema；本模块只管生命周期与降级顺序：
server（已验签最新）→ cache（上次已验签，加载时重验签防篡改）→ 内置默认。
任何失败保留旧值并记 desktop.log，绝不让配置问题影响下载链路。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from engine.client_config import (
    DEFAULT_CLIENT_CONFIG,
    ClientConfigError,
    fetch_config,
    verify_payload,
)

REFRESH_INTERVAL_SECONDS = 6 * 3600
# Capability baseline for the existing signed-config protocol across the
# public source version reset. Display/release versions are synchronized separately.
CLIENT_CONFIG_COMPATIBILITY_VERSION = "1.3.13"
log = logging.getLogger("shengjian.desktop")


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(value or "").split(".")[:4]:
        parts.append(int(piece) if piece.isdigit() else 0)
    return tuple(parts) or (0,)


class ClientConfigStore:
    """线程安全；start() 起后台线程（启动即拉取一次，之后每 6 小时）。"""

    def __init__(
        self,
        base_dir: str | Path,
        server_url: Callable[[], str],
        app_version: str = "",
    ) -> None:
        self._cache_path = Path(base_dir) / "client-config.json"
        self._server_url = server_url
        self._app_version = str(app_version or "")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._effective: dict[str, Any] = {
            "version": 0, "config": DEFAULT_CLIENT_CONFIG, "source": "default",
        }
        self._load_cache()

    # ---------------------------------------------------------------- state
    def _gate(self, verified: dict[str, Any]) -> bool:
        """min_app_version 门槛 + version 单调；不满足即忽略本次内容。"""
        app = self._app_version
        if app and _version_tuple(app) < _version_tuple(verified["min_app_version"]):
            log.info("client_config ignored: app %s < min_app_version %s",
                     app, verified["min_app_version"])
            return False
        with self._lock:
            if int(verified["version"]) <= int(self._effective["version"]):
                return False
        return True

    def _apply(self, verified: dict[str, Any], source: str) -> None:
        with self._lock:
            self._effective = {
                "version": int(verified["version"]),
                "config": verified["config"],
                "source": source,
            }

    def _load_cache(self) -> None:
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            verified = verify_payload(payload)
        except (OSError, ValueError, ClientConfigError):
            return
        if self._gate(verified):
            self._apply(verified, "cache")
            log.info("client_config loaded from cache version=%s", verified["version"])

    # -------------------------------------------------------------- refresh
    def refresh_once(self) -> bool:
        base = str(self._server_url() or "").strip()
        if not base:
            return False
        try:
            payload = fetch_config(base)
            verified = verify_payload(payload)
        except (ClientConfigError, OSError, ValueError) as exc:
            log.info("client_config refresh failed (kept %s version=%s): %s",
                     self._effective.get("source"), self._effective.get("version"),
                     str(exc)[:160])
            return False
        if not self._gate(verified):
            return False
        self._apply(verified, "server")
        try:
            # 原样存签名响应：加载时用同一公钥重验签，防本地篡改。
            self._cache_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
        except OSError:
            pass
        log.info("client_config refreshed version=%s flags=%s",
                 verified["version"], verified["config"].get("flags"))
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception:  # 后台线程永不冒泡
                log.exception("client_config refresh crashed")
            self._stop.wait(REFRESH_INTERVAL_SECONDS)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="client-config-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, Any]:
        """给 /health 透传（本地 127.0.0.1 信任边界内，扩展据此消费）。"""
        with self._lock:
            return {
                "version": self._effective["version"],
                "config": self._effective["config"],
                "source": self._effective["source"],
            }

    def covers_enabled(self) -> bool:
        """TASK-8.17 封面功能云开关：本地提交时判定，关闭即不收封面。"""
        with self._lock:
            flags = self._effective["config"].get("flags")
            return bool(isinstance(flags, dict) and flags.get("covers_enabled"))

    def gallery_enabled(self) -> bool:
        """TASK-8.18 图集云开关：关闭时本地 API 不收图集载荷（与现状一致）。"""
        with self._lock:
            flags = self._effective["config"].get("flags")
            return bool(isinstance(flags, dict) and flags.get("douyin_gallery_enabled"))
