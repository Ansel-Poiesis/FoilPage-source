# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""QueueStore consistency, concurrency, lease, cancellation and retry tests."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import multiprocessing
import os
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from engine import queue_store, validate  # noqa: E402


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _job(
    job_id: str | None = None,
    *,
    status: str = "queued",
    max_attempts: int = 3,
) -> dict:
    now = _ts()
    return {
        "id": job_id or uuid.uuid4().hex[:12],
        "source_path": "C:/media/sample.mp4",
        "output_dir": "C:/output",
        "status": status,
        "whisper_model": "tiny",
        "device": "cpu",
        "language": "en",
        "content_mode": "raw",
        "created_at": now,
        "updated_at": now,
        "error": None,
        "raw_path": None,
        "final_path": None,
        "worker_id": None,
        "claimed_at": None,
        "heartbeat_at": None,
        "attempts": 0,
        "max_attempts": max_attempts,
        "cancel_requested": False,
    }


def _manifest(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "source_path": "C:/media/sample.mp4",
        "source_sha256": "deadbeef",
        "backend": "local",
        "model": "tiny",
        "language": "en",
        "duration_sec": 1.0,
        "raw_path": f"C:/output/{job_id}/sample_raw.md",
        "final_path": f"C:/output/{job_id}/sample.md",
        "content_mode": "raw",
        "prompt_version": "v1",
        "created_at": _ts(),
    }


def _new_store() -> tuple[queue_store.QueueStore, str]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return queue_store.QueueStore(path), path


def _cleanup(store: queue_store.QueueStore | None, path: str) -> None:
    if store is not None:
        store.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def _expect(exc_type: type[BaseException], fn) -> BaseException:
    try:
        fn()
    except exc_type as exc:
        return exc
    raise AssertionError(f"Expected {exc_type.__name__}")


def _claim_process(db_path: str, start, ready, results, worker_id: str) -> None:
    store = queue_store.QueueStore(db_path)
    try:
        ready.put(worker_id)
        if not start.wait(90):
            raise TimeoutError("Queue claim test did not release its startup barrier")
        claimed = store.claim_next(worker_id)
        results.put(claimed["id"] if claimed else None)
    finally:
        store.close()


def test_archived_field_roundtrip_and_done_guard() -> None:
    store, path = _new_store()
    try:
        job = _job(status="queued")
        store.enqueue(job)
        stored = store.get_job(job["id"])
        assert stored["archived"] is False, "新任务默认未归档"
        # 非 done 任务不可归档
        _expect(queue_store.QueueStateError, lambda: store.set_archived(job["id"], True))
        assert store.get_job(job["id"])["archived"] is False
        # 完成后再归档
        claimed = store.claim_next("archiver")
        assert claimed and claimed["id"] == job["id"]
        store.save_manifest(_manifest(job["id"]), worker_id="archiver")
        archived = store.set_archived(job["id"], True)
        assert archived["archived"] is True
        assert store.get_job(job["id"])["archived"] is True
        # 取消归档回滚
        store.set_archived(job["id"], False)
        assert store.get_job(job["id"])["archived"] is False
    finally:
        _cleanup(store, path)


def test_foreign_keys_and_schema_version() -> None:
    store, path = _new_store()
    try:
        assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] >= 5
    finally:
        _cleanup(store, path)


def test_enqueue_requires_queued() -> None:
    store, path = _new_store()
    try:
        _expect(queue_store.QueueStateError, lambda: store.enqueue(_job(status="done")))
    finally:
        _cleanup(store, path)


def test_claim_returns_running_with_lease() -> None:
    store, path = _new_store()
    try:
        item = _job()
        store.enqueue(item)
        claimed = store.claim_next("worker-a")
        assert claimed is not None
        assert claimed["status"] == "running"
        assert claimed["worker_id"] == "worker-a"
        assert claimed["attempts"] == 1
        assert claimed["claimed_at"] and claimed["heartbeat_at"]
    finally:
        _cleanup(store, path)


def test_illegal_transition_and_missing_job_are_rejected() -> None:
    store, path = _new_store()
    try:
        item = _job()
        store.enqueue(item)
        _expect(
            queue_store.QueueStateError,
            lambda: store.update_status(item["id"], "done"),
        )
        _expect(
            queue_store.JobNotFoundError,
            lambda: store.update_status("missing", "failed"),
        )
    finally:
        _cleanup(store, path)


def test_manifest_requires_existing_running_job() -> None:
    store, path = _new_store()
    try:
        _expect(
            queue_store.JobNotFoundError,
            lambda: store.save_manifest(_manifest("missing")),
        )
        item = _job()
        store.enqueue(item)
        _expect(
            queue_store.QueueStateError,
            lambda: store.save_manifest(_manifest(item["id"])),
        )
        assert store.get_manifest(item["id"]) is None
    finally:
        _cleanup(store, path)


def test_save_manifest_finishes_owned_running_job() -> None:
    store, path = _new_store()
    try:
        item = _job()
        store.enqueue(item)
        store.claim_next("worker-a")
        manifest = _manifest(item["id"])
        manifest["content_mode"] = "article"
        store.save_manifest(manifest, worker_id="worker-a")
        saved = store.get_job(item["id"])
        assert saved and saved["status"] == "done"
        assert saved["progress"] == 1.0
        assert saved["content_mode"] == "article"
        assert saved["worker_id"] is None
        assert store.get_manifest(item["id"]) is not None
    finally:
        _cleanup(store, path)


def test_running_progress_is_owned_persisted_and_reset_on_requeue() -> None:
    store, path = _new_store()
    try:
        item = _job()
        store.enqueue(item)
        store.claim_next("worker-a")
        updated = store.update_progress(item["id"], 0.42, "worker-a")
        assert updated["progress"] == 0.42
        _expect(
            queue_store.QueueStateError,
            lambda: store.update_progress(item["id"], 0.5, "worker-b"),
        )
        store.update_status(
            item["id"], "failed", {"code": "failed", "message": "failed"},
            worker_id="worker-a",
        )
        assert store.requeue(item["id"])["progress"] == 0.0
    finally:
        _cleanup(store, path)


def test_terminal_job_can_be_categorized_and_deleted() -> None:
    store, path = _new_store()
    try:
        item = _job()
        store.enqueue(item)
        store.claim_next("worker-a")
        store.save_manifest(_manifest(item["id"]), worker_id="worker-a")
        assert store.set_result_category(item["id"], "知识科普")["result_category"] == "知识科普"
        assert store.delete_job(item["id"]) is True
        assert store.get_job(item["id"]) is None
        assert store.get_manifest(item["id"]) is None
    finally:
        _cleanup(store, path)


def test_set_tags_roundtrip_and_only_done() -> None:
    store, path = _new_store()
    try:
        item = _job()
        store.enqueue(item)
        # 未完成的任务不能打标签
        _expect(queue_store.QueueStateError, lambda: store.set_tags(item["id"], ["a"]))

        store.claim_next("worker-a")
        store.save_manifest(_manifest(item["id"]), worker_id="worker-a")
        assert store.set_tags(item["id"], ["访谈", "访谈", " 复盘 "]) == ["访谈", "复盘"]

        job = store.get_job(item["id"])
        assert job["tags"] == ["访谈", "复盘"]
        # 覆盖写，不累加
        assert store.set_tags(item["id"], ["单条"]) == ["单条"]
        assert store.get_job(item["id"])["tags"] == ["单条"]
    finally:
        _cleanup(store, path)


def test_set_tags_validates_limits() -> None:
    store, path = _new_store()
    try:
        item = _job()
        store.enqueue(item)
        store.claim_next("worker-a")
        store.save_manifest(_manifest(item["id"]), worker_id="worker-a")
        _expect(
            validate.ContractValidationError,
            lambda: store.set_tags(item["id"], ["x" * 25]),
        )
        _expect(
            validate.ContractValidationError,
            lambda: store.set_tags(item["id"], [f"t{i}" for i in range(21)]),
        )
    finally:
        _cleanup(store, path)


def test_active_job_cannot_be_deleted() -> None:
    store, path = _new_store()
    try:
        item = _job()
        store.enqueue(item)
        _expect(queue_store.QueueStateError, lambda: store.delete_job(item["id"]))
    finally:
        _cleanup(store, path)


def test_cancel_request_blocks_manifest_commit() -> None:
    store, path = _new_store()
    try:
        item = _job()
        store.enqueue(item)
        store.claim_next("worker-a")
        store.request_cancel(item["id"])
        _expect(
            queue_store.CancellationRequestedError,
            lambda: store.save_manifest(
                _manifest(item["id"]), worker_id="worker-a"
            ),
        )
        assert store.get_manifest(item["id"]) is None
        assert store.get_job(item["id"])["status"] == "running"
    finally:
        _cleanup(store, path)


def test_cancel_and_requeue_semantics() -> None:
    store, path = _new_store()
    try:
        queued = _job()
        store.enqueue(queued)
        assert store.request_cancel(queued["id"])["status"] == "cancelled"
        assert store.requeue(queued["id"])["status"] == "queued"

        running = store.claim_next("worker-a")
        assert running is not None
        requested = store.request_cancel(running["id"])
        assert requested["status"] == "running"
        assert requested["cancel_requested"] is True
        store.update_status(running["id"], "cancelled", worker_id="worker-a")
        assert store.get_job(running["id"])["status"] == "cancelled"
    finally:
        _cleanup(store, path)


def test_stale_job_recovers_until_max_attempts() -> None:
    store, path = _new_store()
    try:
        item = _job(max_attempts=2)
        store.enqueue(item)
        store.claim_next("worker-a")
        with store._conn:
            store._conn.execute(
                "UPDATE jobs SET heartbeat_at='2000-01-01T00:00:00' WHERE id=?",
                (item["id"],),
            )
        assert store.recover_stale_jobs(1) == [item["id"]]
        assert store.get_job(item["id"])["status"] == "queued"

        store.claim_next("worker-b")
        with store._conn:
            store._conn.execute(
                "UPDATE jobs SET heartbeat_at='2000-01-01T00:00:00' WHERE id=?",
                (item["id"],),
            )
        assert store.recover_stale_jobs(1) == [item["id"]]
        failed = store.get_job(item["id"])
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "worker_lease_expired"
    finally:
        _cleanup(store, path)


def test_two_processes_claim_once() -> None:
    store, path = _new_store()
    processes = []
    ready = results = None
    try:
        item = _job()
        store.enqueue(item)
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        ready = ctx.Queue()
        results = ctx.Queue()
        processes = [
            ctx.Process(target=_claim_process, args=(path, start, ready, results, f"p{i}"))
            for i in range(2)
        ]
        for process in processes:
            process.start()
        # Spawn/import/database startup is separate from the concurrent claim.
        # Both workers must be ready before either may claim the same job.
        startup_deadline = time.monotonic() + 60
        ready_workers = {ready.get(timeout=max(0.01, startup_deadline - time.monotonic())) for _ in processes}
        assert ready_workers == {"p0", "p1"}
        start.set()
        values = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        assert sorted(v for v in values if v is not None) == [item["id"]]
        assert values.count(None) == 1
    finally:
        # Only these test-owned children may be stopped, including failed startup.
        for process in processes:
            if process.pid is None:
                continue
            if process.is_alive():
                process.terminate()
            process.join(5)
            if not process.is_alive():
                process.close()
        for channel in (ready, results):
            if channel is not None:
                channel.close()
                channel.join_thread()
        _cleanup(store, path)


def test_active_job_can_be_force_deleted() -> None:
    store, path = _new_store()
    try:
        item = _job()
        store.enqueue(item)
        store.claim_next("worker-a")
        assert store.is_cancel_requested(item["id"]) is False
        # 非 force 仍拒绝运行中删除
        _expect(queue_store.QueueStateError, lambda: store.delete_job(item["id"]))
        # force 直接删除，且缺失行视为已取消（worker 下一检查点会停止）
        assert store.delete_job(item["id"], force=True) is True
        assert store.get_job(item["id"]) is None
        assert store.is_cancel_requested(item["id"]) is True
    finally:
        _cleanup(store, path)


def test_missing_job_is_cancel_requested() -> None:
    store, path = _new_store()
    try:
        assert store.is_cancel_requested("missing") is True
    finally:
        _cleanup(store, path)



if __name__ == "__main__":
    test_foreign_keys_and_schema_version()
    test_enqueue_requires_queued()
    test_claim_returns_running_with_lease()
    test_illegal_transition_and_missing_job_are_rejected()
    test_manifest_requires_existing_running_job()
    test_save_manifest_finishes_owned_running_job()
    test_cancel_request_blocks_manifest_commit()
    test_set_tags_roundtrip_and_only_done()
    test_set_tags_validates_limits()
    test_cancel_and_requeue_semantics()
    test_stale_job_recovers_until_max_attempts()
    test_active_job_can_be_force_deleted()
    test_missing_job_is_cancel_requested()
    test_two_processes_claim_once()
    print("OK QueueStore consistency tests passed")


def _windows_short_alias(path: Path) -> Path:
    if os.name != "nt":
        pytest.skip("真实 8.3 路径测试仅在 Windows 上运行")
    canonical = path.resolve(strict=True)
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    function.restype = wintypes.DWORD
    needed = function(str(canonical), None, 0)
    if not needed:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(needed)
    if not function(str(canonical), buffer, needed):
        raise ctypes.WinError(ctypes.get_last_error())
    short = Path(buffer.value)
    if str(short).casefold() == str(canonical).casefold():
        pytest.skip("当前 Windows 卷未为测试目录提供 8.3 短路径别名")
    assert short.exists() and short.resolve(strict=True) == canonical
    return short


@pytest.mark.parametrize("short_in_job", [False, True])
def test_real_windows_short_source_matches_manifest_and_preserves_guards(tmp_path, short_in_job):
    directory = tmp_path / "Manifest path equivalence regression"
    directory.mkdir()
    source = directory / "source.wav"
    source.write_bytes(b"source")
    short_source = _windows_short_alias(directory) / source.name
    outside = tmp_path / "outside" / source.name
    outside.parent.mkdir()
    outside.write_bytes(b"outside")
    job_path, manifest_path = (short_source, source) if short_in_job else (source, short_source)
    store = queue_store.QueueStore()
    try:
        job = {**_job("alias"), "source_path": str(job_path)}
        store.enqueue(job)
        store.claim_next("owner")
        manifest = {**_manifest("alias"), "source_path": str(manifest_path)}
        with pytest.raises(queue_store.QueueStateError, match="租约不匹配"):
            store.save_manifest(manifest, worker_id="other")
        with pytest.raises(queue_store.QueueStateError, match="source_path"):
            store.save_manifest({**manifest, "source_path": str(outside)}, worker_id="owner")
        assert store.get_manifest("alias") is None
        assert store.get_job("alias")["status"] == "running"
        store.save_manifest(manifest, worker_id="owner")
        assert store.get_job("alias")["status"] == "done"
        store.replace_manifest({**manifest, "source_path": str(job_path), "source_title": "再次整理"})
        assert store.get_manifest("alias")["source_title"] == "再次整理"
        with pytest.raises(queue_store.QueueStateError, match="source_path"):
            store.replace_manifest({**manifest, "source_path": str(outside)})
        cancel_job = {**_job("cancel-alias"), "source_path": str(job_path)}
        store.enqueue(cancel_job)
        store.claim_next("owner")
        store.request_cancel("cancel-alias")
        with pytest.raises(queue_store.CancellationRequestedError):
            store.save_manifest({**manifest, "job_id": "cancel-alias"}, worker_id="owner")
        assert store.get_manifest("cancel-alias") is None
        assert store.get_job("cancel-alias")["status"] == "running"
    finally:
        store.close()


@pytest.mark.parametrize("error", [OSError("denied"), RuntimeError("loop"), ValueError("invalid")])
def test_manifest_source_resolution_errors_fail_closed(monkeypatch, error):
    store = queue_store.QueueStore()
    try:
        store.enqueue(_job("unresolved"))
        store.claim_next("owner")
        manifest = {**_manifest("unresolved"), "source_path": "C:/other/sample.mp4"}
        with monkeypatch.context() as patch:
            def fail_resolve(*_args, **_kwargs):
                raise error
            patch.setattr(queue_store.Path, "resolve", fail_resolve)
            with pytest.raises(queue_store.QueueStateError, match="source_path"):
                store.save_manifest(manifest, worker_id="owner")
        assert store.get_manifest("unresolved") is None
        assert store.get_job("unresolved")["status"] == "running"
    finally:
        store.close()
