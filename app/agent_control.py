# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Local Agent content and explicit user-side desktop operations.

Document contents are data. Application calls use injected public RPC bindings,
never arbitrary reflection, OS execution or payment administration.
"""
from __future__ import annotations

import math
import re
from typing import Any

from app.agent_operations import AgentOperations, ApplicationOperationError

from engine.account_access import AccountLoginRequired
from engine.account_access import require_account_access
from engine.agent_protocol import (
    AGENT_MAX_BODY_BYTES, AGENT_MAX_TEXT_BYTES, AGENT_PROTOCOL_VERSION,
    DocumentRevisionConflict, DocumentTooLargeError, DocumentUnavailableError,
)
from engine.download_store import DownloadNotFoundError, DownloadStateError
from engine.downloader import DownloadAdapterError
from engine.queue_store import JobNotFoundError, QueueStateError

_TASK_ID = re.compile(r"(job|dl):([a-f0-9]{12})")
_REVISION = re.compile(r"[a-f0-9]{64}")
_ERROR_CODE = re.compile(r"[A-Za-z0-9_-]{1,80}")
_STATUSES = frozenset({
    "queued", "running", "retryable", "done", "failed", "cancelled", "received",
    "resolving", "awaiting_browser", "downloading", "imported", "transcribing",
})
METHODS = (
    "info", "tasks.list", "tasks.get", "documents.list", "documents.read",
    "documents.update", "documents.metadata", "documents.update_metadata",
    "tasks.cancel", "tasks.retry",
    "operations.list", "operations.call",
)
_SCHEMAS = {
    "info": ({}, set()),
    "tasks.list": ({"query": str, "status": str, "limit": int}, set()),
    "tasks.get": ({"task_id": str}, {"task_id"}),
    "documents.list": ({"query": str, "limit": int}, set()),
    "documents.read": ({"task_id": str, "tab": str}, {"task_id"}),
    "documents.update": (
        {"task_id": str, "tab": str, "text": str, "expected_revision": str},
        {"task_id", "tab", "text", "expected_revision"},
    ),
    "documents.metadata": ({"task_id": str}, {"task_id"}),
    "documents.update_metadata": (
        {"task_id": str, "title": str, "tags": list, "category": str, "expected_revision": str},
        {"task_id", "expected_revision"},
    ),
    "tasks.cancel": ({"task_id": str}, {"task_id"}),
    "tasks.retry": ({"task_id": str}, {"task_id"}),
    "operations.list": ({"category": str}, set()),
    "operations.call": ({"operation": str, "arguments": dict, "confirm": bool}, {"operation", "arguments"}),
}


class AgentControlError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def _invalid() -> AgentControlError:
    return AgentControlError(400, "invalid_request", "Agent 请求参数无效")


def _public_task(row: dict, *, kind: str | None = None) -> dict:
    kind = kind or ("download" if row.get("kind") == "download" else "transcription")
    prefix = "dl:" if kind == "download" else "job:"
    progress = row.get("progress", 0)
    progress = float(progress) if isinstance(progress, (int, float)) and not isinstance(progress, bool) else 0.0
    if not math.isfinite(progress):
        progress = 0.0
    # Desktop's empty download-title fallback is the source URL.  Native Agent
    # lists must not turn a missing title into disclosure of a signed URL.
    title = (row.get("title") or "未命名下载") if kind == "download" else (
        row.get("display_title") or row.get("source_title") or "未命名"
    )
    result = {
        "task_id": prefix + str(row.get("task_id") or row.get("id") or ""),
        "kind": kind,
        "title": str(title)[:240],
        "status": str(row.get("status") or ""),
        "progress": max(0.0, min(1.0, progress)),
        "updated_at": str(row.get("updated_at") or ""),
        "archived": bool(row.get("archived", False)),
        "cancel_requested": bool(row.get("cancel_requested", False)),
    }
    for key in ("attempts", "max_attempts"):
        value = row.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = max(0, value)
    # Match the desktop projection for completed legacy download retries.
    # Their transient error remains in storage for diagnostics.
    error = None if kind == "download" and row.get("status") == "done" else row.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
        result["error_code"] = code
    return result


class AgentControl:
    def __init__(self, controller, coordinator, operations=None):
        self.controller = controller
        self.coordinator = coordinator
        self.operations = operations if isinstance(operations, AgentOperations) else AgentOperations(operations or {})

    def call(self, method: Any, params: Any) -> Any:
        activity = getattr(self.controller, "agent_activity", None)
        operation = params.get("operation", method) if isinstance(params, dict) else method
        try:
            result = self._call(method, params)
        except Exception:
            if activity:
                try:
                    activity.record(operation, False)
                except OSError:
                    pass
            raise
        if activity:
            try:
                revision = result.get('revision', '') if isinstance(result, dict) else ''
                category = operation.split('.', 1)[0] if isinstance(operation, str) else 'application'
                activity.record(operation, not (isinstance(result, dict) and result.get('ok') is False), category, revision)
            except OSError:
                pass
        return result

    def _call(self, method: Any, params: Any) -> Any:
        if not isinstance(method, str):
            raise _invalid()
        if method not in _SCHEMAS:
            raise AgentControlError(404, "method_not_found", "Agent 操作不存在")
        schema, required = _SCHEMAS[method]
        if (not isinstance(params, dict) or set(params) - set(schema)
                or not required.issubset(params)):
            raise _invalid()
        for key, value in params.items():
            # Exact types reject JSON booleans substituted for integers and null.
            if type(value) is not schema[key]:
                raise _invalid()
            if isinstance(value, str):
                try:
                    value.encode("utf-8")
                except UnicodeError as exc:
                    raise _invalid() from exc
        if "limit" in params and not 1 <= params["limit"] <= 200:
            raise _invalid()
        if "query" in params and len(params["query"]) > 1000:
            raise _invalid()
        if "status" in params and params["status"] not in _STATUSES | {""}:
            raise _invalid()
        if "tab" in params and params["tab"] not in {"raw", "final"}:
            raise _invalid()
        if "expected_revision" in params and not _REVISION.fullmatch(params["expected_revision"]):
            raise _invalid()
        if "tags" in params and any(type(value) is not str for value in params["tags"]):
            raise _invalid()
        if "task_id" in params and not _TASK_ID.fullmatch(params["task_id"]):
            raise _invalid()
        if method.startswith("documents.") and "task_id" in params and not params["task_id"].startswith("job:"):
            raise _invalid()
        if method == "documents.update_metadata" and not ({"title", "tags", "category"} & set(params)):
            raise _invalid()
        try:
            return self._dispatch(method, params)
        except AgentControlError:
            raise
        except ApplicationOperationError as exc:
            raise AgentControlError(exc.status, exc.code, str(exc)) from None
        except DocumentRevisionConflict as exc:
            raise AgentControlError(409, "revision_conflict", "文稿已更改，请重新读取后再保存") from exc
        except DocumentTooLargeError as exc:
            raise AgentControlError(413, "text_too_large", "文稿超过 2000000 字节限制") from exc
        except AccountLoginRequired as exc:
            raise AgentControlError(403, "login_required", "请先登录 FoilPage，登录后继续") from exc
        except (JobNotFoundError, DownloadNotFoundError) as exc:
            raise AgentControlError(404, "not_found", "任务不存在") from exc
        except (QueueStateError, DownloadStateError) as exc:
            raise AgentControlError(409, "invalid_state", "当前任务状态不支持此操作") from exc
        except (FileNotFoundError, UnicodeError, DocumentUnavailableError) as exc:
            raise AgentControlError(404, "document_unavailable", "文稿不可用") from exc
        except DownloadAdapterError as exc:
            # Resolver errors can contain sensitive URLs; expose only a stable code.
            raise AgentControlError(409, "retry_unavailable", "下载重试未完成，请在任务页检查") from exc
        except ValueError as exc:
            raise _invalid() from exc
        except OSError as exc:
            raise AgentControlError(409, "document_unavailable", "文稿无法读写，请检查本地文件") from exc

    def _job(self, task_id: str, *, done: bool = False) -> dict:
        job = self.controller.queue.get_job(task_id[4:])
        if not job:
            raise AgentControlError(404, "not_found", "任务不存在")
        if done and job.get("status") != "done":
            raise AgentControlError(409, "invalid_state", "仅已完成任务可以编辑文稿")
        return job

    def _task(self, task_id: str) -> dict:
        if task_id.startswith("job:"):
            job = self._job(task_id)
            if not job.get("source_title"):
                from engine.agent_protocol import document_metadata
                job = {**job, "display_title": document_metadata(job)["title"]}
            return _public_task(job, kind="transcription")
        item = self.coordinator.get(task_id[3:])
        if not item:
            raise AgentControlError(404, "not_found", "任务不存在")
        return _public_task(item, kind="download")

    def _dispatch(self, method: str, params: dict) -> Any:
        if method == "info":
            facade = self.operations.facade
            return {
                "app": "FoilPage", "protocol_version": AGENT_PROTOCOL_VERSION, "methods": list(METHODS),
                "limits": {"body_bytes": AGENT_MAX_BODY_BYTES, "text_bytes": AGENT_MAX_TEXT_BYTES, "list_limit": 200},
                "capabilities": {"local_only": True, "content_revision_required": True,
                                 "metadata_revision_required": True, "arbitrary_paths": False,
                                 "settings_write": "save_agent_settings" in facade,
                                 "model_calls": "batch_agent_translate" in facade,
                                 "payment_operations": "create_alipay_order" in facade,
                                 "application_operations": bool(facade), "destructive_confirmation_required": True,
                                 "retry_may_trigger_processing": True},
            }
        if method == "operations.list":
            return self.operations.list(category=params.get("category", ""))
        if method == "operations.call":
            return self.operations.call(params["operation"], params["arguments"], confirm=params.get("confirm", False))
        if method in {"tasks.list", "documents.list"}:
            return self._list(method, params)
        task_id = params["task_id"]
        if method == "tasks.get":
            return self._task(task_id)
        if method.startswith("documents."):
            with self.controller.media_action_lock:
                job = self._job(task_id, done=True)
                if method == "documents.read":
                    value = self.controller.read_document_text(task_id[4:], params.get("tab", "final"))
                    return {"task_id": task_id, "title": self.controller.read_document_metadata(task_id[4:])["title"],
                            **{key: value[key] for key in ("text", "size", "tab", "revision")}}
                if method == "documents.update":
                    value = self.controller.update_document_text(
                        task_id[4:], params["tab"], params["text"], expected_revision=params["expected_revision"],
                    )
                    return {"task_id": task_id, "tab": params["tab"],
                            **{key: value[key] for key in ("size", "revision")}}
                if method == "documents.metadata":
                    return {"task_id": task_id, **self.controller.read_document_metadata(task_id[4:])}
                value = self.controller.update_document_metadata(
                    task_id[4:], **{key: value for key, value in params.items() if key != "task_id"},
                )
                return {"task_id": task_id, **value}
        if method == "tasks.retry":
            require_account_access(dict(self.controller.settings), self.controller.settings_store.base_dir)
        if task_id.startswith("job:"):
            with self.controller.media_action_lock:
                self._job(task_id)
                if method == "tasks.cancel":
                    job = self.controller.queue.request_cancel(task_id[4:])
                else:
                    job = self.controller.queue.requeue(task_id[4:])
                return _public_task(job, kind="transcription")
        self._task(task_id)
        item = self.coordinator.cancel(task_id[3:]) if method == "tasks.cancel" else self.coordinator.retry(task_id[3:])
        return _public_task(item, kind="download")

    def _list(self, method: str, params: dict) -> dict:
        query = params.get("query", "").strip().casefold()
        limit = params.get("limit", 50)
        items = []
        with self.controller.media_action_lock:
            rows = self.controller.task_rows(include_archived=True)
            for row in rows:
                if method == "tasks.list":
                    public = _public_task(row)
                    if params.get("status") and public["status"] != params["status"]:
                        continue
                    search = public["title"]
                else:
                    if row.get("kind") != "transcription" or row.get("status") != "done":
                        continue
                    has = {}
                    for tab in ("final", "raw"):
                        try:
                            self.controller._document_path(row["task_id"], tab)
                            has[tab] = True
                        except (ValueError, OSError, QueueStateError):
                            has[tab] = False
                    if not any(has.values()):
                        continue
                    metadata = self.controller.read_document_metadata(row["task_id"])
                    public = {"task_id": "job:" + row["task_id"], **metadata,
                              "has_final": has["final"], "has_raw": has["raw"],
                              "updated_at": str(row.get("updated_at") or ""), "archived": bool(row.get("archived"))}
                    search = public["title"] + " " + public["category"] + " " + " ".join(public["tags"])
                if query and query not in search.casefold():
                    continue
                items.append(public)
        items.sort(key=lambda value: (value["updated_at"], value["task_id"]), reverse=True)
        return {"items": items[:limit], "total": len(items)}
