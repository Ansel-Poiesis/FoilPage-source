# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Expiration of superseded/empty failed task records, never user files/assets.

Two independent rules:
- Opt-in ``failed_task_retention_days`` deletes empty failed records older
  than the retention period (0 disables; protected rows always survive).
- Always-on ``superseded_failure_retention_hours`` (default 24) deletes a
  failed download row whose same URL later completed successfully — the
  receipt-less duplicate failure rows one click used to accumulate (fixed
  for new submissions in 1.3.10) and pre-fix leftovers. 0 disables.
"""
from __future__ import annotations

import sqlite3
import threading
import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _utc(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # Existing queue timestamps are machine-local wall-clock timestamps.
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


SUPERSEDED_FAILURE_RETENTION_HOURS = 24


class FailedTaskRetention:
    def __init__(self, database_path):
        self.database_path = str(Path(database_path).resolve())
        self._lock = threading.Lock()
        self._last_run = None

    @staticmethod
    def _policy(settings):
        days = settings.get("failed_task_retention_days", 0)
        hours = settings.get("failed_task_cleanup_interval_hours", 24)
        superseded_hours = settings.get(
            "superseded_failure_retention_hours", SUPERSEDED_FAILURE_RETENTION_HOURS)
        if (type(days) is not int or not 0 <= days <= 3650 or type(hours) is not int
                or not 1 <= hours <= 8760 or type(superseded_hours) is not int
                or not 0 <= superseded_hours <= 8760):
            raise ValueError("失败任务保留配置无效")
        return days, hours, superseded_hours

    def _connect(self, *, write=False):
        connection = sqlite3.connect(Path(self.database_path).as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if not write:
            connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _candidates(connection, cutoff):
        jobs = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM jobs")}
        downloads = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM download_jobs")}
        manifest_jobs = {row[0] for row in connection.execute("SELECT job_id FROM manifests")}
        linked_jobs = {row.get("job_id") for row in downloads.values() if row.get("job_id")}
        canonical = lambda value: os.path.normcase(os.path.abspath(str(value)))
        media_paths = {canonical(row[0]) for row in connection.execute("SELECT source_path FROM media")}
        keep, job_ids, download_ids = 0, [], []
        for row in jobs.values():
            when = _utc(row.get("updated_at"))
            if row["status"] != "failed" or when is None or when > cutoff:
                continue
            if row.get("raw_path") or row.get("final_path") or row["id"] in manifest_jobs or row["id"] in linked_jobs or canonical(row.get("source_path")) in media_paths:
                keep += 1
            else:
                job_ids.append(row["id"])
        for row in downloads.values():
            when = _utc(row.get("updated_at"))
            if row["status"] != "failed" or when is None or when > cutoff:
                continue
            if row.get("media_id") or row.get("job_id") or row.get("downloaded_path"):
                keep += 1
            else:
                download_ids.append(row["id"])
        return job_ids, download_ids, keep

    @staticmethod
    def _superseded_candidates(connection, cutoff):
        """Failed download rows whose same URL later completed successfully.

        A success is any done row (or row holding a downloaded file) whose
        requested_at is not older than the failed row's — i.e. the failed
        attempt has been superseded, not merely accompanied. Rows that ever
        produced media, a linked job or a file are protected and counted.
        """
        successes = {}
        for row in connection.execute(
                "SELECT url, requested_at, updated_at, media_id, job_id, downloaded_path "
                "FROM download_jobs WHERE status IN ('done','imported','queued','transcribing') "
                "OR downloaded_path IS NOT NULL"):
            when = _utc(row["requested_at"]) or _utc(row["updated_at"])
            if when is not None and when > successes.get(row["url"], datetime.min.replace(tzinfo=timezone.utc)):
                successes[row["url"]] = when
        doomed, protected = [], 0
        for row in connection.execute(
                "SELECT id, url, requested_at, updated_at, media_id, job_id, downloaded_path "
                "FROM download_jobs WHERE status='failed'"):
            success_at = successes.get(row["url"])
            if success_at is None:
                continue
            when = _utc(row["updated_at"])
            attempted_at = _utc(row["requested_at"]) or when
            if when is None or attempted_at is None or when > cutoff or attempted_at > success_at:
                continue
            if row["media_id"] or row["job_id"] or row["downloaded_path"]:
                protected += 1
                continue
            doomed.append(row["id"])
        return doomed, protected

    def preview(self, settings, *, now=None):
        days, hours, superseded_hours = self._policy(settings)
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        result = {"ok": True, "retention_days": days, "interval_hours": hours,
                  "superseded_retention_hours": superseded_hours,
                  "jobs": [], "downloads": [], "superseded": [],
                  "protected": 0, "superseded_protected": 0, "files_deleted": 0}
        if days == 0 and superseded_hours == 0:
            result["enabled"] = False
            return result
        result["enabled"] = True
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            if days > 0:
                cutoff = now - timedelta(days=days)
                jobs, downloads, protected = self._candidates(db, cutoff)
                result.update({"cutoff_at": cutoff.isoformat(), "jobs": jobs,
                               "downloads": downloads, "protected": protected})
            if superseded_hours > 0:
                superseded, superseded_protected = self._superseded_candidates(
                    db, now - timedelta(hours=superseded_hours))
                result.update({"superseded": superseded, "superseded_protected": superseded_protected})
        return result

    def run_if_due(self, settings, *, now=None, force=False):
        days, hours, superseded_hours = self._policy(settings)
        if days == 0 and superseded_hours == 0:
            return {**self.preview(settings), "ran": False,
                    "removed_jobs": 0, "removed_downloads": 0, "removed_superseded": 0}
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock:
            if not force and self._last_run is not None and now < self._last_run + timedelta(hours=hours):
                return {"ok": True, "enabled": True, "ran": False, "removed_jobs": 0,
                        "removed_downloads": 0, "removed_superseded": 0, "files_deleted": 0}
            db = self._connect(write=True)
            try:
                db.execute("BEGIN IMMEDIATE")
                jobs, downloads, protected = [], [], 0
                if days > 0:
                    jobs, downloads, protected = self._candidates(db, now - timedelta(days=days))
                superseded, superseded_protected = [], 0
                if superseded_hours > 0:
                    superseded, superseded_protected = self._superseded_candidates(
                        db, now - timedelta(hours=superseded_hours))
                for download_id in [*downloads, *superseded]:
                    # Expiration must not let a delayed extension replay create
                    # the same removed task again. Keep opaque request tombstones.
                    db.execute("INSERT OR IGNORE INTO cancelled_download_requests(client_request_id) SELECT client_request_id FROM download_requests WHERE download_id=?", (download_id,))
                    db.execute("DELETE FROM download_requests WHERE download_id=?", (download_id,))
                    db.execute("DELETE FROM download_jobs WHERE id=? AND status='failed'", (download_id,))
                for job_id in jobs:
                    db.execute("DELETE FROM jobs WHERE id=? AND status='failed'", (job_id,))
                db.commit()
                self._last_run = now
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
            return {"ok": True, "enabled": True, "ran": True, "removed_jobs": len(jobs),
                    "removed_downloads": len(downloads), "removed_superseded": len(superseded),
                    "protected": protected, "superseded_protected": superseded_protected,
                    "files_deleted": 0}
