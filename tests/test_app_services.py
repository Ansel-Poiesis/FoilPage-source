# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Controller, model cache and Worker process manager tests."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from pathlib import Path
from typing import Optional
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.controller import AppController  # noqa: E402
from app.process_manager import WorkerProcessManager, worker_command  # noqa: E402
from app.worker import Worker  # noqa: E402
from engine.model_manager import ModelManager, ModelManagerError  # noqa: E402
from engine.settings import SettingsStore  # noqa: E402
from model_cache_fixture import write_model_snapshot


def test_controller_import_enqueue_and_duplicate_guard() -> None:
    with tempfile.TemporaryDirectory(prefix="controller_") as temp:
        root = Path(temp)
        source = root / "sample.wav"
        source.write_bytes(b"RIFF-controller")
        settings = SettingsStore(base_dir=root / "data")
        controller = AppController(settings)
        try:
            media_id = controller.import_files([str(source)])[0].item["id"]
            first = controller.enqueue_media([media_id])
            second = controller.enqueue_media([media_id])
            assert len(first.created) == 1 and not first.skipped
            assert second.skipped == [media_id]
            rows = controller.media_rows()
            assert rows[0]["job_status"] == "queued"
            controller.cancel_jobs(first.created)
            assert controller.queue.get_job(first.created[0])["status"] == "cancelled"
            assert controller.requeue_jobs(first.created) == first.created
        finally:
            controller.close()


def test_controller_keeps_browser_title_for_jobs() -> None:
    with tempfile.TemporaryDirectory(prefix="controller_title_") as temp:
        root = Path(temp)
        source = root / "browser-media-1.mp4"
        source.write_bytes(b"video")
        controller = AppController(SettingsStore(base_dir=root / "data"))
        try:
            media_id = controller.import_files([str(source)], display_names=["真正的标题"])[0].item["id"]
            job_id = controller.enqueue_media([media_id]).created[0]
            job = controller.queue.get_job(job_id)
            assert job and job["source_title"] == "真正的标题"
            assert controller.job_rows()[0]["display_title"] == "真正的标题"
        finally:
            controller.close()


def test_controller_exposes_persisted_transcription_progress() -> None:
    with tempfile.TemporaryDirectory(prefix="controller_progress_") as temp:
        root = Path(temp)
        source = root / "sample.wav"
        source.write_bytes(b"RIFF-controller")
        controller = AppController(SettingsStore(base_dir=root / "data"))
        try:
            media_id = controller.import_files([str(source)])[0].item["id"]
            job_id = controller.enqueue_media([media_id]).created[0]
            controller.queue.claim_next("progress-worker")
            controller.queue.update_progress(job_id, 0.37, "progress-worker")
            task = next(row for row in controller.task_rows() if row["task_id"] == job_id)
            assert task["kind"] == "transcription"
            assert task["progress"] == 0.37
        finally:
            controller.close()


def test_controller_clear_media_library_preserves_queued_work() -> None:
    with tempfile.TemporaryDirectory(prefix="controller_clear_media_") as temp:
        root = Path(temp)
        source = root / "sample.wav"
        source.write_bytes(b"RIFF-controller")
        controller = AppController(SettingsStore(base_dir=root / "data"))
        try:
            media_id = controller.import_files([str(source)])[0].item["id"]
            job_id = controller.enqueue_media([media_id]).created[0]
            assert controller.clear_media_library() == 1
            assert controller.media_rows() == []
            assert controller.queue.get_job(job_id)["status"] == "queued"
            assert source.is_file()
        finally:
            controller.close()


def test_controller_retranscribes_done_job_as_fresh_work() -> None:
    with tempfile.TemporaryDirectory(prefix="controller_retranscribe_") as temp:
        root = Path(temp)
        source = root / "sample.wav"
        source.write_bytes(b"RIFF-controller")
        controller = AppController(SettingsStore(base_dir=root / "data"))
        try:
            media_id = controller.import_files([str(source)])[0].item["id"]
            original_id = controller.enqueue_media([media_id]).created[0]
            claimed = controller.queue.claim_next("test-worker")
            assert claimed and claimed["id"] == original_id
            controller.queue.save_manifest({
                "job_id": original_id,
                "source_path": str(source),
                "source_sha256": "abc",
                "backend": "local",
                "model": "tiny",
                "language": "zh",
                "duration_sec": 1.0,
                "raw_path": str(root / "raw.md"),
                "final_path": str(root / "final.md"),
                "content_mode": "raw",
                "prompt_version": "v1",
                "created_at": "2026-07-24T12:00:00",
            }, worker_id="test-worker")
            result = controller.retranscribe_jobs([original_id])
            assert len(result.created) == 1 and not result.skipped
            fresh = controller.queue.get_job(result.created[0])
            assert fresh and fresh["status"] == "queued"
            assert fresh["id"] != original_id
            assert Path(fresh["source_path"]).resolve() == source.resolve()
        finally:
            controller.close()


def test_controller_retranscribes_failed_job_as_fresh_work() -> None:
    with tempfile.TemporaryDirectory(prefix="controller_retranscribe_failed_") as temp:
        root = Path(temp)
        source = root / "sample.wav"
        source.write_bytes(b"RIFF-controller")
        controller = AppController(SettingsStore(base_dir=root / "data"))
        try:
            media_id = controller.import_files([str(source)])[0].item["id"]
            original_id = controller.enqueue_media([media_id]).created[0]
            claimed = controller.queue.claim_next("test-worker")
            assert claimed and claimed["id"] == original_id
            controller.queue.update_status(
                original_id, "failed", {"code": "no_audio_stream", "message": "no audio"},
                worker_id="test-worker",
            )
            result = controller.retranscribe_jobs([original_id])
            assert len(result.created) == 1 and not result.skipped
            assert controller.queue.get_job(result.created[0])["status"] == "queued"
        finally:
            controller.close()


def test_task_rows_include_browser_failure_without_transcription_job() -> None:
    with tempfile.TemporaryDirectory(prefix="controller_browser_task_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        try:
            item = controller.downloads.create(
                "https://www.bilibili.com/video/BV1task", "浏览器任务", "bilibili",
                {"quality": "best-available", "audio_only": False, "transcribe_after_download": True, "language": "zh"},
            )
            controller.downloads.transition(item["id"], "resolving")
            controller.downloads.transition(item["id"], "failed", error={"code": "network", "message": "连接失败", "retryable": True})
            rows = controller.task_rows()
            assert len(rows) == 1
            assert rows[0]["kind"] == "download"
            assert rows[0]["error"]["message"] == "连接失败"
        finally:
            controller.close()


def test_controller_recovers_and_cancels_browser_tasks() -> None:
    with tempfile.TemporaryDirectory(prefix="controller_browser_actions_") as temp:
        controller = AppController(SettingsStore(base_dir=Path(temp) / "data"))
        try:
            item = controller.downloads.create(
                "https://www.bilibili.com/video/BV1actions", "恢复任务", "bilibili",
                {"quality": "best-available", "audio_only": False, "transcribe_after_download": True, "language": "zh"},
            )
            controller.downloads.transition(item["id"], "resolving")
            controller.downloads.transition(
                item["id"], "failed",
                error={"code": "network", "message": "连接失败", "retryable": True},
            )
            assert controller.retry_download_tasks([item["id"]]) == [item["id"]]
            assert controller.downloads.get(item["id"])["status"] == "received"
            assert controller.cancel_download_tasks([item["id"]]) == [item["id"]]
            assert controller.downloads.get(item["id"])["status"] == "cancelled"
        finally:
            controller.close()


def test_model_manager_detects_and_deletes_verified_cache() -> None:
    with tempfile.TemporaryDirectory(prefix="models_") as temp:
        manager = ModelManager(temp)
        repo = Path(temp) / "hub" / "models--Systran--faster-whisper-tiny"
        snapshot = repo / "snapshots" / "abc"
        snapshot.mkdir(parents=True)
        (repo / "refs").mkdir()
        (repo / "refs" / "main").write_text("abc", encoding="utf-8")
        (snapshot / "config.json").write_text("{}", encoding="utf-8")
        write_model_snapshot(snapshot)
        state = manager.state("tiny")
        assert state.installed and state.verified and state.bytes_on_disk > 0
        assert manager.delete("tiny") is True
        assert manager.state("tiny").installed is False


def test_model_manager_reports_unknown_and_incomplete_cache() -> None:
    with tempfile.TemporaryDirectory(prefix="models_edge_") as temp:
        manager = ModelManager(temp)
        with pytest.raises(ModelManagerError, match="不支持的模型"):
            manager.state("unknown")

        snapshot = (
            Path(temp) / "hub" / "models--Systran--faster-whisper-base"
            / "snapshots" / "incomplete"
        )
        snapshot.mkdir(parents=True)
        (snapshot / "config.json").write_text("{}", encoding="utf-8")
        state = manager.state("base")
        assert state.installed and not state.verified


def test_model_manager_local_files_only_download() -> None:
    with tempfile.TemporaryDirectory(prefix="models_offline_") as temp:
        manager = ModelManager(temp)
        calls = []

        def fake_download(**kwargs):
            calls.append(kwargs)
            snapshot = (
                Path(kwargs["cache_dir"]) / "models--Systran--faster-whisper-tiny"
                / "snapshots" / "offline"
            )
            snapshot.mkdir(parents=True, exist_ok=True)
            (snapshot / "config.json").write_text("{}", encoding="utf-8")
            write_model_snapshot(snapshot)
            return str(snapshot / kwargs["filename"])

        fake_api = SimpleNamespace(
            sha="offline",
            siblings=[
                SimpleNamespace(rfilename="config.json", size=2),
                SimpleNamespace(rfilename="model.bin", size=5),
            ],
        )
        fake_hub = SimpleNamespace(
            HfApi=lambda **_kwargs: SimpleNamespace(
                model_info=lambda *_a, **_k: fake_api
            ),
            hf_hub_download=fake_download,
        )
        with mock.patch.dict(sys.modules, {"huggingface_hub": fake_hub}):
            state = manager.download("tiny", local_files_only=True)
        assert state.verified
        assert len(calls) == 2
        assert all(call["cache_dir"] == str(manager.cache_root) for call in calls)
        assert all(call["local_files_only"] is True for call in calls)
        assert calls[0]["filename"] == "config.json"
        assert calls[1]["filename"] == "model.bin"


def test_model_manager_passes_endpoint_without_mutating_environment() -> None:
    with tempfile.TemporaryDirectory(prefix="models_endpoint_") as temp:
        manager = ModelManager(temp, endpoint="https://mirror.example")
        hub_calls = []

        def fake_download(**kwargs):
            hub_calls.append(kwargs)
            snapshot = (
                Path(kwargs["cache_dir"]) / "models--Systran--faster-whisper-tiny"
                / "snapshots" / "endpoint"
            )
            snapshot.mkdir(parents=True, exist_ok=True)
            (snapshot / "config.json").write_text("{}", encoding="utf-8")
            write_model_snapshot(snapshot)
            return str(snapshot / kwargs["filename"])

        fake_api = SimpleNamespace(
            sha="endpoint",
            siblings=[
                SimpleNamespace(rfilename="config.json", size=2),
                SimpleNamespace(rfilename="model.bin", size=5),
            ],
        )
        api_ctor = mock.Mock(return_value=SimpleNamespace(
            model_info=lambda *_a, **_k: fake_api
        ))
        fake_hub = SimpleNamespace(HfApi=api_ctor, hf_hub_download=fake_download)
        with (
            mock.patch.dict(sys.modules, {"huggingface_hub": fake_hub}),
            mock.patch.dict(os.environ, {"HF_ENDPOINT": "keep-me"}),
        ):
            manager.download("tiny")
            assert os.environ["HF_ENDPOINT"] == "keep-me"
        assert api_ctor.call_args.kwargs == {"endpoint": "https://mirror.example"}
        assert hub_calls[0]["endpoint"] == "https://mirror.example"


def test_model_manager_reports_byte_progress() -> None:
    with tempfile.TemporaryDirectory(prefix="models_progress_") as temp:
        manager = ModelManager(temp)
        events: list[tuple[str, Optional[float]]] = []

        def fake_download(**kwargs):
            snapshot = (
                Path(kwargs["cache_dir"]) / "models--Systran--faster-whisper-tiny"
                / "snapshots" / "progress"
            )
            snapshot.mkdir(parents=True, exist_ok=True)
            if kwargs["filename"] == "config.json":
                (snapshot / "config.json").write_text("{}", encoding="utf-8")
                return str(snapshot / "config.json")
            write_model_snapshot(snapshot)
            return str(snapshot / "model.bin")

        fake_api = SimpleNamespace(
            sha="progress",
            siblings=[
                SimpleNamespace(rfilename="config.json", size=2),
                SimpleNamespace(rfilename="model.bin", size=8),
            ],
        )
        fake_hub = SimpleNamespace(
            HfApi=lambda **_kwargs: SimpleNamespace(
                model_info=lambda *_a, **_k: fake_api
            ),
            hf_hub_download=fake_download,
        )
        with mock.patch.dict(sys.modules, {"huggingface_hub": fake_hub}):
            manager.download("tiny", progress=lambda message, value: events.append((message, value)))
        assert events[0][1] == 0.0
        assert events[-1][1] == 1.0
        progress_values = [value for _, value in events if value is not None]
        assert 0.2 in progress_values and 1.0 in progress_values


def test_worker_installs_sigbreak_handler_when_available() -> None:
    store = mock.Mock()
    instance = Worker(store)
    with mock.patch("app.worker.signal.signal") as register:
        instance._install_signal_handlers()
    installed = [call.args[0] for call in register.call_args_list]
    assert __import__("signal").SIGINT in installed
    assert __import__("signal").SIGTERM in installed
    sigbreak = getattr(__import__("signal"), "SIGBREAK", None)
    if sigbreak is not None:
        assert sigbreak in installed


def test_model_manager_rejects_incomplete_download() -> None:
    with tempfile.TemporaryDirectory(prefix="models_bad_download_") as temp:
        manager = ModelManager(temp)

        def fake_download(**_kwargs):
            snapshot = (
                Path(temp) / "hub" / "models--Systran--faster-whisper-tiny"
                / "snapshots" / "broken"
            )
            snapshot.mkdir(parents=True, exist_ok=True)
            return str(snapshot)

        fake_api = SimpleNamespace(
            sha="broken",
            siblings=[SimpleNamespace(rfilename="config.json", size=2)],
        )
        fake_hub = SimpleNamespace(
            HfApi=lambda **_kwargs: SimpleNamespace(
                model_info=lambda *_a, **_k: fake_api
            ),
            hf_hub_download=fake_download,
        )
        with mock.patch.dict(sys.modules, {"huggingface_hub": fake_hub}):
            with pytest.raises(ModelManagerError, match="缓存不完整"):
                manager.download("tiny", local_files_only=True)


def test_worker_process_manager_streams_logs_and_stops() -> None:
    lines: list[str] = []
    states: list[str] = []
    ready = threading.Event()

    def on_log(line: str) -> None:
        lines.append(line)
        if line == "READY":
            ready.set()

    code = (
        "import time; print('READY', flush=True); "
        "[(time.sleep(0.05)) for _ in iter(int, 1)]"
    )
    manager = WorkerProcessManager(
        [sys.executable, "-c", code],
        log_callback=on_log,
        state_callback=states.append,
        auto_restart=False,
    )
    assert manager.start() is True
    assert ready.wait(3) and manager.running
    assert manager.stop(timeout=1) is True
    for _ in range(30):
        if not manager.running:
            break
        time.sleep(0.05)
    assert "READY" in lines
    assert states[0] == "running" and "stopped" in states


@pytest.mark.skipif(os.name != "nt", reason="SIGBREAK is Windows-specific")
def test_real_worker_handles_sigbreak_gracefully() -> None:
    with tempfile.TemporaryDirectory(prefix="worker_sigbreak_") as temp:
        lines: list[str] = []
        ready = threading.Event()

        def on_log(line: str) -> None:
            lines.append(line)
            if "[worker] 启动" in line:
                ready.set()

        manager = WorkerProcessManager(
            worker_command(ROOT, str(Path(temp) / "queue.db"), poll=0.05),
            log_callback=on_log,
            auto_restart=False,
        )
        assert manager.start()
        assert ready.wait(5)
        assert manager.stop(timeout=3)
        for _ in range(20):
            if any("[worker] 已停止" in line for line in lines):
                break
            time.sleep(0.05)
        assert any("[worker] 已停止" in line for line in lines)


def test_worker_command_passes_runtime_settings_in_source_mode() -> None:
    command = worker_command(
        ROOT, "C:/data/library.db", model_dir="C:/models",
        poll=0.5, heartbeat=1.5, lease_timeout=30.0,
    )
    assert command[:2] == [sys.executable, str(ROOT / "app" / "worker.py")]
    assert command[2:] == [
        "--db", "C:/data/library.db",
        "--poll", "0.5",
        "--heartbeat", "1.5",
        "--lease-timeout", "30.0",
        "--concurrency", "1",
        "--model-dir", "C:/models",
    ]


def test_worker_command_locates_packaged_worker() -> None:
    with tempfile.TemporaryDirectory(prefix="packaged_worker_") as temp:
        app_dir = Path(temp) / "FoilPage-Desktop"
        worker = app_dir / "worker" / "FoilPage-Worker.exe"
        worker.parent.mkdir(parents=True)
        worker.write_bytes(b"exe")
        desktop = app_dir / "FoilPage-Desktop.exe"
        desktop.write_bytes(b"exe")
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "executable", str(desktop)),
        ):
            command = worker_command(ROOT, "C:/data/library.db", model_dir="C:/models")
        assert Path(command[0]).resolve() == worker.resolve()
        assert command[-2:] == ["--model-dir", "C:/models"]


def test_worker_command_still_locates_legacy_packaged_worker() -> None:
    """1.3.8 改名后，旧安装根/开发 dist 里的 VideoToArticle-Worker 布局仍要能找到。"""
    with tempfile.TemporaryDirectory(prefix="packaged_worker_") as temp:
        app_dir = Path(temp) / "VideoToArticle-Desktop"
        worker = app_dir / "worker" / "VideoToArticle-Worker.exe"
        worker.parent.mkdir(parents=True)
        worker.write_bytes(b"exe")
        desktop = app_dir / "VideoToArticle-Desktop.exe"
        desktop.write_bytes(b"exe")
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "executable", str(desktop)),
        ):
            command = worker_command(ROOT, "C:/data/library.db")
        assert Path(command[0]).resolve() == worker.resolve()





def test_controller_delete_purges_output_dir_and_downloaded_source() -> None:
    """删除任务：记录 + 产物文件夹 + 下载目录源文件一并移除；用户自有文件保留。"""
    with tempfile.TemporaryDirectory(prefix="controller_purge_") as temp:
        root = Path(temp)
        controller = AppController(SettingsStore(base_dir=root / "data"))
        try:
            dl = Path(controller.settings["download_dir"])
            dl.mkdir(parents=True, exist_ok=True)
            # 下载目录内的源文件 -> 转写任务 + 产物文件夹
            source = dl / "browser-media-1.mp4"
            source.write_bytes(b"video")
            media_id = controller.import_files([str(source)])[0].item["id"]
            job_id = controller.enqueue_media([media_id]).created[0]
            job = controller.queue.get_job(job_id)
            job_out = Path(job["output_dir"]) / job_id
            job_out.mkdir(parents=True)
            (job_out / "t_raw.md").write_text("raw", encoding="utf-8")
            (job_out / "t.md").write_text("final", encoding="utf-8")
            (job_out / "t_manifest.json").write_text("{}", encoding="utf-8")
            assert controller.delete_tasks([job_id]) == [job_id]
            assert controller.queue.get_job(job_id) is None
            assert not job_out.exists(), "产物文件夹应随记录一并删除"
            assert not source.exists(), "下载目录源文件应一并删除"
            assert Path(job["output_dir"]).exists(), "输出根目录必须保留"
            # 用户自有文件（下载目录外）不得删除
            ext = root / "user-own.mp4"
            ext.write_bytes(b"own")
            media_id2 = controller.import_files([str(ext)])[0].item["id"]
            job2 = controller.enqueue_media([media_id2]).created[0]
            assert controller.delete_tasks([job2]) == [job2]
            assert ext.exists(), "用户自有文件不得删除"
        finally:
            controller.close()


def test_controller_clear_completed_archives_done_jobs_only() -> None:
    """清除完成：done 任务归档（工作台隐藏、文本库保留），未完成任务不受影响。"""
    with tempfile.TemporaryDirectory(prefix="controller_clear_") as temp:
        root = Path(temp)
        controller = AppController(SettingsStore(base_dir=root / "data"))
        try:
            done_src = root / "done.mp4"
            done_src.write_bytes(b"video")
            done_job = controller.enqueue_media(
                [controller.import_files([str(done_src)])[0].item["id"]]
            ).created[0]
            claimed = controller.queue.claim_next("done-worker")
            assert claimed and claimed["id"] == done_job
            controller.queue.save_manifest({
                "job_id": done_job,
                "source_path": str(done_src),
                "source_sha256": "abc",
                "backend": "local",
                "model": "tiny",
                "language": "zh",
                "duration_sec": 1.0,
                "raw_path": str(root / "done_raw.md"),
                "final_path": str(root / "done.md"),
                "content_mode": "raw",
                "prompt_version": "v1",
                "created_at": "2026-08-07T12:00:00",
            }, worker_id="done-worker")
            queued_src = root / "queued.mp4"
            queued_src.write_bytes(b"video")
            queued_job = controller.enqueue_media(
                [controller.import_files([str(queued_src)])[0].item["id"]]
            ).created[0]
            # 工作台可见两条，文本库可见一条
            assert len(controller.task_rows()) == 2
            assert len(controller.task_rows(include_archived=True)) == 2
            # 清除完成
            assert controller.clear_completed() == 1
            assert controller.queue.get_job(done_job)["archived"] is True
            assert controller.queue.get_job(queued_job)["archived"] is False
            # 工作台只剩 queued；文本库保留 done（结果不删）
            ids = [t["task_id"] for t in controller.task_rows()]
            assert ids == [queued_job]
            assert done_job not in ids
            lib_ids = [t["task_id"] for t in controller.task_rows(include_archived=True)]
            assert done_job in lib_ids and queued_job in lib_ids
            # 再次清除：无新增
            assert controller.clear_completed() == 0
        finally:
            controller.close()


def test_controller_delete_texts_preserves_video() -> None:
    """删除文本：仅产物移除，任务记录、产物目录和源视频保留。"""
    with tempfile.TemporaryDirectory(prefix="controller_delete_texts_") as temp:
        root = Path(temp)
        controller = AppController(SettingsStore(base_dir=root / "data"))
        try:
            dl = Path(controller.settings["download_dir"])
            dl.mkdir(parents=True, exist_ok=True)
            source = dl / "browser-media-2.mp4"
            source.write_bytes(b"video")
            media_id = controller.import_files([str(source)])[0].item["id"]
            job_id = controller.enqueue_media([media_id]).created[0]
            job = controller.queue.get_job(job_id)
            job_out = Path(job["output_dir"]) / job_id
            job_out.mkdir(parents=True)
            (job_out / "t_raw.md").write_text("raw", encoding="utf-8")
            (job_out / "t.md").write_text("final", encoding="utf-8")
            controller.queue.claim_next("delete-texts")
            controller.queue.save_manifest({
                "job_id": job_id, "source_path": job["source_path"], "source_sha256": None, "backend": "local",
                "model": "tiny", "language": "en", "duration_sec": 1.0,
                "raw_path": str(job_out / "t_raw.md"), "final_path": str(job_out / "t.md"),
                "content_mode": "raw", "prompt_version": "v1", "created_at": job["created_at"],
            }, worker_id="delete-texts")
            assert controller.delete_texts([job_id]) == [job_id]
            kept = controller.queue.get_job(job_id)
            assert kept is not None and kept["status"] == "done", "任务记录应保留"
            assert kept["raw_path"] is None and kept["final_path"] is None
            assert job_out.is_dir(), "任务产物目录应保留"
            assert not (job_out / "t_raw.md").exists() and not (job_out / "t.md").exists()
            assert source.exists(), "视频源文件应保留（可重新转写）"
            assert Path(job["output_dir"]).exists(), "输出根目录必须保留"
        finally:
            controller.close()


def test_controller_delete_files_preserves_record() -> None:
    """删除文件：产物文件夹与下载目录源视频移除，任务记录保留。"""
    with tempfile.TemporaryDirectory(prefix="controller_delete_files_") as temp:
        root = Path(temp)
        controller = AppController(SettingsStore(base_dir=root / "data"))
        try:
            dl = Path(controller.settings["download_dir"])
            dl.mkdir(parents=True, exist_ok=True)
            source = dl / "browser-media-3.mp4"
            source.write_bytes(b"video")
            media_id = controller.import_files([str(source)])[0].item["id"]
            job_id = controller.enqueue_media([media_id]).created[0]
            job = controller.queue.get_job(job_id)
            job_out = Path(job["output_dir"]) / job_id
            job_out.mkdir(parents=True)
            (job_out / "t_raw.md").write_text("raw", encoding="utf-8")
            (job_out / "t.md").write_text("final", encoding="utf-8")
            assert controller.delete_files([job_id]) == [job_id]
            assert controller.queue.get_job(job_id) is not None, "任务记录应保留"
            assert not job_out.exists(), "文本产物文件夹应删除"
            assert not source.exists(), "下载目录源视频应删除"
        finally:
            controller.close()


def test_controller_delete_download_files_preserves_record() -> None:
    """仅删除下载文件：视频移除，下载记录保留。"""
    with tempfile.TemporaryDirectory(prefix="controller_delete_dl_files_") as temp:
        root = Path(temp)
        controller = AppController(SettingsStore(base_dir=root / "data"))
        try:
            dl_dir = Path(controller.settings["download_dir"])
            dl_dir.mkdir(parents=True, exist_ok=True)
            video = dl_dir / "dl-media.mp4"
            video.write_bytes(b"video")
            dl = controller.downloads.create(
                "https://example.com/video", "下载样例", "generic",
                {"quality": "best-available", "audio_only": False, "transcribe_after_download": True, "language": "zh"},
            )
            controller.downloads.transition(dl["id"], "resolving")
            controller.downloads.transition(dl["id"], "downloading", progress=0.5)
            controller.downloads.transition(
                dl["id"], "imported", progress=1.0, bytes_done=100, bytes_total=100,
                downloaded_path=str(video),
            )
            assert controller.delete_download_files([dl["id"]]) == [dl["id"]]
            assert controller.downloads.get(dl["id"]) is not None, "下载记录应保留"
            assert not video.exists(), "下载视频文件应删除"
        finally:
            controller.close()


def test_controller_force_deletes_active_job_and_linked_download() -> None:
    with tempfile.TemporaryDirectory(prefix="controller_force_delete_") as temp:
        root = Path(temp)
        source = root / "active.mp4"
        source.write_bytes(b"video")
        controller = AppController(SettingsStore(base_dir=root / "data"))
        try:
            media_id = controller.import_files([str(source)])[0].item["id"]
            job_id = controller.enqueue_media([media_id]).created[0]
            controller.queue.claim_next("force-worker")
            # 浏览器下载行关联到该任务
            dl = controller.downloads.create(
                "https://www.bilibili.com/video/BV1force", "关联下载", "bilibili",
                {"quality": "best-available", "audio_only": False, "transcribe_after_download": True, "language": "zh"},
            )
            controller.downloads.transition(dl["id"], "resolving")
            controller.downloads.transition(dl["id"], "downloading", progress=0.6, bytes_done=60)
            controller.downloads.transition(
                dl["id"], "imported", progress=1.0, bytes_done=100, bytes_total=100,
                downloaded_path=str(root / "dl.mp4"), media_id=media_id,
            )
            controller.downloads.transition(dl["id"], "queued", job_id=job_id)
            # 运行中任务 force 删除：任务行 + 关联下载行一并移除
            assert controller.delete_tasks([job_id]) == [job_id]
            assert controller.queue.get_job(job_id) is None
            assert controller.downloads.get(dl["id"]) is None
            # 下载中的下载任务也可直接删除
            dl2 = controller.downloads.create(
                "https://www.bilibili.com/video/BV1force2", "下载中", "bilibili",
                {"quality": "best-available", "audio_only": False, "transcribe_after_download": True, "language": "zh"},
            )
            controller.downloads.claim_next()
            controller.downloads.transition(dl2["id"], "downloading", progress=0.2)
            assert controller.delete_download_tasks([dl2["id"]]) == [dl2["id"]]
            assert controller.downloads.get(dl2["id"]) is None
        finally:
            controller.close()


def test_controller_cache_status_and_cleanup_removes_only_candidates() -> None:
    """缓存清理：只删可再生的临时残留与轮转日志，保留当前日志、普通文件与近期新文件。"""
    with tempfile.TemporaryDirectory(prefix="cache_clean_") as temp:
        root = Path(temp)
        controller = AppController(SettingsStore(base_dir=root / "data"))
        try:
            controller.settings["model_dir"] = str(root / "models")
            # 一个已安装且完整的模型缓存，供 model_bytes 统计
            repo = root / "models" / "hub" / "models--Systran--faster-whisper-tiny"
            snapshot = repo / "snapshots" / "abc"
            snapshot.mkdir(parents=True)
            (repo / "refs").mkdir()
            (repo / "refs" / "main").write_text("abc", encoding="utf-8")
            (snapshot / "config.json").write_text("{}", encoding="utf-8")
            write_model_snapshot(snapshot)

            old = time.time() - 7200
            fake_temp = root / "temp"
            fake_temp.mkdir()
            v2a = fake_temp / "v2a_abandon"
            v2a.mkdir()
            (v2a / "a.bin").write_bytes(b"12345")
            resolve_old = fake_temp / ".resolve-old"
            resolve_old.mkdir()
            (resolve_old / "b.bin").write_bytes(b"678")
            os.utime(v2a, (old, old))
            os.utime(resolve_old, (old, old))

            dl = Path(controller.settings["download_dir"])
            dl.mkdir(parents=True, exist_ok=True)
            direct = dl / ".direct-media-abc"
            direct.mkdir()
            (direct / "c.bin").write_bytes(b"abcd")
            dotdl = dl / ".download-xyz"
            dotdl.mkdir()
            (dotdl / "d.bin").write_bytes(b"123456")
            os.utime(direct, (old, old))
            os.utime(dotdl, (old, old))
            fresh = dl / ".direct-media-fresh"
            fresh.mkdir()
            (fresh / "e.bin").write_bytes(b"fresh")
            normal = dl / "keep.mp4"
            normal.write_bytes(b"user")

            log_dir = controller.settings_store.base_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            rotated = log_dir / "desktop.log.1"
            rotated.write_bytes(b"0123456")
            current = log_dir / "desktop.log"
            current.write_bytes(b"log-current")
            os.utime(rotated, (old, old))

            with mock.patch("tempfile.gettempdir", return_value=str(fake_temp)):
                status = controller.cache_status()
            assert status["ok"] is True
            assert status["cleanable_bytes"] == 5 + 3 + 4 + 6 + 7
            assert status["model_bytes"] > 0

            with mock.patch("tempfile.gettempdir", return_value=str(fake_temp)):
                result = controller.cleanup_cache()
            assert result["ok"] is True
            assert result["freed_bytes"] == 5 + 3 + 4 + 6 + 7
            assert len(result["removed"]) == 5
            assert not v2a.exists() and not direct.exists() and not dotdl.exists()
            assert not rotated.exists()
            assert current.exists() and normal.exists() and fresh.exists()

            with mock.patch("tempfile.gettempdir", return_value=str(fake_temp)):
                again = controller.cleanup_cache()
            assert again["freed_bytes"] == 0 and again["removed"] == []
        finally:
            controller.close()


def test_model_manager_delete_all_removes_installed_and_incomplete_caches() -> None:
    """delete_all：删除全部已安装（含不完整）模型缓存，未安装模型不受影响。"""
    with tempfile.TemporaryDirectory(prefix="models_delete_all_") as temp:
        manager = ModelManager(temp)
        repo = Path(temp) / "hub" / "models--Systran--faster-whisper-tiny"
        snapshot = repo / "snapshots" / "abc"
        snapshot.mkdir(parents=True)
        (repo / "refs").mkdir()
        (repo / "refs" / "main").write_text("abc", encoding="utf-8")
        (snapshot / "config.json").write_text("{}", encoding="utf-8")
        write_model_snapshot(snapshot)
        incomplete = Path(temp) / "hub" / "models--Systran--faster-whisper-base"
        (incomplete / "snapshots" / "incomplete").mkdir(parents=True)
        (incomplete / "snapshots" / "incomplete" / "config.json").write_text(
            "{}", encoding="utf-8"
        )

        assert manager.delete_all() == 2
        assert not repo.exists() and not incomplete.exists()
        assert manager.state("tiny").installed is False
        assert manager.state("base").installed is False
        assert manager.delete_all() == 0


if __name__ == "__main__":
    test_controller_import_enqueue_and_duplicate_guard()
    test_controller_force_deletes_active_job_and_linked_download()
    test_model_manager_detects_and_deletes_verified_cache()
    test_worker_process_manager_streams_logs_and_stops()
    print("OK application service tests passed")


def test_controller_retranscribe_persists_editing_level_override() -> None:
    """文本库重选模式：重新转写可按单任务携带整理力度覆盖（2026-11-02）。"""
    with tempfile.TemporaryDirectory(prefix="controller_relevel_") as temp:
        root = Path(temp)
        source = root / "sample.wav"
        source.write_bytes(b"RIFF-controller")
        controller = AppController(SettingsStore(base_dir=root / "data"))
        try:
            media_id = controller.import_files([str(source)])[0].item["id"]
            original_id = controller.enqueue_media([media_id]).created[0]
            controller.queue.claim_next("test-worker")
            controller.queue.save_manifest({
                "job_id": original_id, "source_path": str(source), "source_sha256": "abc",
                "backend": "local", "model": "tiny", "language": "zh", "duration_sec": 1.0,
                "raw_path": str(root / "raw.md"), "final_path": str(root / "final.md"),
                "content_mode": "raw", "prompt_version": "v1", "created_at": "2026-11-02T12:00:00",
            }, worker_id="test-worker")
            result = controller.retranscribe_jobs([original_id], mode="article", editing_level="structured")
            fresh = controller.queue.get_job(result.created[0])
            assert fresh["agent_level_override"] == "structured"
            assert fresh["body_enabled_override"] is True
            # 默认不带覆盖：沿用全局设置（先把新任务置为终态再重转）
            controller.queue.claim_next("test-worker")
            controller.queue.save_manifest({
                "job_id": fresh["id"], "source_path": str(source), "source_sha256": "abc",
                "backend": "local", "model": "tiny", "language": "zh", "duration_sec": 1.0,
                "raw_path": str(root / "raw2.md"), "final_path": str(root / "final2.md"),
                "content_mode": "raw", "prompt_version": "v1", "created_at": "2026-11-02T12:01:00",
            }, worker_id="test-worker")
            plain = controller.retranscribe_jobs([fresh["id"]], mode="default")
            assert controller.queue.get_job(plain.created[0]).get("agent_level_override") in (None, "")
        finally:
            controller.close()
