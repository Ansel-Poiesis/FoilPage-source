# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Opt-in packaged Worker GPU smoke test for Windows release certification."""
from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from engine.queue_store import QueueStore


ROOT = Path(__file__).resolve().parents[1]
WORKER_EXE = Path(os.environ.get(
    "PACKAGED_WORKER_EXE",
    ROOT / "dist" / "FoilPage-Worker" / "FoilPage-Worker.exe",
))


@pytest.mark.skipif(
    os.environ.get("RUN_PACKAGED_GPU") != "1",
    reason="set RUN_PACKAGED_GPU=1 after building the Worker",
)
def test_packaged_worker_transcribes_on_gpu() -> None:
    assert WORKER_EXE.is_file()
    with tempfile.TemporaryDirectory(prefix="v2a_packaged_gpu_") as temp:
        root = Path(temp)
        store = QueueStore(str(root / "queue.db"))
        now = datetime.now().isoformat(timespec="seconds")
        store.enqueue({
            "id": "gpu-smoke-01",
            "source_path": str(ROOT / "assets" / "sample.mp4"),
            "output_dir": str(root / "output"),
            "status": "queued",
            "whisper_model": "tiny",
            "device": "cuda",
            "language": "en",
            "content_mode": "raw",
            "created_at": now,
            "updated_at": now,
        })
        store.close()

        completed = subprocess.run(
            [str(WORKER_EXE), "--db", str(root / "queue.db"), "--oneshot"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "请求设备=cuda 实际设备=cuda" in completed.stdout

        store = QueueStore(str(root / "queue.db"))
        job = store.get_job("gpu-smoke-01")
        store.close()
        assert job is not None and job["status"] == "done"
        assert Path(job["raw_path"]).is_file()
