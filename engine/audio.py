# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""音频提取（冻结项 S3）。复用 video_to_article.extract_audio 的逻辑，改为可 import、惰性加载 ffmpeg。

所有失败统一抛 `EngineError`（ffmpeg_missing / corrupt_media），便于队列状态机
与 UI 做可控、可脱敏的提示，而不是把原生 RuntimeError 泄漏到上层。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .contracts import EngineError

AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".flac", ".webm"}

# ffmpeg 有界执行：挂死的 ffmpeg 不能永久占住 worker 槽位。提取上限默认
# 1 小时（长视频转码确实慢），探测 2 分钟；SHENGJIAN_FFMPEG_TIMEOUT 可调。
FFMPEG_EXTRACT_TIMEOUT_SEC = max(60.0, float(os.environ.get("SHENGJIAN_FFMPEG_TIMEOUT", "3600")))
FFMPEG_PROBE_TIMEOUT_SEC = 120.0
BROWSER_MEDIA_PROBE_TIMEOUT_SEC = 15.0


def get_ffmpeg(ffmpeg_path: str = "") -> str:
    """获取 ffmpeg 路径：优先 settings 指定，其次打包后的同级 ffmpeg.exe，
    再次 imageio-ffmpeg，最后系统 PATH。"""
    if ffmpeg_path and Path(ffmpeg_path).exists():
        return ffmpeg_path
    # 打包后：spec 把 ffmpeg 二进制附带在 EXE 同级目录，优先用这个，
    # 避免依赖 imageio_ffmpeg 在冻结环境下的路径解析。
    try:
        sibling = Path(sys.executable).parent / "ffmpeg.exe"
        if sibling.exists():
            return str(sibling)
    except Exception:
        pass
    # 打包后：worker 子目录随 imageio-ffmpeg 附带二进制，桌面端可直接复用，
    # 避免在冻结环境下重复下载或要求系统 PATH 里有 ffmpeg。
    try:
        root = Path(sys.executable).resolve().parent
        if root.name == "worker":
            root = root.parent
        worker_internal = root / "worker" / "_internal"
        for pattern in ("ffmpeg*.exe", "imageio_ffmpeg/binaries/ffmpeg*.exe"):
            for candidate in sorted(worker_internal.glob(pattern)):
                if candidate.is_file():
                    return str(candidate)
    except Exception:
        pass
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    raise EngineError(
        "ffmpeg_missing",
        "找不到 ffmpeg：请在设置中指定 ffmpeg_path，或安装 imageio-ffmpeg / 系统 ffmpeg",
    )


def validate_media_streams(path: str, ffmpeg_path: str = "") -> None:
    """Check an audio/video stream using the Worker binary available to Desktop.

    The slim Desktop excludes PyAV. Probe container streams without decoding the
    full video; a missing tool or timeout must not masquerade as a bad download.
    """
    try:
        exe = get_ffmpeg(ffmpeg_path)
    except EngineError as exc:
        raise EngineError(exc.code, "本地媒体校验组件缺失，浏览器文件已保留，请检查 FoilPage 安装完整性。") from exc
    try:
        result = subprocess.run(
            [exe, "-nostdin", "-hide_banner", "-v", "error", "-i", str(path),
             "-map", "0:v:0?", "-map", "0:a:0?", "-t", "0", "-c", "copy", "-f", "null", "-"],
            capture_output=True, timeout=BROWSER_MEDIA_PROBE_TIMEOUT_SEC,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise EngineError("media_validation_timeout", "本地媒体校验超时，浏览器文件已保留，请稍后重试接收。") from exc
    except OSError as exc:
        raise EngineError("media_validation_unavailable", "本地媒体校验组件无法启动，浏览器文件已保留。") from exc
    if result.returncode != 0:
        raise EngineError("corrupt_media", "文件没有可读取的音频或视频流。")


def is_audio_file(path: str) -> bool:
    return Path(path).suffix.lower() in AUDIO_EXTS


def extract_audio(video_path: str, audio_path: str, ffmpeg: str = "") -> str:
    """从视频提取 16kHz 单声道 WAV。复用原逻辑，失败统一抛 EngineError。"""
    # 始终经过 get_ffmpeg 校验（即使调用方传入了路径也重新确认其存在）。
    exe = get_ffmpeg(ffmpeg)

    if not Path(video_path).exists():
        raise EngineError("corrupt_media", f"源文件不存在: {video_path}")

    # A valid silent video makes ffmpeg fail with an unhelpful output error.
    # -select_streams is an ffprobe option, so probe with ffmpeg's own map
    # failure instead of relying on a bundled ffprobe binary.
    probe = [
        exe, "-hide_banner", "-v", "error", "-i", video_path,
        "-map", "0:a:0", "-t", "0", "-f", "null", "-",
    ]
    try:
        probe_result = subprocess.run(
            probe, capture_output=True, timeout=FFMPEG_PROBE_TIMEOUT_SEC,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as e:
        raise EngineError("ffmpeg_missing", f"ffmpeg 可执行文件无效: {exe}") from e
    except subprocess.TimeoutExpired as e:
        raise EngineError(
            "media_timeout", f"ffmpeg 探测音轨超时（>{FFMPEG_PROBE_TIMEOUT_SEC:.0f}s），已中止"
        ) from e
    if probe_result.returncode != 0:
        stderr = probe_result.stderr.decode("utf-8", errors="replace")
        if "matches no streams" in stderr or "does not contain any stream" in stderr:
            raise EngineError("no_audio_stream", "视频没有音轨，无法进行语音转写")

    cmd = [
        exe,
        "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-y",
        audio_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=FFMPEG_EXTRACT_TIMEOUT_SEC,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as e:
        raise EngineError("ffmpeg_missing", f"ffmpeg 可执行文件无效: {exe}") from e
    except subprocess.TimeoutExpired as e:
        raise EngineError(
            "media_timeout",
            f"ffmpeg 音频提取超时（>{FFMPEG_EXTRACT_TIMEOUT_SEC:.0f}s），已中止；"
            "可调环境变量 SHENGJIAN_FFMPEG_TIMEOUT 提高上限",
        ) from e

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[:300]
        raise EngineError("corrupt_media", f"ffmpeg 音频提取失败: {stderr}")
    return audio_path
