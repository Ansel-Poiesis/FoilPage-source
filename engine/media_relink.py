# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Explicit CAS repair of missing media paths, keeping files and history intact."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from datetime import datetime
from pathlib import Path


class MediaRelinkError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _key(path):
    return os.path.normcase(str(Path(path).expanduser().absolute()))


def _sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _file(path):
    value = Path(path).expanduser().absolute()
    for part in (value, *value.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise MediaRelinkError("invalid_path", "不支持链接或重解析路径")
    info = value.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise MediaRelinkError("invalid_path", "请选择单一普通媒体文件")
    return value.resolve(), info


def _revision(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class MediaRelinker:
    def __init__(self, database_path):
        self.database_path = str(Path(database_path).resolve())

    def _connect(self, *, write=False):
        db = sqlite3.connect(Path(self.database_path).as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if not write:
            db.execute("PRAGMA query_only=ON")
        return db

    @staticmethod
    def _snapshot(db, media_id):
        row = db.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
        if row is None:
            raise MediaRelinkError("not_found", "媒体记录不存在")
        media = dict(row)
        old = _key(media["source_path"])
        jobs = [dict(r) for r in db.execute("SELECT id,source_path,status FROM jobs") if _key(r["source_path"]) == old]
        ids = {row["id"] for row in jobs}
        downloads = [dict(r) for r in db.execute("SELECT id,media_id,job_id,downloaded_path,status FROM download_jobs")
                     if r["media_id"] == media_id or r["job_id"] in ids or r["downloaded_path"] and _key(r["downloaded_path"]) == old]
        return {"media": media, "jobs": sorted(jobs, key=lambda r: r["id"]), "downloads": sorted(downloads, key=lambda r: r["id"])}

    @staticmethod
    def _guard(db, snapshot, new_path):
        media = snapshot["media"]
        if Path(media["source_path"]).exists():
            raise MediaRelinkError("source_available", "原文件仍在，请保留原关联")
        keys = {_key(media["source_path"]), _key(new_path)}
        job_ids = {r["id"] for r in snapshot["jobs"]}
        for row in db.execute("SELECT id,source_path,status FROM jobs"):
            if _key(row["source_path"]) in keys and row["status"] not in {"done", "failed", "cancelled"}:
                raise MediaRelinkError("active_media", "媒体正在处理，请完成或取消后修复")
            if _key(row["source_path"]) == _key(new_path) and row["id"] not in job_ids:
                raise MediaRelinkError("path_conflict", "候选位置已经关联其他任务")
        if any(row["status"] not in {"done", "failed", "cancelled"} for row in snapshot["downloads"]):
            raise MediaRelinkError("active_media", "媒体正在下载或处理，请稍后修复")
        download_ids = {r["id"] for r in snapshot["downloads"]}
        if any(row["downloaded_path"] and _key(row["downloaded_path"]) == _key(new_path) and row["id"] not in download_ids
               for row in db.execute("SELECT id,downloaded_path FROM download_jobs")):
            raise MediaRelinkError("path_conflict", "候选位置已经关联其他下载")
        duplicate = db.execute("SELECT id FROM media WHERE canonical_path=? AND id<>?", (_key(new_path), media["id"])).fetchone()
        if duplicate:
            raise MediaRelinkError("path_conflict", "新位置已经关联其他媒体")

    def preview(self, media_id, candidate_path):
        path, info = _file(candidate_path)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            snapshot = self._snapshot(db, media_id)
            self._guard(db, snapshot, path)
        media = snapshot["media"]
        if path.suffix.lower() != media["extension"] or info.st_size != media["size_bytes"]:
            raise MediaRelinkError("content_mismatch", "候选文件与原媒体不一致")
        if _sha(path) != media["content_sha256"] or _identity(info) != _identity(path.stat()):
            raise MediaRelinkError("content_mismatch", "候选文件内容不同或仍在写入")
        return {"ok": True, "media_id": media_id, "old_path": media["source_path"], "new_path": str(path),
                "expected_revision": _revision(snapshot), "content_sha256": media["content_sha256"],
                "candidate_identity": list(_identity(info)),
                "affected": {"media": 1, "jobs": len(snapshot["jobs"]),
                             "downloads": sum(bool(r["downloaded_path"]) and _key(r["downloaded_path"]) == _key(media["source_path"]) for r in snapshot["downloads"])},
                "files_moved": 0, "files_deleted": 0}

    def apply(self, media_id, candidate_path, *, expected_revision):
        preview = self.preview(media_id, candidate_path)
        if preview["expected_revision"] != expected_revision:
            raise MediaRelinkError("revision_conflict", "媒体关联已变化，请重新检查")
        path, info = _file(preview["new_path"])
        if list(_identity(info)) != preview["candidate_identity"]:
            raise MediaRelinkError("content_mismatch", "候选文件在检查后发生变化")
        db = self._connect(write=True)
        try:
            db.execute("BEGIN IMMEDIATE")
            before = self._snapshot(db, media_id)
            if _revision(before) != expected_revision:
                raise MediaRelinkError("revision_conflict", "媒体关联已变化，请重新检查")
            self._guard(db, before, path)
            if _identity(info) != _identity(path.stat()):
                raise MediaRelinkError("content_mismatch", "候选文件在检查后发生变化")
            old_key = _key(before["media"]["source_path"])
            now = datetime.now().isoformat(timespec="seconds")
            db.execute("UPDATE media SET source_path=?,canonical_path=?,modified_ns=?,missing=0,updated_at=? WHERE id=?",
                       (str(path), _key(path), info.st_mtime_ns, now, media_id))
            for row in before["jobs"]:
                db.execute("UPDATE jobs SET source_path=? WHERE id=?", (str(path), row["id"]))
            changed_downloads = []
            for row in before["downloads"]:
                if row["downloaded_path"] and _key(row["downloaded_path"]) == old_key:
                    db.execute("UPDATE download_jobs SET downloaded_path=? WHERE id=?", (str(path), row["id"]))
                    changed_downloads.append(row["id"])
            after = self._snapshot(db, media_id)
            if _identity(info) != _identity(path.stat()):
                raise MediaRelinkError("content_mismatch", "候选文件在保存时发生变化")
            db.commit()
            # The before/after snapshots contain path metadata only (no text,
            # tokens or signed URLs), suitable for a protected local undo receipt.
            return {"ok": True, "media_id": media_id, "old_path": before["media"]["source_path"],
                    "new_path": str(path), "affected": {"media": 1, "jobs": len(before["jobs"]), "downloads": len(changed_downloads)},
                    "before_revision": expected_revision, "after_revision": _revision(after),
                    "undo": {"before": before, "after": after}, "files_moved": 0, "files_deleted": 0,
                    "manifest_history_changed": False}
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rollback(self, undo, *, expected_revision):
        """Restore only this path patch if every affected row is still unchanged.

        Intended for the protected local receipt of apply(), not general SQL or
        recovery of files. It does not move the replacement or recreate source.
        """
        before, after = undo["before"], undo["after"]
        media_id = before["media"]["id"]
        if after["media"]["id"] != media_id or _revision(after) != expected_revision:
            raise MediaRelinkError("revision_conflict", "恢复回执无效")
        permitted = {"source_path", "canonical_path", "modified_ns", "missing", "updated_at"}
        if set(before["media"]) != set(after["media"]) or any(before["media"][k] != after["media"][k] for k in before["media"] if k not in permitted):
            raise MediaRelinkError("invalid_receipt", "回执包含其他媒体变更")
        if _key(before["media"]["source_path"]) != before["media"]["canonical_path"]:
            raise MediaRelinkError("invalid_receipt", "回执路径无效")
        for kind, field in (("jobs", "source_path"), ("downloads", "downloaded_path")):
            if len(before[kind]) != len(after[kind]):
                raise MediaRelinkError("invalid_receipt", "回执关联无效")
            for old, new in zip(before[kind], after[kind]):
                if set(old) != set(new) or any(old[k] != new[k] for k in old if k != field):
                    raise MediaRelinkError("invalid_receipt", "回执包含其他任务变更")
                if old[field] != new[field] and (_key(old[field]) != _key(before["media"]["source_path"]) or _key(new[field]) != _key(after["media"]["source_path"])):
                    raise MediaRelinkError("invalid_receipt", "回执路径不一致")
        db = self._connect(write=True)
        try:
            db.execute("BEGIN IMMEDIATE")
            current = self._snapshot(db, media_id)
            if _revision(current) != expected_revision:
                raise MediaRelinkError("revision_conflict", "关联在修复后已更改，保留当前资料")
            keys = {_key(before["media"]["source_path"]), _key(after["media"]["source_path"])}
            if any(_key(r["source_path"]) in keys and r["status"] not in {"done", "failed", "cancelled"} for r in db.execute("SELECT source_path,status FROM jobs")) or any(r["status"] not in {"done", "failed", "cancelled"} for r in current["downloads"]):
                raise MediaRelinkError("active_media", "媒体正在使用，暂不恢复旧路径")
            old = before["media"]
            db.execute("UPDATE media SET source_path=?,canonical_path=?,modified_ns=?,missing=?,updated_at=? WHERE id=?",
                       (old["source_path"], old["canonical_path"], old["modified_ns"], old["missing"], old["updated_at"], media_id))
            for row in before["jobs"]:
                db.execute("UPDATE jobs SET source_path=? WHERE id=?", (row["source_path"], row["id"]))
            for row in before["downloads"]:
                db.execute("UPDATE download_jobs SET downloaded_path=? WHERE id=?", (row["downloaded_path"], row["id"]))
            restored = self._snapshot(db, media_id)
            if _revision(restored) != _revision(before):
                raise MediaRelinkError("revision_conflict", "恢复检查失败")
            db.commit()
            return {"ok": True, "media_id": media_id, "restored_revision": _revision(restored), "files_moved": 0, "files_deleted": 0}
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
