# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine.media_store import MediaStore
from engine.queue_store import QueueStore
from engine.download_store import DownloadStore
from engine.media_relink import MediaRelinker, MediaRelinkError
from engine.retention import FailedTaskRetention


@pytest.fixture
def stores(tmp_path):
    db = tmp_path / "library.db"
    media, queue, downloads = MediaStore(str(db)), QueueStore(str(db)), DownloadStore(str(db))
    yield db, media, queue, downloads
    downloads.close(); queue.close(); media.close()


def job(queue, source, *, status="failed", age=50, raw=None):
    stamp = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()
    row = {"id": uuid.uuid4().hex[:12], "source_path": str(source), "output_dir": str(Path(source).parent / "output"),
           "status": "queued", "whisper_model": "tiny", "device": "cpu", "language": "auto", "content_mode": "raw",
           "created_at": stamp, "updated_at": stamp, "raw_path": raw, "final_path": None, "error": None,
           "attempts": 0, "max_attempts": 3, "cancel_requested": False}
    queue.enqueue(row)
    with sqlite3.connect(queue.db_path) as conn:
        conn.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=?", (status, stamp, row["id"]))
    return row["id"]


def download(db, store, *, status="failed", age=50, media_id=None, job_id=None, path=None):
    row = store.create("https://example.com/video", "test", "youtube", {})
    stamp = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE download_jobs SET status=?,updated_at=?,media_id=?,job_id=?,downloaded_path=? WHERE id=?",
                     (status, stamp, media_id, job_id, str(path) if path else None, row["id"]))
    return row["id"]


def test_path_relink_keeps_ids_documents_manifests_and_can_undo(stores, tmp_path):
    db, media, queue, downloads = stores
    old, new = tmp_path / "old.mp4", tmp_path / "restored" / "new.mp4"
    old.write_bytes(b"same verified video")
    item = media.import_file(str(old)).item
    raw = tmp_path / "document.md"; raw.write_text("private content", encoding="utf-8")
    jid = job(queue, old, status="done", raw=str(raw))
    did = download(db, downloads, status="done", media_id=item["id"], job_id=jid, path=old)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO manifests(job_id,source_path,backend,model,language,duration_sec,raw_path,final_path,content_mode,prompt_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (jid,str(old),"local","tiny","auto",1,str(raw),str(raw),"raw","v1","2026-01-01"))
    new.parent.mkdir(); new.write_bytes(old.read_bytes()); old.unlink()
    relink = MediaRelinker(db)
    preview = relink.preview(item["id"], new)
    result = relink.apply(item["id"], new, expected_revision=preview["expected_revision"])
    assert result["affected"] == {"media": 1, "jobs": 1, "downloads": 1}
    assert media.get_media(item["id"])["source_path"] == str(new)
    assert queue.get_job(jid)["source_path"] == str(new)
    assert downloads.get(did)["downloaded_path"] == str(new)
    assert raw.read_text("utf-8") == "private content" and new.is_file()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT source_path FROM manifests WHERE job_id=?", (jid,)).fetchone()[0] == str(old)
    relink.rollback(result["undo"], expected_revision=result["after_revision"])
    assert media.get_media(item["id"])["source_path"] == str(old)
    assert queue.get_job(jid)["source_path"] == str(old)
    assert downloads.get(did)["downloaded_path"] == str(old)
    assert new.is_file() and raw.is_file()


@pytest.mark.parametrize("condition", ["active", "different", "stale", "source_present"])
def test_relink_rejects_unsafe_candidates_without_changes(stores, tmp_path, condition):
    db, media, queue, downloads = stores
    old = tmp_path / "old.mp4"; new = tmp_path / "new.mp4"
    old.write_bytes(b"same video"); new.write_bytes(old.read_bytes())
    item = media.import_file(str(old)).item
    if condition != "source_present": old.unlink()
    if condition == "active": job(queue, old, status="running")
    if condition == "different": new.write_bytes(b"diff video")
    relink = MediaRelinker(db)
    before = media.get_media(item["id"])
    with pytest.raises(MediaRelinkError):
        preview = relink.preview(item["id"], new)
        if condition == "stale":
            job(queue, old)
        relink.apply(item["id"], new, expected_revision=preview["expected_revision"])
    assert media.get_media(item["id"])["source_path"] == before["source_path"]
    assert new.is_file()


def test_retention_disabled_even_force_never_opens_database(tmp_path):
    # 完全禁用 = 两条规则都关；此时即便 force 也不创建/打开数据库。
    retention = FailedTaskRetention(tmp_path / "does-not-exist.db")
    result = retention.run_if_due({"superseded_failure_retention_hours": 0}, force=True)
    assert result["ran"] is False
    assert not (tmp_path / "does-not-exist.db").exists()


def test_superseded_failures_expire_by_default_after_one_day(stores):
    db, media, queue, downloads = stores
    url = "https://www.douyin.com/video/9001234567890123"
    stale = download(db, downloads, status="failed", age=3)
    fresh_fail = download(db, downloads, status="failed", age=0.2)
    done = download(db, downloads, status="done", age=0.1, path=str(db.parent / "ok.mp4"))
    conn = downloads._conn
    conn.execute("UPDATE download_jobs SET url=?, updated_at=datetime('now','localtime','-3 day'), requested_at=datetime('now','localtime','-3 day') WHERE id=?", (url, stale))
    conn.execute("UPDATE download_jobs SET url=?, updated_at=datetime('now','localtime','-4 hour'), requested_at=datetime('now','localtime','-4 hour') WHERE id=?", (url, fresh_fail))
    conn.execute("UPDATE download_jobs SET url=?, status='done', updated_at=datetime('now','localtime','-2 hour'), requested_at=datetime('now','localtime','-2 hour') WHERE id=?", (url, done))
    conn.commit()
    retention = FailedTaskRetention(str(db))
    previewed = retention.preview({})
    assert stale in previewed["superseded"]
    assert fresh_fail not in previewed["superseded"], "未过保留期的失败行必须保留"
    result = retention.run_if_due({}, force=True)
    assert result["removed_superseded"] == 1
    remaining = {row["id"] for row in downloads.list()}
    assert stale not in remaining and fresh_fail in remaining


def test_superseded_rule_protects_rows_with_content_and_respects_zero(stores):
    db, media, queue, downloads = stores
    url = "https://www.bilibili.com/video/BV1supersede1"
    occupied = download(db, downloads, status="failed", age=5, media_id="m-keep")
    later = download(db, downloads, status="done", age=0.1, path=str(db.parent / "later.mp4"))
    conn = downloads._conn
    conn.execute("UPDATE download_jobs SET url=? WHERE id=?", (url, occupied))
    conn.execute("UPDATE download_jobs SET url=?, status='done' WHERE id=?", (url, later))
    conn.commit()
    retention = FailedTaskRetention(str(db))
    result = retention.run_if_due({}, force=True)
    assert result["removed_superseded"] == 0
    assert result["superseded_protected"] >= 1
    assert downloads.get(occupied) is not None
    off = retention.run_if_due({"superseded_failure_retention_hours": 0}, force=True)
    assert off["ran"] is False


def test_retention_only_old_empty_failed_rows_and_throttles(stores, tmp_path):
    db, media, queue, downloads = stores
    old = job(queue, tmp_path / "unregistered-missing.mp4")
    young = job(queue, tmp_path / "young.mp4", age=1)
    running = job(queue, tmp_path / "running.mp4", status="running")
    done = job(queue, tmp_path / "done.mp4", status="done")
    cancelled = job(queue, tmp_path / "cancelled.mp4", status="cancelled")
    raw = tmp_path / "keep.md"; raw.write_text("keep", encoding="utf-8")
    with_text = job(queue, tmp_path / "text-source.mp4", raw=str(raw))
    source = tmp_path / "registered.mp4"; source.write_bytes(b"video")
    item = media.import_file(str(source)).item
    with_media = job(queue, source)
    d_old = download(db, downloads)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO download_requests(client_request_id,download_id) VALUES (?,?)", ("opaque-expired-request",d_old))
    d_file = download(db, downloads, path=source)
    d_media = download(db, downloads, media_id=item["id"])
    d_active = download(db, downloads, status="downloading")
    retention = FailedTaskRetention(db)
    settings = {"failed_task_retention_days": 30, "failed_task_cleanup_interval_hours": 24}
    preview = retention.preview(settings)
    assert preview["jobs"] == [old] and preview["downloads"] == [d_old]
    result = retention.run_if_due(settings)
    assert result["removed_jobs"] == result["removed_downloads"] == 1
    assert queue.get_job(old) is None and downloads.get(d_old) is None
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT client_request_id FROM cancelled_download_requests").fetchall() == [("opaque-expired-request",)]
    for jid in (young,running,done,cancelled,with_text,with_media): assert queue.get_job(jid)
    for did in (d_file,d_media,d_active): assert downloads.get(did)
    assert raw.read_text("utf-8") == "keep" and source.is_file()
    assert retention.run_if_due(settings)["ran"] is False


@pytest.mark.parametrize("settings", [{"failed_task_retention_days": True}, {"failed_task_retention_days": -1}, {"failed_task_cleanup_interval_hours": 0}])
def test_retention_invalid_policy_never_creates_database(tmp_path, settings):
    with pytest.raises(ValueError): FailedTaskRetention(tmp_path / "absent.db").run_if_due(settings)
    assert not (tmp_path / "absent.db").exists()


def test_relink_transaction_rolls_back_every_path_when_download_update_fails(stores, tmp_path):
    db, media, queue, downloads = stores
    old, new = tmp_path / "old.mp4", tmp_path / "new.mp4"
    old.write_bytes(b"verified video"); new.write_bytes(old.read_bytes())
    item = media.import_file(str(old)).item
    jid = job(queue, old, status="done")
    did = download(db, downloads, status="done", path=old, media_id=item["id"],job_id=jid)
    old.unlink()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TRIGGER fail_relink BEFORE UPDATE OF downloaded_path ON download_jobs BEGIN SELECT RAISE(ABORT,'synthetic fail'); END")
    relink = MediaRelinker(db)
    revision = relink.preview(item["id"],new)["expected_revision"]
    with pytest.raises(sqlite3.IntegrityError): relink.apply(item["id"],new,expected_revision=revision)
    assert media.get_media(item["id"])["source_path"] == str(old)
    assert queue.get_job(jid)["source_path"] == str(old)
    assert downloads.get(did)["downloaded_path"] == str(old)
    assert new.is_file()


def test_rollback_refuses_changes_after_relink(stores, tmp_path):
    db, media, queue, downloads = stores
    old, new = tmp_path / "old.mp4", tmp_path / "new.mp4"
    old.write_bytes(b"verified video"); new.write_bytes(old.read_bytes())
    item = media.import_file(str(old)).item
    jid = job(queue, old, status="done")
    old.unlink(); relink = MediaRelinker(db)
    revision = relink.preview(item["id"],new)["expected_revision"]
    result = relink.apply(item["id"],new,expected_revision=revision)
    with sqlite3.connect(db) as conn: conn.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
    with pytest.raises(MediaRelinkError): relink.rollback(result["undo"],expected_revision=result["after_revision"])
    assert media.get_media(item["id"])["source_path"] == str(new)
    assert new.is_file()


def test_candidate_mutation_between_preview_and_apply_is_rejected(stores, tmp_path, monkeypatch):
    db, media, queue, downloads = stores
    old, new = tmp_path / "old.mp4", tmp_path / "new.mp4"
    old.write_bytes(b"verified video"); new.write_bytes(old.read_bytes())
    item = media.import_file(str(old)).item
    old.unlink(); relink = MediaRelinker(db)
    revision = relink.preview(item["id"],new)["expected_revision"]
    original = relink.preview
    def mutate(*args):
        result = original(*args)
        new.write_bytes(b"changed after verification")
        return result
    monkeypatch.setattr(relink,"preview",mutate)
    with pytest.raises(MediaRelinkError): relink.apply(item["id"],new,expected_revision=revision)
    assert media.get_media(item["id"])["source_path"] == str(old)


@pytest.mark.parametrize("linked", ["job", "download"])
def test_relink_candidate_already_used_by_other_history_is_not_merged(stores, tmp_path, linked):
    db, media, queue, downloads = stores
    old, new = tmp_path / "old.mp4", tmp_path / "new.mp4"
    old.write_bytes(b"verified video"); new.write_bytes(old.read_bytes())
    item = media.import_file(str(old)).item
    if linked == "job": job(queue,new,status="done")
    else: download(db,downloads,status="done",path=new)
    old.unlink()
    with pytest.raises(MediaRelinkError) as exc: MediaRelinker(db).preview(item["id"],new)
    assert exc.value.code == "path_conflict"
    assert media.get_media(item["id"])["source_path"] == str(old)
