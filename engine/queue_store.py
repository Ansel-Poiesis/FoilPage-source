# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""SQLite-backed job queue with explicit transitions and worker leases."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, ContextManager, Iterator, Optional

from . import validate
from .contracts import Job, Manifest

SCHEMA_VERSION = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    source_path      TEXT NOT NULL,
    output_dir       TEXT NOT NULL,
    status           TEXT NOT NULL,
    whisper_model    TEXT NOT NULL,
    device           TEXT NOT NULL,
    language         TEXT NOT NULL,
    content_mode     TEXT NOT NULL,
    progress         REAL NOT NULL DEFAULT 0,
    source_title     TEXT,
    result_category  TEXT NOT NULL DEFAULT '未分类',
    error            TEXT,
    raw_path         TEXT,
    final_path       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    worker_id        TEXT,
    claimed_at       TEXT,
    heartbeat_at     TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created
    ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS manifests (
    job_id         TEXT PRIMARY KEY,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT,
    backend        TEXT NOT NULL,
    model          TEXT NOT NULL,
    language       TEXT NOT NULL,
    duration_sec   REAL NOT NULL,
    raw_path       TEXT NOT NULL,
    final_path     TEXT NOT NULL,
    content_mode   TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    metadata_json  TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
"""

_MIGRATION_COLUMNS = {
    "progress": "REAL NOT NULL DEFAULT 0",
    "source_title": "TEXT",
    "result_category": "TEXT NOT NULL DEFAULT '未分类'",
    "worker_id": "TEXT",
    "claimed_at": "TEXT",
    "heartbeat_at": "TEXT",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "max_attempts": "INTEGER NOT NULL DEFAULT 3",
    "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
    "archived": "INTEGER NOT NULL DEFAULT 0",
    "tags": "TEXT NOT NULL DEFAULT '[]'",
    "stage_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    # TASK-8.24：重新转写按单任务覆盖 Agent 整理开关（None=沿用全局设置）。
    "body_enabled_override": "INTEGER",
    # 2026-11-02：重新转写可按单任务覆盖整理力度（None=沿用全局设置）。
    "agent_level_override": "TEXT",
}


# A user_version value alone cannot prove that a partially migrated or shared
# database is ready. Match every queue-owned column and constraint before using
# the read-only startup path; unrelated MediaStore tables are not queue schema.
_SCHEMA_COLUMNS = {
    "jobs": {
        "id": ("TEXT", 0, None, 1),
        **{name: ("TEXT", 1, None, 0) for name in (
            "source_path", "output_dir", "status", "whisper_model", "device",
            "language", "content_mode", "created_at", "updated_at",
        )},
        **{name: ("TEXT", 0, None, 0) for name in ("error", "raw_path", "final_path")},
    },
    "manifests": {
        "job_id": ("TEXT", 0, None, 1),
        **{name: ("TEXT", 1, None, 0) for name in (
            "source_path", "backend", "model", "language", "raw_path", "final_path",
            "content_mode", "prompt_version", "created_at",
        )},
        "source_sha256": ("TEXT", 0, None, 0),
        "duration_sec": ("REAL", 1, None, 0),
        "metadata_json": ("TEXT", 1, "'{}'", 0),
    },
}
for _name, _definition in _MIGRATION_COLUMNS.items():
    _declaration, _, _default = _definition.partition(" DEFAULT ")
    _SCHEMA_COLUMNS["jobs"][_name] = (
        _declaration.split()[0], int("NOT NULL" in _declaration), _default or None, 0,
    )


# Persist only documented result metadata, never arbitrary settings/provider payloads.
_STAGE_STRING_FIELDS = frozenset({"body_status", "title_status", "original_source_title",
    "optimized_source_title", "body_billing_mode", "title_billing_mode",
    "body_error_code", "title_error_code", "body_error", "title_error"})
_STAGE_BOOL_FIELDS = frozenset({"body_enabled_snapshot", "title_enabled_snapshot",
    # 积分耗尽标记：呈现层据此显示"请充值"而不是笼统的"处理失败"。
    "body_quota_exceeded", "title_quota_exceeded"})
_STAGE_INT_FIELDS = frozenset({"body_usage_units", "title_usage_units", "body_request_count", "title_request_count"})
_STAGE_FIELDS = _STAGE_STRING_FIELDS | _STAGE_BOOL_FIELDS | _STAGE_INT_FIELDS


def _stage_metadata(value: dict, *, strict: bool = False) -> dict:
    output = {}
    for key in _STAGE_FIELDS & value.keys():
        item = value[key]
        valid = (isinstance(item, str) if key in _STAGE_STRING_FIELDS else
                 isinstance(item, bool) if key in _STAGE_BOOL_FIELDS else
                 isinstance(item, int) and not isinstance(item, bool) and item >= 0)
        if valid and key.endswith("_status"):
            valid = item in {"disabled", "pending", "running", "done", "skipped", "failed", "cancelled"}
        if valid and key.endswith("_billing_mode"):
            valid = item in {"byok", "hosted", "none"}
        if valid:
            output[key] = item
        elif strict:
            raise validate.ContractValidationError(f"AI 阶段 {key} 元数据非法")
    return output


_MANIFEST_STRING_FIELDS = frozenset({
    "source_title", "original_source_title", "segments_path", "agent_profile",
    "agent_model", "agent_status", "agent_prompt_version", "agent_editing_level",
    "title_status", "title_skill_version", "title_model", "title_error",
    # TASK-8.24 文章翻译：状态/方向/语言与产物路径。
    "translation_status", "translation_direction", "translation_lang",
    "translation_path", "translation_segments_path", "translation_error",
})
_MANIFEST_BOOL_FIELDS = frozenset({
    "agent_vision_used", "agent_fidelity_fallback",
    "text_outputs_cleared",
    "asr_low_confidence",
})
# 无效转写检测（1.3.12）：转写置信度指标（None 表示引擎未提供）。
_MANIFEST_FLOAT_FIELDS = frozenset({
    "asr_avg_logprob", "asr_no_speech_prob",
})
_MANIFEST_METADATA_FIELDS = (
    _MANIFEST_STRING_FIELDS | _MANIFEST_BOOL_FIELDS |
    frozenset({"agent_vision_frames", "agent_tags"}) |
    _MANIFEST_FLOAT_FIELDS
)


def _manifest_metadata(value: dict, *, strict: bool = False) -> dict:
    metadata = _stage_metadata(value, strict=strict)
    for key in _MANIFEST_METADATA_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if key in _MANIFEST_STRING_FIELDS:
            valid = isinstance(item, str)
        elif key in _MANIFEST_BOOL_FIELDS:
            valid = isinstance(item, bool)
        elif key in _MANIFEST_FLOAT_FIELDS:
            valid = (
                item is None
                or (isinstance(item, (int, float)) and not isinstance(item, bool))
            )
        elif key == "agent_vision_frames":
            valid = isinstance(item, int) and not isinstance(item, bool) and item >= 0
        else:
            valid = isinstance(item, list) and all(isinstance(tag, str) for tag in item)
        if valid:
            metadata[key] = item
        elif strict:
            raise validate.ContractValidationError(f"Manifest.{key} 元数据类型非法")
    return metadata

_UPDATE_TRANSITIONS = {
    "queued": frozenset({"cancelled"}),
    "running": frozenset({"retryable", "failed", "cancelled"}),
    "retryable": frozenset({"failed", "cancelled"}),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "done": frozenset(),
}


class QueueStoreError(RuntimeError):
    pass


class JobNotFoundError(QueueStoreError):
    pass


class QueueStateError(QueueStoreError):
    pass


class CancellationRequestedError(QueueStateError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _error_json(error: Optional[dict]) -> Optional[str]:
    return json.dumps(error, ensure_ascii=False) if error else None


def _source_paths_equivalent(first: str, second: str) -> bool:
    if first == second:
        return True
    try:
        # Windows resolves 8.3 names, case and reparse aliases to the same
        # canonical path. Different files remain different even if names match.
        return Path(first).resolve() == Path(second).resolve()
    except (OSError, RuntimeError, ValueError):
        # A path that cannot be resolved must never bypass source ownership.
        return False


class QueueStore:
    def __init__(self, db_path: str = ":memory:", *, busy_timeout_ms: int = 5000):
        if db_path != ":memory:" and not str(db_path).startswith("file:"):
            db_path = str(Path(db_path).expanduser().resolve())
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            timeout=max(busy_timeout_ms, 0) / 1000,
        )
        try:
            self._conn.row_factory = sqlite3.Row
            # Refuse a newer database before WAL or schema writes. An older
            # application cannot safely migrate it back to its own version.
            self._supported_schema_version()
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._enable_wal(max(0, int(busy_timeout_ms)))
            # Complete databases are common to desktop/worker/Agent connections.
            # Opening one must not contend with real queue writers just to stamp
            # the same version again. Old/incomplete databases still take the
            # original serialized, idempotent migration path.
            if not self._schema_is_current():
                self._conn.executescript(_SCHEMA)
                self._migrate()
        except BaseException:
            self._conn.close()
            raise

    def _enable_wal(self, busy_timeout_ms: int) -> None:
        # SQLite can reject a concurrent journal-mode conversion immediately,
        # without invoking its busy handler. Retry only that lock race, sharing
        # one budget with SQLite's own waits rather than adding a full timeout
        # to every attempt.
        deadline = time.monotonic() + busy_timeout_ms / 1000
        try:
            while True:
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                self._conn.execute(f"PRAGMA busy_timeout={remaining_ms}")
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
                    return
                except sqlite3.OperationalError as exc:
                    code = getattr(exc, "sqlite_errorcode", None)
                    if not isinstance(code, int) or (code & 0xFF) not in (
                        sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                    ):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(0.01, remaining))
        finally:
            # Subsequent schema/queue transactions keep the caller's timeout.
            self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")

    def _supported_schema_version(self) -> int:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise QueueStoreError(
                f"资料数据库版本 {version} 高于当前支持版本 {SCHEMA_VERSION}，"
                "请更新 FoilPage 后再打开"
            )
        return version

    def _schema_is_current(self) -> bool:
        # One read snapshot prevents mixing schema observations across another
        # connection's migration. BEGIN (deferred) takes no SQLite writer lock.
        self._conn.execute("BEGIN")
        try:
            if self._supported_schema_version() != SCHEMA_VERSION:
                return False
            tables = {
                row["name"]: row["type"] for row in self._conn.execute(
                    "SELECT name,type FROM sqlite_master WHERE name IN ('jobs','manifests')"
                )
            }
            if tables != {"jobs": "table", "manifests": "table"}:
                return False
            for table, expected in _SCHEMA_COLUMNS.items():
                columns = {
                    row["name"]: row for row in self._conn.execute(f"PRAGMA table_xinfo({table})")
                }
                for name, definition in expected.items():
                    row = columns.get(name)
                    if row is None or row["hidden"] != 0 or (
                        row["type"].upper(), row["notnull"], row["dflt_value"], row["pk"]
                    ) != definition:
                        return False
                # The PK must remain a unique key on the expected single column.
                primary = [row for row in self._conn.execute(f"PRAGMA index_list({table})")
                           if row["origin"] == "pk"]
                primary_key = "id" if table == "jobs" else "job_id"
                if len(primary) != 1 or primary[0]["unique"] != 1 or primary[0]["partial"] != 0:
                    return False
                key_columns = [(row["name"], row["desc"], row["coll"], row["cid"] >= 0)
                               for row in self._conn.execute(
                                   'PRAGMA index_xinfo("' + primary[0]["name"].replace('"', '""') + '")'
                               ) if row["key"]]
                if key_columns != [(primary_key, 0, "BINARY", True)]:
                    return False
            foreign_keys = [
                (row["table"], row["from"], row["to"], row["on_update"], row["on_delete"], row["match"])
                for row in self._conn.execute("PRAGMA foreign_key_list(manifests)")
            ]
            if foreign_keys != [("jobs", "job_id", "id", "NO ACTION", "NO ACTION", "NONE")]:
                return False
            index = next((row for row in self._conn.execute("PRAGMA index_list(jobs)")
                          if row["name"] == "idx_jobs_status_created"), None)
            if index is None or index["unique"] != 0 or index["partial"] != 0 or index["origin"] != "c":
                return False
            index_columns = [(row["name"], row["desc"], row["coll"], row["cid"] >= 0)
                             for row in self._conn.execute("PRAGMA index_xinfo(idx_jobs_status_created)")
                             if row["key"]]
            return index_columns == [("status", 0, "BINARY", True), ("created_at", 0, "BINARY", True)]
        finally:
            self._conn.rollback()

    def _migrate(self) -> None:
        with self._lock, self._conn:
            # Desktop and worker can open the same old database together. Acquire
            # the write transaction before inspecting columns to make ALTER idempotent.
            self._conn.execute("BEGIN IMMEDIATE")
            self._supported_schema_version()
            columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")
            }
            for name, definition in _MIGRATION_COLUMNS.items():
                if name not in columns:
                    self._conn.execute(
                        f"ALTER TABLE jobs ADD COLUMN {name} {definition}"
                    )
            manifest_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(manifests)")
            }
            if "metadata_json" not in manifest_columns:
                self._conn.execute(
                    "ALTER TABLE manifests ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
                )
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        # A connection context starts its implicit transaction only on DML.
        # Acquire SQLite's writer before reading a status/lease/cancel flag so
        # independent desktop/worker connections cannot validate a stale row or
        # upgrade a stale WAL read snapshot. The existing busy_timeout applies
        # here; failures roll back the complete operation without replaying it.
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            yield

    def enqueue(self, job: Job) -> None:
        item = validate.coerce_job(job)
        validate.validate_job(item)
        if item["status"] != "queued":
            raise QueueStateError("enqueue 只接受 queued 作业")
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO jobs
                  (id, source_path, output_dir, status, whisper_model, device,
                   language, content_mode, progress, source_title, result_category, error, raw_path, final_path,
                   created_at, updated_at, worker_id, claimed_at, heartbeat_at,
                   attempts, max_attempts, cancel_requested, body_enabled_override,
                   agent_level_override)
                VALUES (:id, :source_path, :output_dir, :status, :whisper_model,
                        :device, :language, :content_mode, :progress, :source_title, :result_category, :error, :raw_path,
                        :final_path, :created_at, :updated_at, :worker_id,
                        :claimed_at, :heartbeat_at, :attempts, :max_attempts,
                        :cancel_requested, :body_enabled_override, :agent_level_override)
                """,
                self._job_row(item),
            )

    def update_status(
        self,
        job_id: str,
        status: str,
        error: Optional[dict] = None,
        *,
        worker_id: Optional[str] = None,
    ) -> Job:
        if status not in validate._JOB_STATUSES:
            raise validate.ContractValidationError(f"Job.status 非法: {status!r}")
        with self._write_transaction():
            row = self._require_job_row(job_id)
            current = row["status"]
            if status not in _UPDATE_TRANSITIONS[current]:
                raise QueueStateError(f"非法状态转换: {current} -> {status}")
            self._assert_owner(row, worker_id)
            self._conn.execute(
                """
                UPDATE jobs
                   SET status=:status, error=:error, updated_at=:updated_at,
                       worker_id=NULL, claimed_at=NULL, heartbeat_at=NULL,
                       cancel_requested=0
                 WHERE id=:id
                """,
                {
                    "id": job_id,
                    "status": status,
                    "error": _error_json(error),
                    "updated_at": _now(),
                },
            )
            return self._row_to_job(self._require_job_row(job_id))

    def save_manifest(
        self,
        manifest: Manifest,
        *,
        worker_id: Optional[str] = None,
    ) -> None:
        validate.validate_manifest(manifest)
        with self._write_transaction():
            row = self._require_job_row(manifest["job_id"])
            if row["status"] != "running":
                raise QueueStateError(
                    f"manifest 只能完成 running 作业，当前为 {row['status']}"
                )
            self._assert_owner(row, worker_id)
            if row["cancel_requested"]:
                raise CancellationRequestedError("作业已请求取消，拒绝保存 manifest")
            if not _source_paths_equivalent(manifest["source_path"], row["source_path"]):
                raise QueueStateError("Manifest.source_path 与 Job.source_path 不一致")
            completed_tags = row["tags"]
            if manifest.get("agent_status") == "done" and manifest.get("agent_tags"):
                completed_tags = json.dumps(validate.coerce_tags(manifest["agent_tags"]), ensure_ascii=False)
            self._conn.execute(
                """
                INSERT INTO manifests
                  (job_id, source_path, source_sha256, backend, model, language,
                   duration_sec, raw_path, final_path, content_mode,
                   prompt_version, created_at, metadata_json)
                VALUES (:job_id, :source_path, :source_sha256, :backend, :model,
                        :language, :duration_sec, :raw_path, :final_path,
                        :content_mode, :prompt_version, :created_at, :metadata_json)
                ON CONFLICT(job_id) DO UPDATE SET
                    source_path=excluded.source_path,
                    source_sha256=excluded.source_sha256,
                    backend=excluded.backend,
                    model=excluded.model,
                    language=excluded.language,
                    duration_sec=excluded.duration_sec,
                    raw_path=excluded.raw_path,
                    final_path=excluded.final_path,
                    content_mode=excluded.content_mode,
                    prompt_version=excluded.prompt_version,
                    created_at=excluded.created_at,
                    metadata_json=excluded.metadata_json
                """,
                self._manifest_row(manifest),
            )
            cursor = self._conn.execute(
                """
                UPDATE jobs
                   SET status='done', progress=1.0, raw_path=:raw_path, final_path=:final_path,
                       content_mode=:content_mode, error=NULL, updated_at=:updated_at, worker_id=NULL,
                       claimed_at=NULL, heartbeat_at=NULL, cancel_requested=0, tags=:tags
                 WHERE id=:job_id AND status='running'
                """,
                {
                    "job_id": manifest["job_id"],
                    "raw_path": manifest["raw_path"],
                    "final_path": manifest["final_path"],
                    "content_mode": manifest["content_mode"],
                    "updated_at": _now(),
                    "tags": completed_tags,
                },
            )
            if cursor.rowcount != 1:
                raise QueueStateError("完成作业失败：running 行未更新")
            self._write_stage_metadata(manifest["job_id"], manifest)

    def replace_manifest(self, manifest: Manifest) -> None:
        """Replace the presentation result of a completed job after Agent retranslation."""
        validate.validate_manifest(manifest)
        with self._lock, self._conn:
            # Serialize the cleared-marker check with writers on other connections.
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._require_job_row(manifest["job_id"])
            if row["status"] != "done":
                raise QueueStateError("只有已完成任务可重转译")
            if not _source_paths_equivalent(manifest["source_path"], row["source_path"]):
                raise QueueStateError("Manifest.source_path 与 Job.source_path 不一致")
            current = self._conn.execute(
                "SELECT * FROM manifests WHERE job_id=?", (manifest["job_id"],)
            ).fetchone()
            if current and self._row_to_manifest(current).get("text_outputs_cleared"):
                raise QueueStateError("文本已删除，请重新转写后再生成产物")
            self._conn.execute(
                """
                UPDATE manifests SET source_sha256=:source_sha256, backend=:backend,
                   model=:model, language=:language, duration_sec=:duration_sec,
                   raw_path=:raw_path, final_path=:final_path, content_mode=:content_mode,
                   prompt_version=:prompt_version, created_at=:created_at,
                   metadata_json=:metadata_json
                 WHERE job_id=:job_id
                """, self._manifest_row(manifest)
            )
            self._conn.execute(
                """UPDATE jobs SET raw_path=?, final_path=?, content_mode=?,
                   updated_at=? WHERE id=? AND status='done'""",
                (manifest["raw_path"], manifest["final_path"], manifest["content_mode"],
                 _now(), manifest["job_id"]),
            )
            self._write_stage_metadata(manifest["job_id"], manifest)

    def _write_stage_metadata(self, job_id: str, changes: dict) -> None:
        row = self._require_job_row(job_id)
        try:
            current = json.loads(row["stage_metadata_json"])
        except (ValueError, TypeError):
            current = {}
        current = _stage_metadata(current if isinstance(current, dict) else {})
        current.update(_stage_metadata(changes, strict=True))
        self._conn.execute("UPDATE jobs SET stage_metadata_json=?, updated_at=? WHERE id=?",
                           (json.dumps(current, ensure_ascii=False), _now(), job_id))

    def set_stage_metadata(self, job_id: str, changes: dict, *, worker_id: Optional[str] = None) -> None:
        """Persist execution snapshots even when ASR/AI is cancelled before a manifest exists."""
        with self._write_transaction():
            row = self._require_job_row(job_id)
            self._assert_owner(row, worker_id)
            self._write_stage_metadata(job_id, changes)

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            return self._row_to_job(row) if row else None

    def set_source_title(self, job_id: str, title: str) -> None:
        """Persist a human title without changing the media file path."""
        clean = " ".join(str(title or "").replace("\x00", "").split()).strip()[:240]
        if not clean:
            return
        with self._write_transaction():
            self._require_job_row(job_id)
            self._conn.execute(
                "UPDATE jobs SET source_title=?, updated_at=? WHERE id=?",
                (clean, _now(), job_id),
            )

    def set_result_category(self, job_id: str, category: str) -> Job:
        clean = " ".join(str(category or "").replace("\x00", "").split()).strip()[:40]
        if not clean:
            raise ValueError("文本分类不能为空")
        with self._write_transaction():
            row = self._require_job_row(job_id)
            if row["status"] != "done":
                raise QueueStateError("仅已完成任务可以设置文本分类")
            self._conn.execute(
                "UPDATE jobs SET result_category=?, updated_at=? WHERE id=?",
                (clean, _now(), job_id),
            )
            return self._row_to_job(self._require_job_row(job_id))

    def set_tags(self, job_id: str, tags: list[str]) -> list[str]:
        """Persist the tag list for a completed text asset (deduped, validated)."""
        cleaned = validate.coerce_tags(tags)
        with self._write_transaction():
            row = self._require_job_row(job_id)
            if row["status"] != "done":
                raise QueueStateError("仅已完成任务可以设置标签")
            self._conn.execute(
                "UPDATE jobs SET tags=?, updated_at=? WHERE id=?",
                (json.dumps(cleaned, ensure_ascii=False), _now(), job_id),
            )
            return cleaned

    def update_document_metadata(
        self, job_id: str, *, expected_revision: str,
        title: str | None = None, tags: list[str] | None = None,
        category: str | None = None,
    ) -> dict:
        """Compare and update completed-document metadata in one DB transaction.

        The writer is acquired before reading, so a desktop or a finishing
        Worker on another connection cannot change the fields between the
        revision check and this update.
        """
        from engine.agent_protocol import DocumentRevisionConflict, document_metadata
        if not isinstance(expected_revision, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_revision):
            raise ValueError("文稿版本无效")
        if title is tags is category is None:
            raise ValueError("至少提供一个文稿字段")
        clean_title = None
        clean_category = None
        clean_tags = None
        if title is not None:
            if not isinstance(title, str):
                raise ValueError("标题格式无效")
            clean_title = " ".join(title.replace("\x00", "").split()).strip()
            if not clean_title or len(clean_title) > 240:
                raise ValueError("标题应为 1 到 240 字")
        if category is not None:
            if not isinstance(category, str):
                raise ValueError("分类格式无效")
            clean_category = " ".join(category.replace("\x00", "").split()).strip()
            if not clean_category or len(clean_category) > 40:
                raise ValueError("分类应为 1 到 40 字")
        if tags is not None:
            clean_tags = validate.coerce_tags(tags)
        with self._write_transaction():
            row = self._require_job_row(job_id)
            if row["status"] != "done":
                raise QueueStateError("仅已完成任务可以编辑文稿")
            before = self._row_to_job(row)
            current = document_metadata(before)
            if current["revision"] != expected_revision:
                raise DocumentRevisionConflict(current["revision"])
            self._conn.execute(
                "UPDATE jobs SET source_title=?, tags=?, result_category=?, updated_at=? WHERE id=?",
                (
                    before.get("source_title", "") if clean_title is None else clean_title,
                    json.dumps(before.get("tags") or [], ensure_ascii=False) if clean_tags is None
                    else json.dumps(clean_tags, ensure_ascii=False),
                    (before.get("result_category") or "未分类") if clean_category is None else clean_category,
                    _now(), job_id,
                ),
            )
            return document_metadata(self._row_to_job(self._require_job_row(job_id)))

    def set_archived(self, job_id: str, archived: bool) -> Job:
        """Mark a job as archived (hidden from workbench, kept in library)."""
        with self._write_transaction():
            row = self._require_job_row(job_id)
            if row["status"] != "done":
                raise QueueStateError("仅已完成任务可以归档")
            self._conn.execute(
                "UPDATE jobs SET archived=?, updated_at=? WHERE id=?",
                (1 if archived else 0, _now(), job_id),
            )
            return self._row_to_job(self._require_job_row(job_id))

    def clear_text_outputs(
        self, job_id: str, *,
        remove_files: Optional[Callable[[Job, Optional[Manifest]], Optional[ContextManager]]] = None,
    ) -> bool:
        """Clear text references while retaining the job, manifest and media links.

        The optional, scoped filesystem cleanup runs under the same SQLite
        writer transaction as the terminal-state check. Active workers cannot
        claim/requeue this job between validation and clearing its artifacts.
        A filesystem context can stage/restore its files on preparation or SQL
        failure. Commit happens before that context finalizes physical removal.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._require_job_row(job_id)
                if row["status"] in ("queued", "running", "retryable"):
                    raise QueueStateError("运行中的任务请先取消后再删除文本")
                manifest_row = self._conn.execute(
                    "SELECT * FROM manifests WHERE job_id=?", (job_id,)
                ).fetchone()
                manifest = self._row_to_manifest(manifest_row) if manifest_row else None
                if not (row["raw_path"] or row["final_path"] or
                        manifest and any(manifest.get(key) for key in
                                         ("raw_path", "final_path", "segments_path"))):
                    self._conn.commit()
                    return False
                cleanup = remove_files(self._row_to_job(row), manifest) if remove_files else None
                with cleanup if cleanup is not None else nullcontext():
                    # Commit inside the filesystem context: a failed COMMIT also
                    # restores staged text/sidecars before the outer rollback.
                    with self._conn:
                        self._conn.execute(
                            "UPDATE jobs SET raw_path=NULL, final_path=NULL, updated_at=? WHERE id=?",
                            (_now(), job_id),
                        )
                        if manifest is not None:
                            metadata = _manifest_metadata(manifest)
                            metadata.update(segments_path="", text_outputs_cleared=True)
                            self._conn.execute(
                                """UPDATE manifests SET raw_path='', final_path='', metadata_json=?
                                   WHERE job_id=?""",
                                (json.dumps(metadata, ensure_ascii=False), job_id),
                            )
                return True
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        """Delete a job record; source and result files remain intact.

        Without ``force`` only terminal rows can be deleted. With ``force`` any
        row is removed immediately — the running worker/download coordinator
        observes the missing row as a cancellation and stops at its next check.
        """
        with self._write_transaction():
            row = self._require_job_row(job_id)
            if not force and row["status"] in ("queued", "running", "retryable"):
                raise QueueStateError("运行中的任务请先取消后再删除")
            self._conn.execute("DELETE FROM manifests WHERE job_id=?", (job_id,))
            return self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,)).rowcount == 1

    def get_manifest(self, job_id: str) -> Optional[Manifest]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM manifests WHERE job_id=?", (job_id,)
            ).fetchone()
            return self._row_to_manifest(row) if row else None

    def list_jobs(self, status: Optional[str] = None) -> list[Job]:
        if status is not None and status not in validate._JOB_STATUSES:
            raise validate.ContractValidationError(f"Job.status 非法: {status!r}")
        with self._lock:
            if status is None:
                rows = self._conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at, id"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE status=? ORDER BY created_at, id",
                    (status,),
                ).fetchall()
            return [self._row_to_job(row) for row in rows]

    def claim_next(self, worker_id: str = "worker-0") -> Optional[Job]:
        if not worker_id.strip():
            raise ValueError("worker_id 不得为空")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    """
                    SELECT id FROM jobs
                     WHERE status='queued'
                       AND cancel_requested=0
                       AND attempts < max_attempts
                     ORDER BY created_at, id
                     LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                now = _now()
                cursor = self._conn.execute(
                    """
                    UPDATE jobs
                       SET status='running', worker_id=:worker_id,
                           claimed_at=:now, heartbeat_at=:now,
                           attempts=attempts + 1, updated_at=:now, error=NULL,
                           progress=0
                     WHERE id=:id AND status='queued'
                    """,
                    {"worker_id": worker_id, "now": now, "id": row["id"]},
                )
                if cursor.rowcount != 1:
                    raise QueueStateError("原子领取失败")
                claimed = self._require_job_row(row["id"])
                self._conn.execute("COMMIT")
                return self._row_to_job(claimed)
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def heartbeat(self, job_id: str, worker_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE jobs SET heartbeat_at=:now, updated_at=:now
                 WHERE id=:id AND status='running' AND worker_id=:worker_id
                """,
                {"now": _now(), "id": job_id, "worker_id": worker_id},
            )
            return cursor.rowcount == 1

    def update_progress(self, job_id: str, progress: float, worker_id: str) -> Job:
        """Persist determinate progress for the worker that owns a running job."""
        value = float(progress)
        if not 0.0 <= value <= 1.0:
            raise validate.ContractValidationError("Job.progress 必须在 [0,1]")
        with self._write_transaction():
            row = self._require_job_row(job_id)
            if row["status"] != "running":
                raise QueueStateError("只能更新 running 作业的进度")
            self._assert_owner(row, worker_id)
            self._conn.execute(
                "UPDATE jobs SET progress=?, updated_at=? WHERE id=?",
                (value, _now(), job_id),
            )
            return self._row_to_job(self._require_job_row(job_id))

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                # 行已被强制删除：视为已取消，让 worker/下载协调器尽快停止。
                return True
            return bool(row["cancel_requested"])

    def request_cancel(self, job_id: str) -> Job:
        with self._write_transaction():
            row = self._require_job_row(job_id)
            now = _now()
            if row["status"] in ("queued", "retryable"):
                self._conn.execute(
                    """
                    UPDATE jobs
                       SET status='cancelled', cancel_requested=0,
                           updated_at=?, error=NULL
                     WHERE id=?
                    """,
                    (now, job_id),
                )
            elif row["status"] == "running":
                self._conn.execute(
                    "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?",
                    (now, job_id),
                )
            return self._row_to_job(self._require_job_row(job_id))

    def requeue(self, job_id: str) -> Job:
        with self._write_transaction():
            row = self._require_job_row(job_id)
            if row["status"] not in ("retryable", "failed", "cancelled"):
                raise QueueStateError(
                    f"只有 retryable/failed/cancelled 可重排队，当前为 {row['status']}"
                )
            if row["attempts"] >= row["max_attempts"]:
                raise QueueStateError("已达到最大尝试次数，不能重排队")
            self._conn.execute(
                """
                UPDATE jobs
                   SET status='queued', progress=0, error=NULL, updated_at=?, worker_id=NULL,
                       claimed_at=NULL, heartbeat_at=NULL, cancel_requested=0
                 WHERE id=?
                """,
                (_now(), job_id),
            )
            return self._row_to_job(self._require_job_row(job_id))

    def recover_stale_jobs(self, lease_timeout_sec: float = 300.0) -> list[str]:
        if lease_timeout_sec < 0:
            raise ValueError("lease_timeout_sec 必须 >= 0")
        cutoff = (datetime.now() - timedelta(seconds=lease_timeout_sec)).isoformat(
            timespec="seconds"
        )
        recovered: list[str] = []
        with self._write_transaction():
            rows = self._conn.execute(
                """
                SELECT * FROM jobs
                 WHERE status='running'
                   AND COALESCE(heartbeat_at, claimed_at, updated_at) <= ?
                 ORDER BY created_at, id
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                if row["cancel_requested"]:
                    status = "cancelled"
                    error = None
                elif row["attempts"] < row["max_attempts"]:
                    status = "queued"
                    error = {
                        "code": "worker_lease_expired",
                        "message": "Worker 心跳超时，作业已自动重新排队",
                    }
                else:
                    status = "failed"
                    error = {
                        "code": "worker_lease_expired",
                        "message": "Worker 心跳超时且已达到最大尝试次数",
                    }
                self._conn.execute(
                    """
                    UPDATE jobs
                       SET status=:status,
                           progress=CASE WHEN :status='queued' THEN 0 ELSE progress END,
                           error=:error, updated_at=:now,
                           worker_id=NULL, claimed_at=NULL, heartbeat_at=NULL,
                           cancel_requested=0
                     WHERE id=:id AND status='running'
                    """,
                    {
                        "status": status,
                        "error": _error_json(error),
                        "now": _now(),
                        "id": row["id"],
                    },
                )
                recovered.append(row["id"])
        return recovered

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def _require_job_row(self, job_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(f"作业不存在: {job_id}")
        return row

    @staticmethod
    def _assert_owner(row: sqlite3.Row, worker_id: Optional[str]) -> None:
        if worker_id is not None and row["worker_id"] != worker_id:
            raise QueueStateError(
                f"Worker 租约不匹配: expected={row['worker_id']!r}, got={worker_id!r}"
            )

    @staticmethod
    def _job_row(job: Job) -> dict:
        return {
            "id": job["id"],
            "source_path": job["source_path"],
            "output_dir": job["output_dir"],
            "status": job["status"],
            "whisper_model": job["whisper_model"],
            "device": job["device"],
            "language": job["language"],
            "content_mode": job["content_mode"],
            "progress": float(job.get("progress", 0.0)),
            "source_title": job.get("source_title", "") or None,
            "result_category": job.get("result_category", "未分类") or "未分类",
            "error": _error_json(job.get("error")),
            "raw_path": job.get("raw_path"),
            "final_path": job.get("final_path"),
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "worker_id": job.get("worker_id"),
            "claimed_at": job.get("claimed_at"),
            "heartbeat_at": job.get("heartbeat_at"),
            "attempts": job.get("attempts", 0),
            "max_attempts": job.get("max_attempts", 3),
            "cancel_requested": int(job.get("cancel_requested", False)),
            "body_enabled_override": (
                None if job.get("body_enabled_override") is None
                else int(bool(job.get("body_enabled_override")))
            ),
            "agent_level_override": str(job.get("agent_level_override") or "") or None,
            "archived": int(job.get("archived", False)),
            "tags": json.dumps(job.get("tags") or [], ensure_ascii=False),
        }

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        job = {
            "id": row["id"],
            "source_path": row["source_path"],
            "output_dir": row["output_dir"],
            "status": row["status"],
            "whisper_model": row["whisper_model"],
            "device": row["device"],
            "language": row["language"],
            "content_mode": row["content_mode"],
            "progress": float(row["progress"]),
            "source_title": row["source_title"] or "",
            "result_category": row["result_category"] or "未分类",
            "error": json.loads(row["error"]) if row["error"] else None,
            "raw_path": row["raw_path"],
            "final_path": row["final_path"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "worker_id": row["worker_id"],
            "claimed_at": row["claimed_at"],
            "heartbeat_at": row["heartbeat_at"],
            "attempts": row["attempts"],
            "max_attempts": row["max_attempts"],
            "cancel_requested": bool(row["cancel_requested"]),
            "body_enabled_override": (
                bool(row["body_enabled_override"]) if row["body_enabled_override"] is not None else None
            ),
            "agent_level_override": row["agent_level_override"],
            "archived": bool(row["archived"]),
            "tags": json.loads(row["tags"]) if row["tags"] else [],
        }
        try:
            metadata = json.loads(row["stage_metadata_json"])
        except (ValueError, TypeError):
            metadata = {}
        job.update(_stage_metadata(metadata if isinstance(metadata, dict) else {}))
        return job

    @staticmethod
    def _manifest_row(manifest: Manifest) -> dict:
        return {
            **manifest,
            "metadata_json": json.dumps(
                _manifest_metadata(manifest, strict=True), ensure_ascii=False,
            ),
        }

    @staticmethod
    def _row_to_manifest(row: sqlite3.Row) -> Manifest:
        manifest = {
            "job_id": row["job_id"],
            "source_path": row["source_path"],
            "source_sha256": row["source_sha256"],
            "backend": row["backend"],
            "model": row["model"],
            "language": row["language"],
            "duration_sec": row["duration_sec"],
            "raw_path": row["raw_path"],
            "final_path": row["final_path"],
            "content_mode": row["content_mode"],
            "prompt_version": row["prompt_version"],
            "created_at": row["created_at"],
        }
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (IndexError, TypeError, json.JSONDecodeError):
            metadata = {}
        if isinstance(metadata, dict):
            manifest.update(_manifest_metadata(metadata))
        return manifest
