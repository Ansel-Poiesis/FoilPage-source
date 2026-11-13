# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""运行时数据校验（P1）：TypedDict 仅静态约束，此处补运行时校验。

落库 / 入队 / 跨进程回传前调用，缺失字段、非法取值、不可序列化错误都会被
拦下，避免在 SQLite/worker 阶段才暴露数据契约问题。

设计：
- 校验失败统一抛 `ContractValidationError`（继承 ValueError），便于调用方
  与引擎运行时 `EngineError` 区分处理。
- `EngineError` 必须由 `assert_error_picklable` 确认可 pickle，否则队列
  worker 跨进程传递会失败（即本文件要挡住的 P0 回归）。
"""
from __future__ import annotations

import pickle
from typing import Any

from .contracts import (
    EngineError,
    Job,
    Manifest,
    TranscriptionResult,
)

_JOB_STATUSES: tuple[str, ...] = (
    "queued", "running", "cancelled", "retryable", "failed", "done",
)
_DEVICES: tuple[str, ...] = ("cpu", "cuda")
_CONTENT_MODES: tuple[str, ...] = ("raw", "article", "asset-pack")
_BACKENDS: tuple[str, ...] = ("local", "groq", "openai")

_JOB_REQUIRED = (
    "id", "source_path", "output_dir", "status", "whisper_model",
    "device", "language", "content_mode", "created_at", "updated_at",
)
_JOB_OPTIONAL_DEFAULTS = {
    "progress": 0.0,
    "error": None,
    "raw_path": None,
    "final_path": None,
    "worker_id": None,
    "claimed_at": None,
    "heartbeat_at": None,
    "attempts": 0,
    "max_attempts": 3,
    "cancel_requested": False,
    "result_category": "未分类",
    "tags": [],
    "body_enabled_override": None,
}


_TAG_MAX_CHARS = 24
_TAG_MAX_COUNT = 20


class ContractValidationError(ValueError):
    """数据契约校验失败；message 已带字段路径便于定位。"""


def coerce_tags(value: object) -> list[str]:
    """把任意输入规范化为去重、去空、限长、限量的标签列表。

    空标签被丢弃；单个标签不超过 ``_TAG_MAX_CHARS`` 字，总量不超过
    ``_TAG_MAX_COUNT`` 个。返回新列表，不修改入参。
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ContractValidationError("tags 必须是列表")
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise ContractValidationError("标签必须是字符串")
        tag = " ".join(raw.replace("\x00", "").split()).strip()
        if not tag:
            continue
        if len(tag) > _TAG_MAX_CHARS:
            raise ContractValidationError(f"标签过长（最多 {_TAG_MAX_CHARS} 字）: {tag!r}")
        if tag not in seen:
            seen.add(tag)
            cleaned.append(tag)
    if len(cleaned) > _TAG_MAX_COUNT:
        raise ContractValidationError(f"标签数量过多（最多 {_TAG_MAX_COUNT} 个）")
    return cleaned


def _require(d: dict, fields: tuple[str, ...], where: str) -> None:
    for f in fields:
        # 缺失、None、空字符串都视为不合法（避免空 Job.id/source_path/output_dir 漏过）。
        if f not in d or d[f] is None or d[f] == "":
            raise ContractValidationError(f"{where}.{f} 缺失或为空")


def validate_transcription_result(d: TranscriptionResult) -> None:
    # duration_sec / language_probability 也列入必填，缺失会抛 ContractValidationError
    # 而非裸 KeyError（此前仅 text/language/model/backend 必填）。
    _require(
        d,
        ("text", "language", "model", "backend", "duration_sec", "language_probability"),
        "TranscriptionResult",
    )
    if not isinstance(d["text"], str) or not d["text"].strip():
        raise ContractValidationError("TranscriptionResult.text 必须是非空字符串")
    if not isinstance(d["language"], str) or not d["language"]:
        raise ContractValidationError("TranscriptionResult.language 必须非空")
    if d["backend"] not in _BACKENDS:
        raise ContractValidationError(
            f"TranscriptionResult.backend 非法: {d['backend']!r}（允许 {_BACKENDS}）"
        )
    if not isinstance(d["duration_sec"], (int, float)) or d["duration_sec"] < 0:
        raise ContractValidationError("TranscriptionResult.duration_sec 必须 >= 0")
    _lp = d["language_probability"]
    if not isinstance(_lp, (int, float)) or not (0.0 <= float(_lp) <= 1.0):
        raise ContractValidationError("TranscriptionResult.language_probability 必须在 [0,1]")
    # 可选字段：类型为 float 或 None，且概率值需在 [0,1]。
    for opt_name in ("avg_logprob", "no_speech_prob", "no_speech_prob_mean"):
        v = d.get(opt_name)
        if v is not None and not isinstance(v, (int, float)):
            raise ContractValidationError(
                f"TranscriptionResult.{opt_name} 必须为 float 或 None"
            )
    for _prob_name in ("no_speech_prob", "no_speech_prob_mean"):
        _pv = d.get(_prob_name)
        if _pv is not None and not (0.0 <= float(_pv) <= 1.0):
            raise ContractValidationError(
                f"TranscriptionResult.{_prob_name} 必须在 [0,1]"
            )
    segments = d.get("segments")
    if segments is not None:
        if not isinstance(segments, list):
            raise ContractValidationError("TranscriptionResult.segments 必须是列表")
        for idx, seg in enumerate(segments):
            if not isinstance(seg, dict):
                raise ContractValidationError(
                    f"TranscriptionResult.segments[{idx}] 必须是对象"
                )
            start = seg.get("start")
            end = seg.get("end")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                raise ContractValidationError(
                    f"TranscriptionResult.segments[{idx}].start/end 必须为数值"
                )
            if float(start) < 0 or float(end) < float(start):
                raise ContractValidationError(
                    f"TranscriptionResult.segments[{idx}] 时间轴非法"
                )
            if not isinstance(seg.get("text"), str) or not seg.get("text").strip():
                raise ContractValidationError(
                    f"TranscriptionResult.segments[{idx}].text 必须是非空字符串"
                )


def validate_job(d: Job) -> None:
    _require(d, _JOB_REQUIRED, "Job")
    if d["status"] not in _JOB_STATUSES:
        raise ContractValidationError(f"Job.status 非法: {d['status']!r}")
    if d["device"] not in _DEVICES:
        raise ContractValidationError(f"Job.device 非法: {d['device']!r}")
    if d["content_mode"] not in _CONTENT_MODES:
        raise ContractValidationError(f"Job.content_mode 非法: {d['content_mode']!r}")
    progress = d.get("progress", 0.0)
    if not isinstance(progress, (int, float)) or not 0.0 <= float(progress) <= 1.0:
        raise ContractValidationError("Job.progress 必须在 [0,1]")
    attempts = d.get("attempts", 0)
    max_attempts = d.get("max_attempts", 3)
    if not isinstance(attempts, int) or attempts < 0:
        raise ContractValidationError("Job.attempts 必须是 >= 0 的整数")
    if not isinstance(max_attempts, int) or max_attempts < 1:
        raise ContractValidationError("Job.max_attempts 必须是 >= 1 的整数")
    if attempts > max_attempts:
        raise ContractValidationError("Job.attempts 不得大于 max_attempts")
    if not isinstance(d.get("cancel_requested", False), bool):
        raise ContractValidationError("Job.cancel_requested 必须是 bool")
    if "source_title" in d and not isinstance(d["source_title"], str):
        raise ContractValidationError("Job.source_title 必须是字符串")
    if not isinstance(d.get("result_category", "未分类"), str):
        raise ContractValidationError("Job.result_category 必须是字符串")
    if d.get("tags") is not None:
        coerce_tags(d["tags"])
    override = d.get("body_enabled_override")
    if override is not None and not isinstance(override, bool):
        raise ContractValidationError("Job.body_enabled_override 必须是 bool 或缺省")


def validate_manifest(d: Manifest) -> None:
    _require(
        d,
        ("job_id", "source_path", "backend", "model", "language",
         "duration_sec", "raw_path", "final_path", "content_mode",
         "prompt_version", "created_at"),
        "Manifest",
    )
    if d["content_mode"] not in _CONTENT_MODES:
        raise ContractValidationError(f"Manifest.content_mode 非法: {d['content_mode']!r}")
    if not isinstance(d["duration_sec"], (int, float)) or d["duration_sec"] < 0:
        raise ContractValidationError("Manifest.duration_sec 必须 >= 0")
    if d["backend"] not in _BACKENDS:
        raise ContractValidationError(f"Manifest.backend 非法: {d['backend']!r}")
    for field in ("source_title", "agent_profile", "agent_model", "agent_status", "agent_prompt_version", "segments_path"):
        if field in d and not isinstance(d[field], str):
            raise ContractValidationError(f"Manifest.{field} 必须是字符串")
    if "agent_vision_used" in d and not isinstance(d["agent_vision_used"], bool):
        raise ContractValidationError("Manifest.agent_vision_used 必须是 bool")
    if "agent_fidelity_fallback" in d and not isinstance(d["agent_fidelity_fallback"], bool):
        raise ContractValidationError("Manifest.agent_fidelity_fallback 必须是 bool")
    if "agent_vision_frames" in d and (
        not isinstance(d["agent_vision_frames"], int) or d["agent_vision_frames"] < 0
    ):
        raise ContractValidationError("Manifest.agent_vision_frames 必须是 >= 0 的整数")
    # 无效转写检测（1.3.12）：转写置信度指标随 manifest 持久化。
    for _metric in ("asr_avg_logprob", "asr_no_speech_prob"):
        if _metric in d and d[_metric] is not None and not isinstance(d[_metric], (int, float)):
            raise ContractValidationError(f"Manifest.{_metric} 必须为数值或 None")
    if "asr_no_speech_prob" in d and d["asr_no_speech_prob"] is not None:
        _msp = d["asr_no_speech_prob"]
        if not (0.0 <= float(_msp) <= 1.0):
            raise ContractValidationError("Manifest.asr_no_speech_prob 必须在 [0,1]")
    if "asr_low_confidence" in d and not isinstance(d["asr_low_confidence"], bool):
        raise ContractValidationError("Manifest.asr_low_confidence 必须是 bool")


def assert_error_picklable(e: Exception) -> None:
    """校验错误对象可被 pickle（队列 worker 跨进程传递的前提）。"""
    try:
        restored = pickle.loads(pickle.dumps(e))
    except Exception as exc:  # noqa: BLE001
        raise ContractValidationError(
            f"错误对象不可 pickle: {type(e).__name__}: {exc}"
        ) from exc
    if not isinstance(restored, type(e)):
        raise ContractValidationError(
            f"错误反序列化类型不一致: 期望 {type(e).__name__}，实际 {type(restored).__name__}"
        )


def validate_engine_error(e: EngineError) -> None:
    assert_error_picklable(e)
    if not getattr(e, "code", None):
        raise ContractValidationError("EngineError.code 必须非空")
    if not getattr(e, "message", None):
        raise ContractValidationError("EngineError.message 必须非空")


def coerce_job(d: dict) -> Job:
    """补齐 Job 可选字段默认值（轻量迁移 / 兼容旧数据）。返回新 dict。"""
    out: dict[str, Any] = dict(d)
    for k, v in _JOB_OPTIONAL_DEFAULTS.items():
        out.setdefault(k, v)
    return out  # type: ignore[return-value]
