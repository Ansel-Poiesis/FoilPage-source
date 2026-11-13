# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Local validation shared by the CLI and MCP tool handlers."""
from __future__ import annotations

import re
from typing import Any

from agent.client import AgentError
from engine.agent_protocol import AGENT_MAX_TEXT_BYTES


_TASK_ID = re.compile(r"^(?:job|dl):[a-f0-9]{12}$")
_REVISION = re.compile(r"^[a-f0-9]{64}$")


def task_id(value: str) -> str:
    if not isinstance(value, str) or not _TASK_ID.fullmatch(value):
        raise AgentError("invalid_argument", "任务 ID 必须是 job: 或 dl: 加 12 位小写十六进制")
    return value


def revision(value: str) -> str:
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        raise AgentError("invalid_argument", "请提供最近读取返回的 64 位 revision")
    return value


def tab(value: str) -> str:
    if value not in ("final", "raw"):
        raise AgentError("invalid_argument", "正文类型只能是 final 或 raw")
    return value


def text(value: str) -> str:
    if not isinstance(value, str):
        raise AgentError("invalid_argument", "正文必须是文本")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise AgentError("invalid_argument", "正文必须是有效 UTF-8") from None
    if size > AGENT_MAX_TEXT_BYTES:
        raise AgentError("request_too_large", "正文超过 2 MB，请缩短内容")
    return value


def listing(query: str, limit: int) -> dict[str, Any]:
    if not isinstance(query, str) or len(query) > 1000:
        raise AgentError("invalid_argument", "搜索文本须在 1000 字以内")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise AgentError("invalid_argument", "查询数量须在 1 到 200 之间")
    return {"query": query, "limit": limit}


def status(value: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or any(ord(char) < 32 for char in value):
        raise AgentError("invalid_argument", "任务状态参数无效")
    return value


def metadata_changes(
    *, title: str | None = None, tags: list[str] | None = None, category: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if title is not None:
        if not isinstance(title, str) or not title.strip() or len(title) > 240:
            raise AgentError("invalid_argument", "标题须为 1 到 240 字的文本")
        result["title"] = title
    if category is not None:
        if not isinstance(category, str) or not category.strip() or len(category) > 40:
            raise AgentError("invalid_argument", "分类须为 1 到 40 字的文本")
        result["category"] = category
    if tags is not None:
        if not isinstance(tags, list) or len(tags) > 20 or any(
            not isinstance(value, str) or not value.strip() or len(value) > 24 for value in tags
        ):
            raise AgentError("invalid_argument", "最多可设置 20 个标签，每个标签为 1 到 24 字")
        result["tags"] = tags
    if not result:
        raise AgentError("invalid_argument", "请至少指定一个标题、标签或分类字段")
    return result
