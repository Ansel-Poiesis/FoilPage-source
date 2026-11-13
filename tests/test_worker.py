# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""worker 执行链自测。

覆盖：
1. classify_error：终态失败 / 可重试分类（含 allowlist 钩子）。
2. 端到端成功：enqueue 真实 MP4 作业 → Worker.step() → status=done + manifest 入 DB + 产物文件存在。
   （需 HF_HOME 指向已缓存 tiny 模型，免联网；见运行命令。）
3. 失败路径：不存在的源文件 → status=failed 且 error code 落库。
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from app import worker
from engine import queue_store
from account_auth_fixtures import grant_account


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_store():
    path = str(Path(tempfile.mkdtemp(prefix="worker_account_")) / "queue.db")
    return queue_store.QueueStore(path), path


def _cleanup(store: queue_store.QueueStore, db_path: str) -> None:
    store.close()
    for p in (db_path, db_path + "-wal", db_path + "-shm"):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    (Path(db_path).parent / "device.id").unlink(missing_ok=True)
    Path(db_path).parent.rmdir()


def _enqueue(store: queue_store.QueueStore, source: str, output_dir: str, language: str = "en") -> str:
    jid = uuid.uuid4().hex[:12]
    store.enqueue({
        "id": jid,
        "source_path": source,
        "output_dir": output_dir,
        "status": "queued",
        "whisper_model": "tiny",
        "device": "cpu",
        "language": language,
        "content_mode": "raw",
        "created_at": _ts(),
        "updated_at": _ts(),
    })
    return jid


# ---- 1. 分类 ----
def test_classify_default_failed():
    assert worker.classify_error("ffmpeg_missing") == "failed"
    assert worker.classify_error("corrupt_media") == "failed"
    assert worker.classify_error("cuda_unavailable") == "failed"


def test_classify_media_timeout_retryable():
    # ffmpeg 有界执行超限（00-待办 #4）：挂死进程释放槽位后重试通常恢复。
    assert worker.classify_error("media_timeout") == "retryable"


def test_classify_retryable_when_allowed():
    saved = worker._RETRYABLE_CODES
    worker._RETRYABLE_CODES = frozenset({"corrupt_media"})
    try:
        assert worker.classify_error("corrupt_media") == "retryable"
        assert worker.classify_error("ffmpeg_missing") == "failed"
    finally:
        worker._RETRYABLE_CODES = saved


# ---- 2. 端到端成功 ----
def test_end_to_end_success():
    source = str(ROOT / "assets" / "sample.mp4")
    assert Path(source).exists(), f"缺测试素材: {source}"
    out_dir = tempfile.mkdtemp(prefix="wtest_out_")
    store, db_path = _new_store()
    try:
        jid = _enqueue(store, source, out_dir, language="en")
        w = worker.Worker(store, model="tiny", device="cpu", language="en", worker_id="t1",
                          agent_settings=grant_account({}, Path(db_path).parent))
        assert w.step() is True

        job = store.get_job(jid)
        assert job is not None, "作业应存在"
        assert job["status"] == "done", f"期望 done，实际 {job['status']}"
        assert job["raw_path"] and job["final_path"]

        man = store.get_manifest(jid)
        assert man is not None, "manifest 应入 DB"
        assert man["backend"] == "local"

        raw_file = Path(job["raw_path"])
        assert raw_file.exists(), f"产物缺失: {raw_file}"
        text = raw_file.read_text(encoding="utf-8").strip()
        assert "moon" in text.lower(), f"转写异常: {text!r}"
    finally:
        _cleanup(store, db_path)


# ---- 3. 失败路径 ----
def test_failure_path_missing_source():
    store, db_path = _new_store()
    try:
        jid = _enqueue(store, "C:/no/such/file.mp4", tempfile.mkdtemp(prefix="wtest_fail_"))
        w = worker.Worker(store, model="tiny", device="cpu", language="en",
                          agent_settings=grant_account({}, Path(db_path).parent))
        w.step()
        job = store.get_job(jid)
        assert job["status"] == "failed", f"期望 failed，实际 {job['status']}"
        assert job["error"] and job["error"]["code"] == "corrupt_media"
    finally:
        _cleanup(store, db_path)


# ---- 4. 转写完成后清理 ----
def test_delete_process_files_keeps_final_and_manifest():
    job_dir = Path(tempfile.mkdtemp(prefix="wtest_proc_"))
    try:
        raw = job_dir / "样片_raw.md"
        final = job_dir / "样片.md"
        manifest = job_dir / "样片_manifest.json"
        segments = job_dir / "样片_segments.json"
        extra = job_dir / "sample.wav"
        for path in (raw, final, manifest, segments, extra):
            path.write_text("x", encoding="utf-8")

        worker.Worker._delete_process_files({
            "raw_path": str(raw),
            "final_path": str(final),
        })

        assert not raw.exists(), "原始稿应被删除"
        assert not extra.exists(), "中间音频应被删除"
        assert final.exists(), "最终稿应保留"
        assert manifest.exists(), "manifest 应保留"
        assert segments.exists(), "字幕时间轴是资产，应保留"
    finally:
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)


def test_delete_process_files_agent_article_keeps_article():
    job_dir = Path(tempfile.mkdtemp(prefix="wtest_proc_agent_"))
    try:
        raw = job_dir / "样片_raw.md"
        article = job_dir / "新标题_文章.md"
        manifest = job_dir / "样片_manifest.json"
        for path in (raw, article, manifest):
            path.write_text("x", encoding="utf-8")

        worker.Worker._delete_process_files({
            "raw_path": str(raw),
            "final_path": str(article),
        })

        assert not raw.exists(), "原始稿应被删除"
        assert article.exists(), "Agent 文章应保留"
        assert manifest.exists(), "manifest 应保留"
    finally:
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)


if __name__ == "__main__":
    test_classify_default_failed()
    test_classify_retryable_when_allowed()
    test_end_to_end_success()
    test_failure_path_missing_source()
    test_delete_process_files_keeps_final_and_manifest()
    test_delete_process_files_agent_article_keeps_article()
    print("OK worker 自测通过")


# ---- 4. 独立标题优化后的标题回写开关 ----
def _agent_manifest(jid: str, source: str, out_dir: str) -> dict:
    return {
        "job_id": jid,
        "source_path": source,
        "source_sha256": "0" * 64,
        "backend": "local",
        "model": "tiny",
        "language": "en",
        "duration_sec": 0.0,
        "raw_path": str(Path(out_dir) / "raw.md"),
        "final_path": str(Path(out_dir) / "final.md"),
        "content_mode": "raw",
        "prompt_version": "v1",
        "created_at": _ts(),
        "source_title": "NEW TITLE",
        "agent_status": "done",
        "agent_tags": ["复盘", "访谈"],
    }


def _run_agent_job(monkeypatch, store, db_path, *, backfill_enabled: bool) -> str:
    import app.worker as worker_mod
    source = str(ROOT / "assets" / "sample.mp4")
    out_dir = tempfile.mkdtemp(prefix="wtest_agent_")
    jid = _enqueue(store, source, out_dir, language="en")
    store.set_source_title(jid, "ORIGINAL TITLE")
    claimed = store.claim_next("test-agent")
    assert claimed and claimed["id"] == jid
    job = store.get_job(jid)

    def fake_run(*_a, **_k):
        return dict(_agent_manifest(jid, source, out_dir))

    def fake_agent(manifest, *_a, **_k):
        out = dict(manifest)
        out["source_title"] = "ORIGINAL TITLE"
        out["agent_status"] = "done"
        out["agent_tags"] = ["复盘", "访谈"]
        return out

    monkeypatch.setattr("run_engine.run", fake_run)
    monkeypatch.setattr("app.agent_service.apply_agent_translation", fake_agent)
    def fake_title(manifest, *_a, **_k):
        return {**manifest, "source_title": "NEW TITLE", "optimized_source_title": "NEW TITLE",
                "title_status": "done", "title_enabled_snapshot": True}
    monkeypatch.setattr("app.agent_service.apply_title_optimization", fake_title)

    w = worker_mod.Worker(
        store, model="tiny", device="cpu", language="en", worker_id="test-agent",
        agent_settings=grant_account({
            "agent_enabled": True,
            "title_optimization_enabled": True,
            "update_title_after_transcribe": backfill_enabled,
            "delete_source_after_translation": False,
        }, Path(db_path).parent),
    )
    w._run_job(job)
    return jid


def test_worker_title_backfill_respects_setting(monkeypatch):
    store, db_path = _new_store()
    try:
        jid_on = _run_agent_job(monkeypatch, store, db_path, backfill_enabled=True)
        assert store.get_job(jid_on)["source_title"] == "NEW TITLE"
        assert store.get_job(jid_on)["tags"] == ["复盘", "访谈"]

        store2, db2 = _new_store()
        try:
            jid_off = _run_agent_job(monkeypatch, store2, db2, backfill_enabled=False)
            assert store2.get_job(jid_off)["source_title"] != "NEW TITLE"
        finally:
            _cleanup(store2, db2)
    finally:
        _cleanup(store, db_path)
