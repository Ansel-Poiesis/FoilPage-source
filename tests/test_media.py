# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Playback codec probing and external player discovery tests."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import audio  # noqa: E402
from engine import media  # noqa: E402


def test_probe_video_codecs_parses_ffmpeg_output() -> None:
    stderr = (
        "  Stream #0:0[0x1](und): Video: hevc (Main) (hev1 / 0x31766568), "
        "yuv420p(tv, bt709/bt709/bt709, progressive), 1280x720, 864 kb/s, 25 fps\n"
        "  Stream #0:1[0x2](und): Audio: aac (HE-AACv2), 48000 Hz, stereo\n"
    )
    probe = subprocess.CompletedProcess(["ffmpeg"], 1, stdout=b"", stderr=stderr.encode())
    with mock.patch.object(media, "get_ffmpeg", return_value="ffmpeg"), mock.patch.object(
        media.subprocess, "run", return_value=probe
    ) as run:
        assert media.probe_video_codecs("sample.mp4") == ("hevc",)
    run.assert_called_once()
    assert run.call_args.kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_probe_video_codecs_returns_empty_on_failure() -> None:
    probe = subprocess.CompletedProcess(["ffmpeg"], 127, stdout=b"", stderr=b"boom")
    with mock.patch.object(media, "get_ffmpeg", return_value="ffmpeg"), mock.patch.object(
        media.subprocess, "run", return_value=probe
    ):
        assert media.probe_video_codecs("missing.mp4") == ()

    with mock.patch.object(media, "get_ffmpeg", side_effect=RuntimeError("no ffmpeg")):
        assert media.probe_video_codecs("missing.mp4") == ()


def test_find_external_media_player_picks_installed_candidate() -> None:
    with tempfile.TemporaryDirectory(prefix="media_player_") as temp:
        player = Path(temp) / "PotPlayerMini64.exe"
        player.write_bytes(b"player")
        with mock.patch.object(media, "_PLAYER_CANDIDATES", (player,)), mock.patch.dict(
            "os.environ", {"LOCALAPPDATA": str(Path(temp) / "missing")}
        ):
            assert Path(media.find_external_media_player()).resolve() == player.resolve()


def test_get_ffmpeg_reuses_worker_bundled_binary() -> None:
    with tempfile.TemporaryDirectory(prefix="media_ffmpeg_") as temp:
        root = Path(temp)
        exe = root / "VideoToArticle-Desktop.exe"
        worker_internal = root / "worker" / "_internal"
        worker_internal.mkdir(parents=True)
        bundled = worker_internal / "ffmpeg-win-x86_64-v7.1.exe"
        bundled.write_bytes(b"binary")
        with mock.patch.object(audio.sys, "executable", str(exe)):
            assert Path(media.get_ffmpeg()).resolve() == bundled.resolve()
