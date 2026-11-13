# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""数据契约（冻结项 §3.1）：全链路类型化，引擎函数返回这些类型而非裸 str。

对应技术大纲 v2 的冻结要求：
- `transcribe_audio` 必须返回 `TranscriptionResult`（不再只返 str）
- 队列 `Job` / 落位 `Manifest` / LLM 产物 `StructuredDoc` 统一定义
- 统一错误类型 `EngineError`（供队列状态机与 UI 脱敏使用）
"""
from __future__ import annotations

from typing import Literal, NotRequired, Optional, TypedDict

Backend = Literal["local", "groq", "openai"]
Device = Literal["cpu", "cuda"]
ContentMode = Literal["raw", "article", "asset-pack"]
JobStatus = Literal["queued", "running", "cancelled", "retryable", "failed", "done"]
DownloadStatus = Literal[
    "received", "resolving", "downloading", "imported", "queued",
    "transcribing", "awaiting_browser", "done", "failed", "cancelled",
]
EngineErrorCode = Literal[
    "no_speech",
    "suspected_noise",
    "repetitive_hallucination",
    "no_audio_stream",
    "cuda_unavailable",
    "llm_failed",
    "corrupt_media",
    "ffmpeg_missing",
    "model_load_failed",
    "model_missing",
    "cancelled",
]


class TranscriptionResult(TypedDict):
    """ASR 转写产物（取代 video_to_article.transcribe_audio 返回的 str）。"""

    text: str
    language: str
    language_probability: float
    duration_sec: float
    avg_logprob: Optional[float]
    no_speech_prob: Optional[float]
    # 无效转写检测（1.3.12）：逐段 no_speech_prob 的均值，与 max 分开聚合，
    # 幻听/纯音乐门用均值（单个坏段不误杀），max 仅供诊断与警告。
    no_speech_prob_mean: NotRequired[Optional[float]]
    backend: Backend
    model: str
    segments: NotRequired[list["SubtitleSegment"]]


class SubtitleSegment(TypedDict):
    """带时间轴的字幕段（导出 SRT/VTT 的数据源）。"""

    start: float
    end: float
    text: str


class StructuredDoc(TypedDict):
    """LLM 结构化产物（v2 启用）。"""

    title: str
    summary: str
    body: str
    mode: ContentMode


class DownloadOptions(TypedDict):
    quality: Literal["best-available"]
    client_request_id: NotRequired[str]
    audio_only: bool
    transcribe_after_download: bool
    language: str
    mode: NotRequired[Literal["local", "browser"]]
    browser_fallback: NotRequired[bool]
    browser_attempted: NotRequired[list[str]]
    rejected_media: NotRequired[list[str]]
    fallback_stage: NotRequired[Literal["browser", "resolver"]]
    share_body: NotRequired[str]
    share_hashtags: NotRequired[list[str]]
    share_raw: NotRequired[str]
    referer: NotRequired[str]
    user_agent: NotRequired[str]


class DownloadError(TypedDict):
    code: str
    message: str
    retryable: bool
    diagnostic: NotRequired[dict[str, str | int | None]]


class DownloadJob(TypedDict):
    id: str
    url: str
    title: str
    source: Literal["browser"]
    platform_hint: str
    requested_at: str
    updated_at: str
    options: DownloadOptions
    status: DownloadStatus
    progress: float
    bytes_total: NotRequired[Optional[int]]
    bytes_done: int
    downloaded_path: NotRequired[Optional[str]]
    media_id: NotRequired[Optional[str]]
    job_id: NotRequired[Optional[str]]
    error: NotRequired[Optional[DownloadError]]
    cancel_requested: NotRequired[bool]
    attempts: NotRequired[int]
    body_enabled_snapshot: NotRequired[bool]
    title_enabled_snapshot: NotRequired[bool]
    body_status: NotRequired[str]
    title_status: NotRequired[str]
    transcription_label: NotRequired[str]
    transcription_error: NotRequired[bool]


class Job(TypedDict):
    """队列单元；status 供状态机驱动。"""

    id: str
    source_path: str
    output_dir: str
    status: JobStatus
    whisper_model: str
    device: Device
    language: str
    content_mode: ContentMode
    progress: float
    source_title: NotRequired[str]
    result_category: NotRequired[str]
    created_at: str
    updated_at: str
    error: Optional[str]
    raw_path: Optional[str]
    final_path: Optional[str]
    worker_id: NotRequired[Optional[str]]
    claimed_at: NotRequired[Optional[str]]
    heartbeat_at: NotRequired[Optional[str]]
    attempts: NotRequired[int]
    max_attempts: NotRequired[int]
    cancel_requested: NotRequired[bool]
    archived: NotRequired[bool]
    tags: NotRequired[list[str]]
    # TASK-8.24：重新转写时按单任务覆盖 Agent 整理开关；None=沿用全局设置。
    body_enabled_override: NotRequired[Optional[bool]]
    body_enabled_snapshot: NotRequired[bool]
    title_enabled_snapshot: NotRequired[bool]
    body_status: NotRequired[str]
    title_status: NotRequired[str]
    original_source_title: NotRequired[str]
    optimized_source_title: NotRequired[str]
    body_usage_units: NotRequired[int]
    title_usage_units: NotRequired[int]
    body_request_count: NotRequired[int]
    title_request_count: NotRequired[int]
    body_billing_mode: NotRequired[str]
    title_billing_mode: NotRequired[str]


class Manifest(TypedDict):
    """落位溯源（v1 即写）。"""

    job_id: str
    source_path: str
    source_sha256: Optional[str]
    backend: str
    model: str
    language: str
    duration_sec: float
    raw_path: str
    final_path: str
    content_mode: ContentMode
    prompt_version: str
    created_at: str
    source_title: NotRequired[str]
    agent_profile: NotRequired[str]
    body_error_code: NotRequired[str]
    body_error: NotRequired[str]
    title_error_code: NotRequired[str]
    agent_model: NotRequired[str]
    agent_status: NotRequired[str]
    agent_prompt_version: NotRequired[str]
    agent_editing_level: NotRequired[str]
    agent_vision_used: NotRequired[bool]
    agent_vision_frames: NotRequired[int]
    agent_fidelity_fallback: NotRequired[bool]
    segments_path: NotRequired[str]
    agent_tags: NotRequired[list[str]]
    original_source_title: NotRequired[str]
    title_status: NotRequired[str]
    title_skill_version: NotRequired[str]
    title_model: NotRequired[str]
    title_error: NotRequired[str]
    # TASK-8.24 文章翻译（先生 2026-10-25）：方向 zh-en/en-zh/auto。
    translation_status: NotRequired[str]
    translation_direction: NotRequired[str]
    translation_lang: NotRequired[str]
    translation_path: NotRequired[str]
    translation_segments_path: NotRequired[str]
    translation_error: NotRequired[str]
    body_enabled_snapshot: NotRequired[bool]
    title_enabled_snapshot: NotRequired[bool]
    body_status: NotRequired[str]
    optimized_source_title: NotRequired[str]
    body_usage_units: NotRequired[int]
    title_usage_units: NotRequired[int]
    body_request_count: NotRequired[int]
    title_request_count: NotRequired[int]
    body_billing_mode: NotRequired[str]
    title_billing_mode: NotRequired[str]
    # 无效转写检测（1.3.12）：转写置信度指标随 manifest 持久化，供诊断与
    # 低置信度警示；None 表示引擎未提供该指标。
    asr_avg_logprob: NotRequired[Optional[float]]
    asr_no_speech_prob: NotRequired[Optional[float]]
    asr_low_confidence: NotRequired[bool]


class EngineError(Exception):
    """统一错误类型；code 供 UI 做可控提示，message 可脱敏后展示。

    关键（P0）：必须可被 pickle 跨 worker 进程传递。
    Exception 默认用 `self.args` 重建自身，因此这里把 `args` 设为
    `(code, message)`，反序列化时调用 `EngineError(code, message)` 即可
    正常重建；`code`/`message` 也会在 __dict__ 中被还原。`__str__` 负责
    还原人类可读格式。
    """

    def __init__(self, code: EngineErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(code, message)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"
