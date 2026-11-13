# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from pathlib import Path
from unittest import mock

from engine.video_frames import extract_frame_data_urls, sample_timestamps


def test_sample_timestamps_spreads_across_segment_midpoints() -> None:
    rows = [
        {"start": 0.0, "end": 1.0, "text": "第一句"},
        {"start": 4.0, "end": 5.0, "text": "第二句"},
        {"start": 9.0, "end": 10.0, "text": "第三句"},
    ]
    values = sample_timestamps(12.0, rows, count=2)
    assert len(values) == 2
    assert values[0] == 0.5
    assert values[1] == 4.5


def test_extract_frame_data_urls_is_bounded_and_fail_soft(tmp_path: Path) -> None:
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"placeholder")
    completed = mock.Mock(returncode=0, stdout=b"jpeg-bytes", stderr=b"")
    with mock.patch("engine.video_frames.get_ffmpeg", return_value="ffmpeg"), mock.patch(
        "engine.video_frames.subprocess.run", return_value=completed
    ) as run:
        frames = extract_frame_data_urls(source, [0, 1, 2], timeout_sec=1)
    assert len(frames) == 3
    assert frames[0]["timestamp"] == 0.0
    assert str(frames[0]["data_url"]).startswith("data:image/jpeg;base64,")
    assert run.call_count == 3
