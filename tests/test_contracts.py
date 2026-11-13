# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""契约自测（冻结项 §3.1 的最小物证）。

不依赖 torch/faster-whisper：只验证 TypedDict 可实例化、序列化、EngineError 可用。
运行：python -m tests.test_contracts  或  python tests/test_contracts.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.contracts import (  # noqa: E402
    EngineError,
    Job,
    JobStatus,
    Manifest,
    TranscriptionResult,
)


def test_transcription_result_roundtrip() -> None:
    r: TranscriptionResult = {
        "text": "这是一段转写。",
        "language": "zh",
        "language_probability": 0.98,
        "duration_sec": 30.0,
        "avg_logprob": -0.4,
        "no_speech_prob": 0.01,
        "backend": "local",
        "model": "medium",
    }
    # 序列化/反序列化保持契约
    back = TranscriptionResult(**json.loads(json.dumps(r)))
    assert back["text"] == r["text"]
    assert back["language"] == "zh"
    print("[OK] TranscriptionResult 往返序列化通过")


def test_job_status_enum() -> None:
    j: Job = {
        "id": "job-1",
        "source_path": "x.mp4",
        "output_dir": "out",
        "status": "queued",
        "whisper_model": "medium",
        "device": "cpu",
        "language": "zh",
        "content_mode": "raw",
        "created_at": "2026-07-22T17:00:00",
        "updated_at": "2026-07-22T17:00:00",
        "error": None,
        "raw_path": None,
        "final_path": None,
    }
    assert j["status"] == "queued"
    # 状态机允许的取值
    allowed: tuple[JobStatus, ...] = ("queued", "running", "cancelled", "retryable", "failed", "done")
    assert j["status"] in allowed
    print("[OK] Job 状态机构造通过")


def test_manifest_schema() -> None:
    m: Manifest = {
        "job_id": "job-1",
        "source_path": "x.mp4",
        "source_sha256": None,
        "backend": "local",
        "model": "medium",
        "language": "zh",
        "duration_sec": 30.0,
        "raw_path": "out/x_raw.md",
        "final_path": "out/x.md",
        "content_mode": "raw",
        "prompt_version": "v1",
        "created_at": "2026-07-22T17:00:00",
    }
    assert m["content_mode"] == "raw"
    print("[OK] Manifest 溯源结构通过")


def test_engine_error() -> None:
    err = EngineError("no_speech", "未识别到有效语音")
    assert err.code == "no_speech"
    assert err.message == "未识别到有效语音"
    assert "no_speech" in str(err)
    print("[OK] EngineError 统一错误类型通过")


def test_engine_error_pickle_roundtrip() -> None:
    """P0：EngineError 必须能跨多进程 pickle 往返（队列 worker 依赖）。"""
    import pickle

    err = EngineError("ffmpeg_missing", "找不到 ffmpeg 可执行文件")
    blob = pickle.dumps(err)
    restored = pickle.loads(blob)
    assert isinstance(restored, EngineError)
    assert restored.code == "ffmpeg_missing"
    assert restored.message == "找不到 ffmpeg 可执行文件"
    assert str(restored) == "[ffmpeg_missing] 找不到 ffmpeg 可执行文件"
    print("[OK] EngineError pickle 往返通过（可跨 worker 进程）")


if __name__ == "__main__":
    test_transcription_result_roundtrip()
    test_job_status_enum()
    test_manifest_schema()
    test_engine_error()
    test_engine_error_pickle_roundtrip()
    print("\n全部契约自测通过 ✅")
