# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Durable receipts for desktop translation and article-processing runs.

Only public progress and input fingerprints are stored, never model credentials
or document contents. A new user attempt receives a new operation identity;
calls within that attempt retain it. The desktop owns execution, so an unfinished
receipt on its next startup is interrupted and must be explicitly retried.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone


class PostprocessStore:
    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5)
        self._conn.row_factory = sqlite3.Row
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """A restored older archive can legitimately lack desktop receipts."""
        with self._lock:
            if not self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='postprocess_runs'"
            ).fetchone():
                with self._conn:
                    self._conn.execute("""CREATE TABLE IF NOT EXISTS postprocess_runs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
                    self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_postprocess_subject
                    ON postprocess_runs(kind,subject,sequence)""")

    def start(self, kind: str, subject: str, state: dict) -> dict:
        value = {**state, "run_id": secrets.token_hex(12), "running": True, "status": "running"}
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO postprocess_runs(run_id,kind,subject,status,state_json,updated_at) VALUES(?,?,?,?,?,?)",
                (value["run_id"], kind, subject, "running", json.dumps(value, ensure_ascii=False), self._now()),
            )
        return value

    @staticmethod
    def validate_database(connection: sqlite3.Connection) -> None:
        """Validate optional imported receipts before replacing the live set."""
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='postprocess_runs'"
        ).fetchone():
            return
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(postprocess_runs)")}
            if not {"sequence", "run_id", "kind", "subject", "status", "state_json", "updated_at"} <= columns:
                raise ValueError("invalid receipt columns")
            for run_id, kind, subject, status, payload in connection.execute(
                "SELECT run_id,kind,subject,status,state_json FROM postprocess_runs"
            ):
                state = json.loads(payload)
                if (not isinstance(state, dict) or kind not in {"translation", "batch"}
                        or not isinstance(run_id, str) or len(run_id) != 24
                        or any(char not in "0123456789abcdef" for char in run_id)
                        or not isinstance(subject, str) or status not in {"running", "done", "failed"}
                        or state.get("run_id") != run_id or state.get("status") != status
                        or type(state.get("running")) is not bool
                        or state["running"] != (status == "running")):
                    raise ValueError("invalid receipt state")
                for key in ("task_id", "direction", "source_revision", "source_tab", "prompt_version",
                            "error", "message", "code", "lang"):
                    if key in state and not isinstance(state[key], str):
                        raise ValueError("invalid receipt text")
                for key in ("task_ids", "processed_ids", "failed_ids", "errors"):
                    if key in state and (not isinstance(state[key], list)
                                         or any(not isinstance(value, str) for value in state[key])):
                        raise ValueError("invalid receipt list")
                for key in ("done", "failed", "skipped"):
                    if key in state and (type(state[key]) is not int or state[key] < 0):
                        raise ValueError("invalid receipt count")
        except (ValueError, TypeError, sqlite3.DatabaseError) as exc:
            raise ValueError("备份中的后台处理记录损坏，导入已中止") from exc

    def freeze(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA query_only=ON")

    def update(self, run_id: str, **changes) -> dict:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM postprocess_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("后台处理记录不存在")
            value = {**json.loads(row["state_json"]), **changes, "run_id": run_id}
            self._conn.execute(
                "UPDATE postprocess_runs SET status=?,state_json=?,updated_at=? WHERE run_id=?",
                (value["status"], json.dumps(value, ensure_ascii=False), self._now(), run_id),
            )
            return value

    def latest(self, kind: str, subject: str | None = None) -> dict | None:
        with self._lock:
            query = "SELECT state_json FROM postprocess_runs WHERE kind=?"
            args = [kind]
            if subject is not None:
                query += " AND subject=?"
                args.append(subject)
            row = self._conn.execute(query + " ORDER BY sequence DESC LIMIT 1", args).fetchone()
            return json.loads(row["state_json"]) if row else None

    def unfinished(self, kind: str) -> list[dict]:
        with self._lock:
            return [{"subject": row["subject"], **json.loads(row["state_json"])} for row in self._conn.execute(
                "SELECT subject,state_json FROM postprocess_runs WHERE kind=? AND status='running'", (kind,)
            )]

    def interrupt_running(self) -> None:
        """Called at desktop startup/shutdown; never silently reissue a paid call."""
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT * FROM postprocess_runs WHERE status='running'").fetchall()
            for row in rows:
                state = json.loads(row["state_json"])
                state.update(running=False, status="failed", code="interrupted", retryable=True)
                message = "上次处理已中断，可重新执行；已完成的资料保留"
                if row["kind"] == "translation":
                    state["error"] = message
                else:
                    pending = [value for value in state.get("task_ids", [])
                               if value not in state.get("processed_ids", [])]
                    state.update(message=message, errors=[message],
                                 failed_ids=state.get("failed_ids", []) + pending,
                                 failed=state.get("failed", 0) + len(pending))
                self._conn.execute(
                    "UPDATE postprocess_runs SET status='failed',state_json=?,updated_at=? WHERE run_id=?",
                    (json.dumps(state, ensure_ascii=False), self._now(), row["run_id"]),
                )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
