# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Playback codec diagnostics and external player discovery."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from .audio import get_ffmpeg

_VIDEO_CODEC_RE = re.compile(r"\bVideo:\s*([^\s,]+)")

_PLAYER_CANDIDATES = (
    Path(r"C:\Program Files\PotPlayer\PotPlayerMini64.exe"),
    Path(r"C:\Program Files\PotPlayer\PotPlayerMini.exe"),
    Path(r"C:\Program Files (x86)\PotPlayer\PotPlayerMini.exe"),
    Path(r"C:\Program Files\VideoLAN\VLC\vlc.exe"),
    Path(r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"),
)


def probe_video_codecs(video_path: str, ffmpeg_path: str = "") -> Tuple[str, ...]:
    """Return lowercased video codecs probed by ffmpeg; empty if probing fails."""
    try:
        from .audio import FFMPEG_PROBE_TIMEOUT_SEC

        exe = get_ffmpeg(ffmpeg_path)
        result = subprocess.run(
            [exe, "-hide_banner", "-i", video_path],
            capture_output=True,
            timeout=FFMPEG_PROBE_TIMEOUT_SEC,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return ()
    if result.returncode not in (0, 1):
        return ()
    stderr = result.stderr.decode("utf-8", errors="replace")
    return tuple(match.group(1).lower() for match in _VIDEO_CODEC_RE.finditer(stderr))


def find_external_media_player() -> Optional[str]:
    """Return the first installed PotPlayer/VLC executable, or None."""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        local / "Programs" / "PotPlayer" / "PotPlayerMini64.exe",
        local / "Programs" / "VideoLAN" / "VLC" / "vlc.exe",
        *_PLAYER_CANDIDATES,
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None
