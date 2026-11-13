# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Persistent browser-download queue stored beside the media and ASR tables."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Iterable

from .contracts import DownloadJob, DownloadOptions

DOWNLOAD_STATUSES = frozenset({
    "received", "resolving", "awaiting_browser", "downloading", "imported", "queued",
    "transcribing", "done", "failed", "cancelled",
})

_TRANSITIONS = {
    "received": frozenset({"resolving", "cancelled"}),
    "resolving": frozenset({"received", "downloading", "awaiting_browser", "failed", "cancelled"}),
    "awaiting_browser": frozenset({"received", "resolving", "imported", "failed", "cancelled"}),
    "downloading": frozenset({"received", "awaiting_browser", "imported", "failed", "cancelled"}),
    "imported": frozenset({"queued", "done", "failed", "cancelled"}),
    "queued": frozenset({"transcribing", "done", "failed", "cancelled"}),
    "transcribing": frozenset({"queued", "done", "failed", "cancelled"}),
    # A completed download can be deliberately promoted to transcription from
    # the desktop task view without fetching the media a second time.
    "done": frozenset({"queued"}),
    # A terminally failed browser task can still be completed out-of-band: the
    # original Chrome transfer may auto-resume and finish after every fallback
    # channel was exhausted. Its import resurrects the same task row.
    "failed": frozenset({"received", "cancelled", "imported"}),
    "cancelled": frozenset({"received"}),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_schema (
    component TEXT PRIMARY KEY,
    version   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS download_jobs (
    id               TEXT PRIMARY KEY,
    url              TEXT NOT NULL,
    title            TEXT NOT NULL,
    source           TEXT NOT NULL,
    platform_hint    TEXT NOT NULL,
    requested_at     TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    options          TEXT NOT NULL,
    status           TEXT NOT NULL,
    progress         REAL NOT NULL DEFAULT 0,
    bytes_total      INTEGER,
    bytes_done       INTEGER NOT NULL DEFAULT 0,
    downloaded_path  TEXT,
    media_id         TEXT,
    job_id           TEXT,
    error            TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    cover_path       TEXT
);
CREATE INDEX IF NOT EXISTS idx_download_jobs_status_requested
    ON download_jobs(status, requested_at);
CREATE TABLE IF NOT EXISTS download_requests (
    client_request_id TEXT PRIMARY KEY,
    download_id TEXT NOT NULL REFERENCES download_jobs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS cancelled_download_requests (
    client_request_id TEXT PRIMARY KEY
);
"""

# 与 media_store 同款的组件版本登记：加列时在 _migrate() 里按版本逐级执行，
# 保持桌面与 Worker 双进程同时开旧库安全。版本 2 = cover_path（TASK-8.17）。
_COMPONENT_VERSION = 2


class DownloadStoreError(RuntimeError):
    pass


class DownloadNotFoundError(DownloadStoreError):
    pass


class DownloadStateError(DownloadStoreError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DownloadStore:
    def __init__(self, db_path: str = ":memory:", *, busy_timeout_ms: int = 5000) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, timeout=max(busy_timeout_ms, 0) / 1000,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """幂等迁移：登记/推进 download 组件版本。加列时在此按版本逐级
        ALTER（参照 media_store），保持桌面与 Worker 双进程同时开旧库安全。"""
        with self._conn:
            row = self._conn.execute(
                "SELECT version FROM app_schema WHERE component='downloads'"
            ).fetchone()
            current = row["version"] if row else 0
            if current > _COMPONENT_VERSION:
                raise DownloadStoreError(
                    f"下载库版本 {current} 高于当前程序支持版本 {_COMPONENT_VERSION}，请先升级应用"
                )
            if current < 1:
                self._conn.execute(
                    "INSERT INTO app_schema(component, version) VALUES ('downloads', ?) "
                    "ON CONFLICT(component) DO UPDATE SET version=excluded.version",
                    (max(current, 1),),
                )
                current = 1
            if current < 2:
                # 新库（executescript 建表）已含 cover_path，旧库补列。
                columns = {
                    row["name"]
                    for row in self._conn.execute("PRAGMA table_info(download_jobs)").fetchall()
                }
                if "cover_path" not in columns:
                    self._conn.execute("ALTER TABLE download_jobs ADD COLUMN cover_path TEXT")
                self._conn.execute(
                    "UPDATE app_schema SET version=2 WHERE component='downloads'"
                )

    def create(
        self,
        url: str,
        title: str,
        platform_hint: str,
        options: DownloadOptions,
    ) -> DownloadJob:
        now = _now()
        item: DownloadJob = {
            "id": uuid.uuid4().hex[:12],
            "url": url,
            "title": title.strip()[:240],
            "source": "browser",
            "platform_hint": platform_hint,
            "requested_at": now,
            "updated_at": now,
            "options": options,
            "status": "received",
            "progress": 0.0,
            "bytes_total": None,
            "bytes_done": 0,
            "downloaded_path": None,
            "media_id": None,
            "job_id": None,
            "error": None,
            "cancel_requested": False,
            "attempts": 0,
            "cover_path": None,
        }
        self._validate(item)
        with self._lock, self._conn:
            request_id = options.get("client_request_id", "")
            existing = self.find_by_client_request_id(request_id) if request_id else None
            if existing is not None:
                return existing
            self._conn.execute(
                """
                INSERT INTO download_jobs
                  (id,url,title,source,platform_hint,requested_at,updated_at,options,
                   status,progress,bytes_total,bytes_done,downloaded_path,media_id,
                   job_id,error,cancel_requested,attempts,cover_path)
                VALUES
                  (:id,:url,:title,:source,:platform_hint,:requested_at,:updated_at,
                   :options,:status,:progress,:bytes_total,:bytes_done,:downloaded_path,
                   :media_id,:job_id,:error,:cancel_requested,:attempts,:cover_path)
                """,
                self._to_row(item),
            )
            if request_id:
                self._conn.execute("INSERT INTO download_requests VALUES (?,?)", (request_id, item["id"]))
        return item

    def get(self, download_id: str) -> DownloadJob | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM download_jobs WHERE id=?", (download_id,)
            ).fetchone()
            return self._from_row(row) if row else None

    def client_request_cancelled(self, request_id: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM cancelled_download_requests WHERE client_request_id=?", (request_id,)).fetchone() is not None

    def cancel_client_request(self, request_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR IGNORE INTO cancelled_download_requests VALUES (?)", (request_id,))

    def find_by_client_request_id(self, request_id: str) -> DownloadJob | None:
        """Resolve a durable submission receipt, including terminal tasks."""
        if not request_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT j.* FROM download_jobs j JOIN download_requests r ON r.download_id=j.id WHERE r.client_request_id=?",
                (request_id,),
            ).fetchone()
            return self._from_row(row) if row else None

    def remember_client_request(self, request_id: str, download_id: str) -> None:
        if not request_id:
            return
        with self._lock, self._conn:
            self._require(download_id)
            self._conn.execute("INSERT OR IGNORE INTO download_requests VALUES (?,?)", (request_id, download_id))

    def list(self, statuses: Iterable[str] | None = None, *, limit: int = 100) -> list[DownloadJob]:
        limit = max(1, min(int(limit), 500))
        selected = tuple(dict.fromkeys(statuses or ()))
        if any(status not in DOWNLOAD_STATUSES for status in selected):
            raise DownloadStateError("下载状态非法")
        with self._lock:
            if selected:
                marks = ",".join("?" for _ in selected)
                rows = self._conn.execute(
                    f"SELECT * FROM download_jobs WHERE status IN ({marks}) ORDER BY requested_at DESC, id DESC LIMIT ?",
                    (*selected, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM download_jobs ORDER BY requested_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._from_row(row) for row in rows]

    def claim_next(self) -> DownloadJob | None:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT id FROM download_jobs WHERE status='received' AND cancel_requested=0 ORDER BY requested_at,id LIMIT 1"
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                self._conn.execute(
                    "UPDATE download_jobs SET status='resolving', attempts=attempts+1, updated_at=? WHERE id=? AND status='received'",
                    (_now(), row["id"]),
                )
                claimed = self._require(row["id"])
                self._conn.execute("COMMIT")
                return self._from_row(claimed)
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def transition(self, download_id: str, status: str, **changes: Any) -> DownloadJob:
        if status not in DOWNLOAD_STATUSES:
            raise DownloadStateError(f"下载状态非法: {status}")
        allowed_fields = {
            "progress", "bytes_total", "bytes_done", "downloaded_path",
            "media_id", "job_id", "error", "cancel_requested", "options",
            "cover_path",
        }
        unknown = set(changes) - allowed_fields
        if unknown:
            raise DownloadStoreError(f"下载更新字段非法: {sorted(unknown)}")
        with self._lock, self._conn:
            current = self._from_row(self._require(download_id))
            if status != current["status"] and status not in _TRANSITIONS[current["status"]]:
                raise DownloadStateError(f"非法下载状态转换: {current['status']} -> {status}")
            current.update(changes)
            current["status"] = status
            current["updated_at"] = _now()
            self._validate(current)
            row = self._to_row(current)
            self._conn.execute(
                """
                UPDATE download_jobs SET status=:status,updated_at=:updated_at,options=:options,
                  progress=:progress,bytes_total=:bytes_total,bytes_done=:bytes_done,
                  downloaded_path=:downloaded_path,media_id=:media_id,job_id=:job_id,
                  error=:error,cancel_requested=:cancel_requested,cover_path=:cover_path
                  WHERE id=:id
                """,
                row,
            )
            return current

    def request_cancel(self, download_id: str) -> DownloadJob:
        current = self.get(download_id)
        if current is None:
            raise DownloadNotFoundError(download_id)
        if current["status"] in ("done", "failed", "cancelled"):
            return current
        # Browser handoff has no worker loop left to observe cancel_requested;
        # transition it immediately so an interrupted Chrome download cannot
        # leave the task permanently stuck in awaiting_browser.
        if current["status"] in ("received", "imported", "queued", "awaiting_browser"):
            return self.transition(download_id, "cancelled", cancel_requested=False)
        return self.transition(download_id, current["status"], cancel_requested=True)

    def retry(self, download_id: str) -> DownloadJob:
        current = self.get(download_id)
        if current is None:
            raise DownloadNotFoundError(download_id)
        error = current.get("error") or {}
        if current["status"] != "failed" or not error.get("retryable"):
            raise DownloadStateError("只有可重试的失败下载才能重试")
        return self.transition(
            download_id, "received", progress=0.0, bytes_total=None,
            bytes_done=0, downloaded_path=None, media_id=None, job_id=None,
            error=None, cancel_requested=False, cover_path=None,
        )

    def delete(self, download_id: str, *, force: bool = False) -> bool:
        """Remove a browser task record; never deletes a downloaded file.

        Without ``force`` only terminal rows can be deleted. With ``force`` any
        row is removed immediately; the download coordinator sees the missing
        row as a cancellation and stops the active download at its next check.
        """
        with self._lock, self._conn:
            current = self._from_row(self._require(download_id))
            if not force and current["status"] not in ("done", "failed", "cancelled"):
                raise DownloadStateError("运行中的下载请先取消后再删除")
            return self._conn.execute(
                "DELETE FROM download_jobs WHERE id=?", (download_id,)
            ).rowcount == 1

    def is_cancel_requested(self, download_id: str) -> bool:
        current = self.get(download_id)
        if current is None:
            # 行已被强制删除：视为已取消，让下载协调器尽快停止。
            return True
        return bool(current.get("cancel_requested"))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _require(self, download_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM download_jobs WHERE id=?", (download_id,)
        ).fetchone()
        if row is None:
            raise DownloadNotFoundError(download_id)
        return row

    @staticmethod
    def _validate(item: DownloadJob) -> None:
        if item["status"] not in DOWNLOAD_STATUSES:
            raise DownloadStateError("下载状态非法")
        if not 0.0 <= float(item["progress"]) <= 1.0:
            raise DownloadStoreError("progress 必须在 0 到 1 之间")
        if not item["url"].strip() or item["source"] != "browser":
            raise DownloadStoreError("下载任务字段非法")

    @staticmethod
    def _to_row(item: DownloadJob) -> dict[str, Any]:
        row = dict(item)
        row["options"] = json.dumps(item["options"], ensure_ascii=False)
        row["error"] = json.dumps(item.get("error"), ensure_ascii=False) if item.get("error") else None
        row["cancel_requested"] = int(bool(item.get("cancel_requested")))
        return row

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DownloadJob:
        return {
            "id": row["id"], "url": row["url"], "title": row["title"],
            "source": row["source"], "platform_hint": row["platform_hint"],
            "requested_at": row["requested_at"], "updated_at": row["updated_at"],
            "options": json.loads(row["options"]), "status": row["status"],
            "progress": float(row["progress"]), "bytes_total": row["bytes_total"],
            "bytes_done": int(row["bytes_done"]), "downloaded_path": row["downloaded_path"],
            "media_id": row["media_id"], "job_id": row["job_id"],
            "error": json.loads(row["error"]) if row["error"] else None,
            "cancel_requested": bool(row["cancel_requested"]),
            "attempts": int(row["attempts"]),
            "cover_path": row["cover_path"],
        }
