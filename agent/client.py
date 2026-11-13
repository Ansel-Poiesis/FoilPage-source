# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Bounded loopback client; never migrate settings or expose credentials."""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import socket
from pathlib import Path
from typing import Any

from engine.agent_protocol import AGENT_MAX_BODY_BYTES, derive_agent_token


MAX_SETTINGS_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
READ_METHODS = frozenset({
    "info", "tasks.list", "tasks.get", "documents.list", "documents.read", "documents.metadata", "operations.list",
})
WRITE_METHODS = frozenset({"documents.update", "documents.update_metadata", "tasks.cancel", "operations.call"})
PROCESSING_METHODS = frozenset({"tasks.retry"})
ALLOWED_METHODS = READ_METHODS | WRITE_METHODS | PROCESSING_METHODS
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class AgentError(RuntimeError):
    """An intentionally bounded public error, safe for CLI/MCP consumers."""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        self.code = code if _CODE.fullmatch(code) else "request_failed"
        self.message = message[:500]
        self.status = status
        super().__init__(self.message)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def default_data_dir() -> Path:
    """Resolve the user's profile without importing mutable SettingsStore.

    Mirrors engine.settings.default_app_data_dir's legacy fallback on purpose:
    the desktop owns the one-time rename migration (VideoToArticle → FoilPage,
    1.3.8), the CLI only resolves — current root first, legacy root while the
    desktop has not migrated yet (or a downgrade left only the legacy path).
    """
    root = os.environ.get("LOCALAPPDATA")
    base = Path(root).expanduser().resolve() if root else (
        Path(os.environ.get("USERPROFILE", str(Path.home()))).expanduser().resolve()
        / "AppData" / "Local")
    current = base / "FoilPage"
    legacy = base / "VideoToArticle"
    if not current.exists() and legacy.exists():
        return legacy
    return current


def _unprotect_connection_token(value: str) -> str:
    if not value.startswith("dpapi:v1:"):
        # Read-only compatibility for old profiles; the desktop owns migration.
        return value
    try:
        import win32crypt
        blob = base64.b64decode(value[len("dpapi:v1:"):], validate=True)
        _, plaintext = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
        return plaintext.decode("utf-8")
    except Exception:
        raise AgentError("profile_locked", "当前用户无法读取本机连接授权，请由原用户启动 FoilPage") from None


class AgentClient:
    def __init__(self, *, port: int, token: str, timeout: float = 15.0) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise AgentError("invalid_profile", "本机连接端口无效")
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise AgentError("invalid_profile", "本机连接授权缺失，请先启动 FoilPage")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise AgentError("invalid_argument", "连接超时须在 0 到 120 秒之间")
        self.port = port
        self.timeout = float(timeout)
        self._token = token

    @classmethod
    def from_data_dir(cls, data_dir: Path | str | None = None, *, timeout: float = 15.0) -> "AgentClient":
        path = (Path(data_dir).expanduser().resolve() if data_dir is not None else default_data_dir()) / "settings.json"
        try:
            with path.open("rb") as stream:
                encoded = stream.read(MAX_SETTINGS_BYTES + 1)
            if len(encoded) > MAX_SETTINGS_BYTES:
                raise AgentError("invalid_profile", "FoilPage 配置文件过大")
            data = json.loads(encoded.decode("utf-8"))
        except FileNotFoundError:
            raise AgentError("profile_missing", "尚未找到 FoilPage 配置，请先启动应用或指定 --data-dir") from None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise AgentError("invalid_profile", "无法读取 FoilPage 配置，请在应用中检查配置") from None
        if not isinstance(data, dict):
            raise AgentError("invalid_profile", "FoilPage 配置必须是 JSON 对象")
        local_token = data.get("local_api_token", "")
        if not isinstance(local_token, str) or not local_token or len(local_token) > 8192:
            raise AgentError("invalid_profile", "本机连接授权缺失，请先启动 FoilPage")
        local_token = _unprotect_connection_token(local_token)
        try:
            token = derive_agent_token(local_token)
        except ValueError:
            raise AgentError("invalid_profile", "本机连接授权无效") from None
        return cls(port=data.get("local_api_port"), token=token, timeout=timeout)

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method not in ALLOWED_METHODS:
            raise AgentError("unknown_method", "不支持该 Agent 操作")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise AgentError("invalid_argument", "操作参数必须是 JSON 对象")
        try:
            body = json.dumps({"method": method, "params": params}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise AgentError("invalid_argument", "操作参数无法编码为 UTF-8 JSON") from None
        if len(body) > AGENT_MAX_BODY_BYTES:
            raise AgentError("request_too_large", "请求内容过大，请缩短正文")
        # http.client does not inspect HTTP_PROXY/HTTPS_PROXY and does not follow
        # redirects. The host, route and credential scope cannot be overridden.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.request(
                "POST", "/v1/agent/call", body=body,
                headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json; charset=utf-8"},
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise AgentError("redirect_refused", "本机服务返回了重定向，已拒绝转发连接授权", status=response.status)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise AgentError("response_too_large", "本机服务响应过大，请减少查询数量", status=response.status)
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise AgentError("invalid_response", "本机服务响应无效，请检查 FoilPage 版本", status=response.status) from None
            if not isinstance(decoded, dict):
                raise AgentError("invalid_response", "本机服务响应无效", status=response.status)
            if response.status != 200:
                error = decoded.get("error")
                if isinstance(error, dict) and isinstance(error.get("code"), str) and isinstance(error.get("message"), str):
                    # The API errors contain fixed, bounded messages, not request
                    # bodies. Never include the raw body or connection headers.
                    raise AgentError(error["code"], error["message"], status=response.status)
                raise AgentError("request_failed", "本机服务拒绝了该操作", status=response.status)
            if decoded.get("ok") is not True or "result" not in decoded:
                raise AgentError("invalid_response", "本机服务未确认操作结果", status=response.status)
            return decoded["result"]
        except (socket.timeout, TimeoutError):
            raise AgentError("connection_timeout", "FoilPage 未及时响应；写入超时后请先重新读取，避免重复提交") from None
        except (OSError, http.client.HTTPException):
            raise AgentError("connection_failed", "无法连接 FoilPage，请确认同一用户的应用正在后台运行") from None
        finally:
            connection.close()
