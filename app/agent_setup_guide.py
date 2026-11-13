# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Read-only Agent guide with explicit, non-secret runtime configuration."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from engine.settings import default_logs_dir


_IDENTIFIER = re.compile(r"[\w./:-]{1,100}\Z")
_SECRET_LIKE = re.compile(r"(?:\b(?:sk-|Bearer\s)|(?:token|api[_-]?key|cookie|secret)(?:=|:))", re.I)
_ENUM_FIELDS = {
    "whisper_model": {"tiny", "base", "small", "medium", "large-v3"},
    "device": {"cpu", "cuda"},
    "agent_billing_mode": {"byok", "hosted"},
    "agent_provider": {"mimo", "deepseek", "openai"},
    "agent_editing_level": {"light", "balanced", "structured"},
}
_BOOL_FIELDS = ("agent_enabled", "title_optimization_enabled", "agent_vision_enabled",
                "local_api_enabled", "worker_auto_start", "close_to_tray", "task_notifications")


def _path(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve())


def _endpoint(value: Any) -> str:
    """Never export URL credentials, query, fragment or secret-looking paths."""
    if not isinstance(value, str) or not value.strip():
        return "未配置"
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "地址格式待检查"
        hostname = parsed.hostname
        if ":" in hostname:
            hostname = "[" + hostname + "]"
        netloc = hostname + (":" + str(parsed.port) if parsed.port else "")
        path = parsed.path
        if _SECRET_LIKE.search(path) or any(part.lower() in {"token", "key", "secret", "cookie"} for part in path.split("/")):
            path = "/[路径已隐藏]"
        return urlunsplit((parsed.scheme, netloc, path, "", ""))
    except ValueError:
        return "地址格式待检查"


def build_agent_setup_guide(
    settings: Mapping[str, Any],
    *,
    data_dir: str | Path,
    app_version: str,
    packaged: bool | None = None,
    executable: str | Path | None = None,
    source_root: str | Path | None = None,
    guide_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build a fresh snapshot without probes, subprocesses, writes or secret reads."""
    frozen = bool(getattr(sys, "frozen", False)) if packaged is None else packaged
    root = Path(source_root).resolve() if source_root is not None else Path(__file__).resolve().parent.parent
    binary = Path(executable or sys.executable).resolve()
    bundle = Path(getattr(sys, "_MEIPASS", root)) if frozen else root
    document = Path(guide_path) if guide_path is not None else bundle / "docs" / "agent-setup.md"
    try:
        body = document.read_text(encoding="utf-8").strip()
        if not body:
            raise ValueError("empty guide")
    except (OSError, UnicodeError, ValueError):
        return {"ok": False, "error": "配置指南缺失或无法读取，请检查安装文件或更新应用"}

    base = Path(data_dir).expanduser().resolve()
    model_dir = settings.get("model_dir", "")
    model_root = model_dir if isinstance(model_dir, str) and model_dir else os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface"
    paths = {"data_dir": str(base), "settings_file": str(base / "settings.json"),
             "database_path": _path(settings.get("database_path") or base / "library.db"),
             "output_dir": _path(settings.get("output_dir") or base / "output"),
             "download_dir": _path(settings.get("download_dir") or base / "downloads"),
             "model_cache_dir": _path(model_root),
             "worker_log_dir": str(base / "logs"),
             "desktop_log_dir": str(default_logs_dir())}
    config: dict[str, Any] = {}
    for name, choices in _ENUM_FIELDS.items():
        value = settings.get(name)
        config[name] = value if isinstance(value, str) and value in choices else "未配置或需检查"
    for name in _BOOL_FIELDS:
        value = settings.get(name)
        config[name] = value if isinstance(value, bool) else "未配置或需检查"
    for name in ("agent_model", "title_model"):
        value = settings.get(name, "")
        config[name] = value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) and not _SECRET_LIKE.search(value) else "留空使用默认模型" if value == "" else "名称需在本机检查"
    config["agent_base_url"] = _endpoint(settings.get("agent_base_url"))
    config["subscription_server_url"] = _endpoint(settings.get("subscription_server_url"))
    config["hf_endpoint"] = _endpoint(settings.get("hf_endpoint"))
    port = settings.get("local_api_port", 18765)
    config["local_api_port"] = port if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535 else "端口需检查"
    timeout = settings.get("agent_timeout_seconds")
    config["agent_timeout_seconds"] = timeout if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and math.isfinite(timeout) and timeout > 0 else "超时需检查"
    runtime = {"app_version": str(app_version), "mode": "安装版" if frozen else "源码开发版",
               "executable": str(binary), "installation_dir": str(binary.parent) if frozen else "源码运行，无安装目录",
               "source_root": "安装版不含可编辑开发源码" if frozen else str(root),
               "paths": paths, "configuration": config}
    snapshot = json.dumps(runtime, ensure_ascii=False, indent=2)
    markdown = ("# 当前 FoilPage 实例\n\n以下为读取时的运行配置；尚未执行环境检查。"
                "只导出明确白名单字段，未读取或导出 Key、token、cookie、登录码、订单和任务正文。\n\n"
                "```json\n" + snapshot + "\n```\n\n" + body + "\n")
    return {"ok": True, "file_name": "FoilPage-Agent-Setup.md", "markdown": markdown}
