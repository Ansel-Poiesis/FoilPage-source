# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Fast Worker/run_engine behavior tests with the ASR backend replaced by fakes."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from app import worker  # noqa: E402
from engine import queue_store  # noqa: E402
from engine.contracts import EngineError  # noqa: E402
import run_engine  # noqa: E402
from account_auth_fixtures import grant_account  # noqa: E402


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_store() -> tuple[queue_store.QueueStore, str]:
    path = str(Path(tempfile.mkdtemp(prefix="worker_account_")) / "queue.db")
    return queue_store.QueueStore(path), path


def _cleanup(store: queue_store.QueueStore, path: str) -> None:
    store.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass
    (Path(path).parent / "device.id").unlink(missing_ok=True)
    Path(path).parent.rmdir()


def _enqueue(
    store: queue_store.QueueStore,
    source: str,
    output: str,
    *,
    model: str = "small",
    language: str = "auto",
    max_attempts: int = 3,
) -> str:
    job_id = uuid.uuid4().hex[:12]
    store.enqueue({
        "id": job_id,
        "source_path": source,
        "output_dir": output,
        "status": "queued",
        "whisper_model": model,
        "device": "cpu",
        "language": language,
        "content_mode": "raw",
        "created_at": _ts(),
        "updated_at": _ts(),
        "max_attempts": max_attempts,
    })
    return job_id


def _manifest(job_id: str, source: str, output: str) -> dict:
    job_dir = Path(output) / job_id
    return {
        "job_id": job_id,
        "source_path": source,
        "source_sha256": "abc",
        "backend": "local",
        "model": "small",
        "language": "en",
        "duration_sec": 1.0,
        "raw_path": str(job_dir / "same_raw.md"),
        "final_path": str(job_dir / "same.md"),
        "content_mode": "raw",
        "prompt_version": "v1",
        "created_at": _ts(),
    }


def _fake_result(text: str = "hello") -> dict:
    return {
        "text": text,
        "language": "en",
        "language_probability": 0.99,
        "duration_sec": 1.0,
        "avg_logprob": -0.1,
        "no_speech_prob": 0.01,
        "backend": "local",
        "model": "tiny",
    }


def test_source_cleanup_only_removes_browser_downloads() -> None:
    with tempfile.TemporaryDirectory(prefix="worker_source_cleanup_") as temp:
        root = Path(temp)
        download_dir = root / "downloads"
        download_dir.mkdir()
        downloaded = download_dir / "browser.mp4"
        imported = root / "manual.mp4"
        downloaded.write_bytes(b"video")
        imported.write_bytes(b"video")
        settings = {"download_dir": str(download_dir), "database_path": ""}

        worker.Worker._delete_downloaded_source(str(downloaded), settings)
        worker.Worker._delete_downloaded_source(str(imported), settings)

        assert not downloaded.exists()
        assert imported.exists()


def test_worker_uses_job_parameters_unless_overridden() -> None:
    store, db_path = _new_store()
    source = "C:/media/same.wav"
    output = tempfile.mkdtemp(prefix="worker_params_")
    try:
        job_id = _enqueue(store, source, output, model="small", language="auto")
        calls: list[tuple] = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return _manifest(job_id, source, output)

        with mock.patch.object(run_engine, "run", side_effect=fake_run):
            assert worker.Worker(store, worker_id="w1", agent_settings=grant_account({}, Path(db_path).parent)).step()
        args, _kwargs = calls[0]
        assert args[2:5] == ("small", "cpu", "auto")

        job_id_2 = _enqueue(store, source, output, model="small", language="auto")
        with mock.patch.object(
            run_engine,
            "run",
            return_value=_manifest(job_id_2, source, output),
        ) as patched:
            worker.Worker(
                store, model="tiny", language="en", worker_id="w2",
                agent_settings=grant_account({}, Path(db_path).parent)
            ).step()
        assert patched.call_args.args[2:5] == ("tiny", "cpu", "en")
    finally:
        _cleanup(store, db_path)


def test_worker_persists_throttled_engine_progress() -> None:
    store, db_path = _new_store()
    output = tempfile.mkdtemp(prefix="worker_progress_")
    source = "C:/media/progress.wav"
    try:
        job_id = _enqueue(store, source, output)

        def fake_run(*_args, progress, **_kwargs):
            for value in (0.2, 0.205, 0.35, 1.0):
                progress(value)
            return _manifest(job_id, source, output)

        with (
            mock.patch.object(run_engine, "run", side_effect=fake_run),
            mock.patch.object(store, "update_progress", wraps=store.update_progress) as update,
        ):
            worker.Worker(store, worker_id="progress-worker", agent_settings=grant_account({}, Path(db_path).parent)).step()
        assert [call.args[1] for call in update.call_args_list] == [0.2, 0.35, 1.0]
        assert store.get_job(job_id)["progress"] == 1.0
    finally:
        _cleanup(store, db_path)


def test_worker_cooperative_cancel() -> None:
    store, db_path = _new_store()
    output = tempfile.mkdtemp(prefix="worker_cancel_")
    source = "C:/media/same.wav"
    started = threading.Event()
    try:
        job_id = _enqueue(store, source, output)

        def fake_run(*_args, cancel_check, **_kwargs):
            started.set()
            while not cancel_check():
                time.sleep(0.01)
            raise EngineError("cancelled", "作业已取消")

        with mock.patch.object(run_engine, "run", side_effect=fake_run):
            instance = worker.Worker(
                store, worker_id="cancel-worker", heartbeat_interval=0.05,
                agent_settings=grant_account({}, Path(db_path).parent)
            )
            thread = threading.Thread(target=instance.step)
            thread.start()
            assert started.wait(2)
            store.request_cancel(job_id)
            thread.join(5)
            assert not thread.is_alive()
        cancelled = store.get_job(job_id)
        assert cancelled and cancelled["status"] == "cancelled"
        assert store.get_manifest(job_id) is None
    finally:
        _cleanup(store, db_path)


def test_cancel_race_before_manifest_is_not_marked_done() -> None:
    store, db_path = _new_store()
    output = tempfile.mkdtemp(prefix="worker_cancel_race_")
    source = "C:/media/same.wav"
    try:
        job_id = _enqueue(store, source, output)

        def fake_run(*_args, **_kwargs):
            store.request_cancel(job_id)
            return _manifest(job_id, source, output)

        with mock.patch.object(run_engine, "run", side_effect=fake_run):
            worker.Worker(store, worker_id="race-worker", agent_settings=grant_account({}, Path(db_path).parent)).step()
        assert store.get_job(job_id)["status"] == "cancelled"
        assert store.get_manifest(job_id) is None
    finally:
        _cleanup(store, db_path)


def test_retryable_error_respects_max_attempts() -> None:
    store, db_path = _new_store()
    output = tempfile.mkdtemp(prefix="worker_retry_")
    try:
        source = "C:/media/same.wav"
        first = _enqueue(store, source, output, max_attempts=2)
        failure = EngineError("model_load_failed", "temporary")
        with mock.patch.object(run_engine, "run", side_effect=failure):
            worker.Worker(store, worker_id="retry-worker", agent_settings=grant_account({}, Path(db_path).parent)).step()
            last = _enqueue(store, source, output, max_attempts=1)
            worker.Worker(store, worker_id="last-worker", agent_settings=grant_account({}, Path(db_path).parent)).step()
        assert store.get_job(first)["status"] == "retryable"
        assert store.get_job(last)["status"] == "failed"
    finally:
        _cleanup(store, db_path)


def test_model_cache_and_same_name_output_isolation() -> None:
    run_engine.clear_model_cache()
    with tempfile.TemporaryDirectory(prefix="engine_runtime_") as temp:
        root = Path(temp)
        source_a = root / "a" / "same.wav"
        source_b = root / "b" / "same.wav"
        source_a.parent.mkdir()
        source_b.parent.mkdir()
        source_a.write_bytes(b"RIFF-a")
        source_b.write_bytes(b"RIFF-b")
        output = root / "out"

        with (
            mock.patch.object(run_engine.dll, "add_runtime_dll_dirs"),
            mock.patch.object(
                run_engine.asr, "load_whisper_model", return_value=(object(), "cpu")
            ) as load,
            mock.patch.object(
                run_engine.asr, "transcribe_audio", return_value=_fake_result()
            ),
            mock.patch.object(
                run_engine.text, "convert_to_simplified", side_effect=lambda value: value
            ),
        ):
            first = run_engine.run(
                str(source_a), str(output), "tiny", "cpu", "en", "", "job-a"
            )
            second = run_engine.run(
                str(source_b), str(output), "tiny", "cpu", "en", "", "job-b"
            )

        assert load.call_count == 1
        assert Path(first["raw_path"]).parent.name == "job-a"
        assert Path(second["raw_path"]).parent.name == "job-b"
        assert first["raw_path"] != second["raw_path"]
        assert Path(first["raw_path"]).read_text(encoding="utf-8") == "hello"
        meta = Path(first["raw_path"]).parent / "same_manifest.json"
        assert json.loads(meta.read_text(encoding="utf-8"))["job_id"] == "job-a"
        assert not any(path.name.startswith(".") for path in output.iterdir())
    run_engine.clear_model_cache()


def test_cuda_load_failure_falls_back_to_cpu_once() -> None:
    run_engine.clear_model_cache()
    with (
        mock.patch.object(
            run_engine,
            "_load_model_cached",
            side_effect=[
                EngineError("cuda_unavailable", "missing cudnn"),
                (object(), "cpu"),
            ],
        ) as load,
        mock.patch.object(
            run_engine.asr, "transcribe_audio", return_value=_fake_result()
        ) as transcribe,
    ):
        result, actual_device = run_engine._transcribe_with_device_fallback(
            "sample.wav", "tiny", "cuda", "zh", "", None
        )
    assert result["text"] == "hello"
    assert actual_device == "cpu"
    assert load.call_args_list[0].args[1] == "cuda"
    assert load.call_args_list[1].args[1] == "cpu"
    assert transcribe.call_count == 1


def test_cuda_inference_failure_falls_back_to_cpu_once() -> None:
    run_engine.clear_model_cache()
    with (
        mock.patch.object(
            run_engine,
            "_load_model_cached",
            side_effect=[(object(), "cuda"), (object(), "cpu")],
        ),
        mock.patch.object(
            run_engine.asr,
            "transcribe_audio",
            side_effect=[RuntimeError("cudnn missing"), _fake_result()],
        ) as transcribe,
    ):
        _result, actual_device = run_engine._transcribe_with_device_fallback(
            "sample.wav", "tiny", "cuda", "zh", "", None
        )
    assert actual_device == "cpu"
    assert transcribe.call_count == 2


def test_cuda_does_not_mask_business_errors() -> None:
    with (
        mock.patch.object(
            run_engine, "_load_model_cached", return_value=(object(), "cuda")
        ),
        mock.patch.object(
            run_engine.asr,
            "transcribe_audio",
            side_effect=EngineError("cancelled", "作业已取消"),
        ),
    ):
        with pytest.raises(EngineError) as raised:
            run_engine._transcribe_with_device_fallback(
                "sample.wav", "tiny", "cuda", "zh", "", None
            )
    assert raised.value.code == "cancelled"




def test_worker_survives_force_deleted_job_during_run() -> None:
    store, db_path = _new_store()
    output = tempfile.mkdtemp(prefix="worker_deleted_")
    source = "C:/media/same.wav"
    started = threading.Event()
    try:
        job_id = _enqueue(store, source, output)

        def fake_run(*_args, cancel_check, **_kwargs):
            started.set()
            # 模拟运行中被用户强制删除：行没了，下一检查点视为取消
            store.delete_job(job_id, force=True)
            while not cancel_check():
                time.sleep(0.01)
            raise EngineError("cancelled", "作业已取消")

        with mock.patch.object(run_engine, "run", side_effect=fake_run):
            instance = worker.Worker(store, worker_id="delete-worker", heartbeat_interval=0.05,
                                     agent_settings=grant_account({}, Path(db_path).parent))
            thread = threading.Thread(target=instance.step)
            thread.start()
            assert started.wait(2)
            thread.join(5)
            # worker 不能崩溃：行已删除时应静默收尾
            assert not thread.is_alive()
        assert store.get_job(job_id) is None
    finally:
        _cleanup(store, db_path)


def test_worker_survives_force_deleted_job_before_manifest() -> None:
    store, db_path = _new_store()
    output = tempfile.mkdtemp(prefix="worker_deleted_manifest_")
    source = "C:/media/same.wav"
    try:
        job_id = _enqueue(store, source, output)

        def fake_run(*_args, **_kwargs):
            # 引擎跑完后行已被删：save_manifest 应被容忍，不抛到外层
            store.delete_job(job_id, force=True)
            return _manifest(job_id, source, output)

        with mock.patch.object(run_engine, "run", side_effect=fake_run):
            worker.Worker(store, worker_id="delete-worker-2", agent_settings=grant_account({}, Path(db_path).parent)).step()
        assert store.get_job(job_id) is None
        assert store.get_manifest(job_id) is None
    finally:
        _cleanup(store, db_path)


if __name__ == "__main__":
    test_worker_uses_job_parameters_unless_overridden()
    test_worker_cooperative_cancel()
    test_worker_survives_force_deleted_job_during_run()
    test_worker_survives_force_deleted_job_before_manifest()
    test_cancel_race_before_manifest_is_not_marked_done()
    test_retryable_error_respects_max_attempts()
    test_model_cache_and_same_name_output_isolation()
    print("OK Worker runtime tests passed")
