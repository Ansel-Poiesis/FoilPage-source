# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""run_engine 落盘 segments 时间轴的测试（字幕导出数据源）。"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from app import run_engine


def _result_with_segments() -> dict:
    return {
        "text": "你好世界",
        "language": "zh",
        "language_probability": 0.99,
        "duration_sec": 2.0,
        "avg_logprob": -0.1,
        "no_speech_prob": 0.01,
        "backend": "local",
        "model": "tiny",
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "你好"},
            {"start": 1.0, "end": 2.0, "text": "世界"},
        ],
    }


def test_run_engine_writes_segments_sidecar(tmp_path) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    out_dir = tmp_path / "out"

    with mock.patch.object(
        run_engine,
        "_transcribe_with_device_fallback",
        return_value=(_result_with_segments(), "cpu"),
    ) as transcribe:
        manifest = run_engine.run(
            str(source),
            str(out_dir),
            model="tiny",
            device="cpu",
            language="zh",
            model_dir="",
        )

    assert transcribe.call_count == 1
    segments_path = Path(manifest["segments_path"])
    assert segments_path.is_file()
    rows = json.loads(segments_path.read_text(encoding="utf-8"))
    assert rows == [
        {"start": 0.0, "end": 1.0, "text": "你好"},
        {"start": 1.0, "end": 2.0, "text": "世界"},
    ]
    assert Path(manifest["raw_path"]).is_file()
    assert Path(manifest["final_path"]).is_file()


def test_run_engine_omits_segments_when_absent(tmp_path) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    out_dir = tmp_path / "out2"
    result = _result_with_segments()
    result.pop("segments")

    with mock.patch.object(
        run_engine,
        "_transcribe_with_device_fallback",
        return_value=(result, "cpu"),
    ):
        manifest = run_engine.run(
            str(source),
            str(out_dir),
            model="tiny",
            device="cpu",
            language="zh",
            model_dir="",
        )

    assert "segments_path" not in manifest
    assert not list((out_dir / manifest["job_id"]).glob("*_segments.json"))
