# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""engine 包：从 video_to_article.py 抽出的纯函数引擎层（冻结项 §四）。

设计约束：
- 无导入副作用：重依赖（faster_whisper / opencc / imageio_ffmpeg）全部惰性 import。
- 配置注入：路径类参数（ffmpeg_path / cuda_dll_dir / model_dir）由调用方传入，不外置硬编码。
- 类型化返回：见 contracts。
"""
from __future__ import annotations

from .contracts import (
    Backend,
    ContentMode,
    Device,
    DownloadError,
    DownloadJob,
    DownloadOptions,
    DownloadStatus,
    EngineError,
    EngineErrorCode,
    Job,
    JobStatus,
    Manifest,
    StructuredDoc,
    TranscriptionResult,
)
__all__ = [
    "Backend", "ContentMode", "Device", "DownloadError", "DownloadJob",
    "DownloadOptions", "DownloadStatus", "EngineError", "EngineErrorCode",
    "Job", "JobStatus", "Manifest", "StructuredDoc", "TranscriptionResult",
]
