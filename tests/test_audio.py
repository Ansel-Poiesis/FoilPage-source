# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Audio extraction diagnostics."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import audio  # noqa: E402
from engine.contracts import EngineError  # noqa: E402


def test_extract_audio_reports_missing_audio_track() -> None:
    with tempfile.TemporaryDirectory(prefix="audio_no_track_") as temp:
        source = Path(temp) / "silent.mp4"
        source.write_bytes(b"video")
        probe = subprocess.CompletedProcess(
            ["ffmpeg"], 1, stdout=b"", stderr=b"Stream map '0:a:0' matches no streams."
        )
        with mock.patch.object(audio, "get_ffmpeg", return_value="ffmpeg"), mock.patch.object(
            audio.subprocess, "run", return_value=probe
        ) as run:
            with pytest.raises(EngineError, match="没有音轨") as exc:
                audio.extract_audio(str(source), str(Path(temp) / "audio.wav"))
        assert exc.value.code == "no_audio_stream"
        assert run.call_count == 1
        assert run.call_args.kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_extract_audio_converts_ffmpeg_hang_to_retryable_timeout() -> None:
    """挂死的 ffmpeg 必须在限期内失败并可重试（00-待办 #4），不能永久占住槽位。"""
    with tempfile.TemporaryDirectory(prefix="audio_hang_") as temp:
        source = Path(temp) / "video.mp4"
        source.write_bytes(b"video")
        probe_ok = subprocess.CompletedProcess(["ffmpeg"], 0, stdout=b"", stderr=b"")
        with mock.patch.object(audio, "get_ffmpeg", return_value="ffmpeg"), mock.patch.object(
            audio.subprocess,
            "run",
            side_effect=[probe_ok, subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=3600)],
        ) as run:
            with pytest.raises(EngineError, match="超时") as exc:
                audio.extract_audio(str(source), str(Path(temp) / "audio.wav"))
        assert exc.value.code == "media_timeout"
        assert run.call_args.kwargs.get("timeout") == audio.FFMPEG_EXTRACT_TIMEOUT_SEC
        assert all(call.kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0) for call in run.call_args_list)


def test_probe_audio_track_timeout_raises_media_timeout() -> None:
    with tempfile.TemporaryDirectory(prefix="audio_probe_hang_") as temp:
        source = Path(temp) / "video.mp4"
        source.write_bytes(b"video")
        with mock.patch.object(audio, "get_ffmpeg", return_value="ffmpeg"), mock.patch.object(
            audio.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=120),
        ) as run:
            with pytest.raises(EngineError) as exc:
                audio.extract_audio(str(source), str(Path(temp) / "audio.wav"))
        assert exc.value.code == "media_timeout"
        assert run.call_args.kwargs.get("timeout") == audio.FFMPEG_PROBE_TIMEOUT_SEC
