# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Shared, credential-free contract for the local Agent control transport."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping

AGENT_PROTOCOL_VERSION = "1"
AGENT_TOKEN_CONTEXT = b"shengjian-agent-control-v1"
AGENT_MAX_BODY_BYTES = 3 * 1024 * 1024
AGENT_MAX_TEXT_BYTES = 2_000_000


class DocumentRevisionConflict(ValueError):
    """A caller attempted to save content read before the current revision."""

    def __init__(self, current_revision: str):
        super().__init__("文稿已更改，请重新读取后再保存")
        self.current_revision = current_revision


class DocumentTooLargeError(ValueError):
    """A document exceeds the bounded local editor transport."""


class DocumentUnavailableError(ValueError):
    """Keep the desktop editor's validation contract for missing owned assets."""


def document_metadata(job: Mapping[str, Any]) -> dict:
    """Canonical editable metadata, independent of body revisions or secrets."""
    from pathlib import Path
    value = {
        "title": str(job.get("source_title") or Path(str(job.get("source_path") or "")).stem),
        "tags": list(job.get("tags") or []),
        "category": str(job.get("result_category") or "未分类"),
    }
    raw = json.dumps({"job_id": str(job.get("id") or ""), **value}, ensure_ascii=False,
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**value, "revision": hashlib.sha256(raw).hexdigest()}


def document_revision(job_id: str, tab: str, raw_bytes: bytes) -> str:
    """An opaque revision is scoped to one registered document and one tab."""
    bare_id = job_id[4:] if job_id.startswith("job:") else job_id
    context = json.dumps(["shengjian-document-v1", bare_id, tab], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(context + b"\x00" + raw_bytes).hexdigest()


def derive_agent_token(local_api_token: str) -> str:
    """Scope a local profile's connection secret to the Agent API only."""
    if not isinstance(local_api_token, str) or not local_api_token:
        raise ValueError("本地连接令牌不可用")
    return hmac.new(local_api_token.encode("utf-8"), AGENT_TOKEN_CONTEXT, hashlib.sha256).hexdigest()
