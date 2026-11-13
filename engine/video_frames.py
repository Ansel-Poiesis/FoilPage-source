# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Small, bounded video-frame sampler for visual transcript verification.

Frames are extracted locally with the same ffmpeg binary used by the ASR
pipeline and returned as data URLs.  Nothing is written to the project or sent
over the network by this module; the caller decides whether to include the
frames in a multimodal request.
"""
from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from .audio import get_ffmpeg


DEFAULT_FRAME_COUNT = 6
MAX_FRAME_COUNT = 12
MAX_FRAME_BYTES = 2_000_000


def sample_timestamps(
    duration_sec: float,
    segments: Sequence[Mapping[str, object]] | None = None,
    *,
    count: int = DEFAULT_FRAME_COUNT,
) -> list[float]:
    """Return stable timestamps that cover the video without over-sampling.

    When ASR segments are available, choose segment midpoints spread across
    the source.  This puts subtitles in the frame more often than sampling the
    exact beginning/end of a clip, while keeping the request bounded.
    """
    count = max(1, min(int(count), MAX_FRAME_COUNT))
    duration = max(0.0, float(duration_sec or 0.0))
    candidates: list[float] = []
    for segment in segments or ():
        try:
            start = max(0.0, float(segment.get("start", 0.0)))
            end = max(start, float(segment.get("end", start)))
        except (TypeError, ValueError, AttributeError):
            continue
        text = str(segment.get("text") or "").strip()
        if text:
            candidates.append((start + end) / 2.0)
    if candidates:
        if len(candidates) > count:
            step = len(candidates) / count
            candidates = [candidates[min(len(candidates) - 1, int(i * step))] for i in range(count)]
    elif duration > 0:
        # Keep a small margin so a fade-in/fade-out frame is not the only
        # evidence sent to the visual model.
        margin = min(1.0, duration * 0.05)
        span = max(0.0, duration - margin * 2)
        candidates = [margin + span * ((i + 0.5) / count) for i in range(count)]
    else:
        candidates = [0.0]
    result: list[float] = []
    for value in candidates:
        value = max(0.0, min(value, duration if duration > 0 else value))
        if not result or abs(value - result[-1]) > 0.05:
            result.append(round(value, 3))
    return result[:count]


def extract_frame_data_urls(
    source_path: str | Path,
    timestamps: Sequence[float],
    *,
    ffmpeg_path: str = "",
    timeout_sec: float = 20.0,
) -> list[dict[str, object]]:
    """Extract JPEG frames and return ``[{timestamp, data_url}]`` records.

    A single failed timestamp is skipped so a damaged frame or a short clip
    does not discard the whole transcript.  The method is intentionally
    fail-soft because visual review is an enhancement after local ASR.
    """
    source = Path(source_path).expanduser()
    if not source.is_file():
        return []
    try:
        ffmpeg = get_ffmpeg(ffmpeg_path)
    except Exception:
        return []
    frames: list[dict[str, object]] = []
    for raw_timestamp in timestamps[:MAX_FRAME_COUNT]:
        try:
            timestamp = max(0.0, float(raw_timestamp))
        except (TypeError, ValueError):
            continue
        command = [
            ffmpeg,
            "-hide_banner", "-loglevel", "error",
            "-ss", f"{timestamp:.3f}",
            "-i", str(source),
            "-frames:v", "1",
            "-vf", "scale=1280:-2",
            "-q:v", "5",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "pipe:1",
        ]
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(1.0, float(timeout_sec)),
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        data = completed.stdout or b""
        if completed.returncode != 0 or not data or len(data) > MAX_FRAME_BYTES:
            continue
        frames.append({
            "timestamp": timestamp,
            "data_url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"),
        })
    return frames
