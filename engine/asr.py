# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""ASR 转写（冻结项 S4 / §3.1 / §3.2）。

关键改动（相对旧 video_to_article.transcribe_audio）：
1. `transcribe_audio` 返回 `TranscriptionResult`（类型化契约），不再只返 str。
2. `load_whisper_model` 增加 `model_dir` 参数（下载/缓存根，回应 P0-GPU）。
3. CUDA 加载失败由调用方决定回退；本模块只抛 `EngineError`。
4. 重依赖（faster_whisper）惰性 import，便于无 GPU/无模型环境也能 import 本包做 Spike。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Callable, List, Optional, Tuple

from .contracts import Device, EngineError, TranscriptionResult

# ---------------------------------------------------------------------------
# 无效转写检测（1.3.12）：拦截"非空但无效"的转写，避免幻听文本白白进入
# LLM 整理浪费积分。阈值全部保守设定——宁可放行可疑结果，也不误杀真实
# 语音；误放行的代价只是一次低质量整理，误杀的代价是用户任务直接失败。
# ---------------------------------------------------------------------------
# ≥10s 的音频有效字符（字母/数字/汉字）不足 4 个：视为没有可整理的对白。
_SHORT_CLIP_MIN_DURATION = 10.0
_SHORT_CLIP_MIN_CHARS = 4
# ≥60s 的长音频按密度复核：低于 max(30, 0.05 字/秒) 判无语音。
# 正常语速 3-5 字/秒，1 字/20 秒意味着几乎全程无声。
_DENSITY_MIN_DURATION = 60.0
_DENSITY_CHARS_PER_SEC = 0.05
_DENSITY_MIN_ABS_CHARS = 30
# 幻听循环（纯音乐/无人声常触发 whisper 重复同一句）：≥8 条有效段中同一行
# 占 ≥60%，且置信度差（均值 logprob < -0.8 或段均 no_speech > 0.6）才判。
# 置信度联防是为了放过真实的高重复语音（口号/跟读/咒语式内容）。
_LOOP_MIN_LINES = 8
_LOOP_MIN_LINE_CHARS = 5
_LOOP_TOP_LINE_RATIO = 0.6
_LOOP_BAD_LOGPROB = -0.8
_LOOP_BAD_NO_SPEECH = 0.6
# 置信度极差：均值 logprob < -1.2 或段均 no_speech > 0.85，判疑似非语音。
_NOISE_LOGPROB = -1.2
_NOISE_NO_SPEECH = 0.85
# 非阻断警告线：到达此线但未过硬门的结果，落 manifest 低置信度标记。
_LOW_CONFIDENCE_LOGPROB = -0.9
_LOW_CONFIDENCE_NO_SPEECH = 0.6


def _effective_char_count(text: str) -> int:
    """字母/数字/汉字计数（剔除标点与空白），用于过短与密度判定。"""
    return sum(1 for ch in text if ch.isalnum())


def _normalize_line(text: str) -> str:
    """折叠重复循环判定用的行：小写、去标点空白。"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text.lower())


def low_confidence_warning(
    avg_logprob: Optional[float], no_speech_prob: Optional[float]
) -> bool:
    """转写是否值得低置信度警示（非阻断，仅落 manifest 标记）。"""
    if avg_logprob is not None and avg_logprob < _LOW_CONFIDENCE_LOGPROB:
        return True
    return no_speech_prob is not None and no_speech_prob > _LOW_CONFIDENCE_NO_SPEECH


def _detect_invalid_transcription(
    text: str,
    segment_texts: List[str],
    duration_sec: float,
    avg_logprob: Optional[float],
    no_speech_mean: Optional[float],
) -> Optional[Tuple[str, str]]:
    """检测"非空但无效"的转写；返回 (EngineError code, message) 或 None。

    只做保守硬判：过短/密度过低（no_speech）、幻听循环
    （repetitive_hallucination）、疑似非语音乱码（suspected_noise）。
    """
    effective = _effective_char_count(text)
    if duration_sec >= _SHORT_CLIP_MIN_DURATION and effective < _SHORT_CLIP_MIN_CHARS:
        return (
            "no_speech",
            f"音频约 {int(duration_sec)} 秒但仅识别到 {effective} 个有效字符，"
            "可能没有可整理的对白；可尝试更换更大的转写模型或指定源语言后重试",
        )
    if duration_sec >= _DENSITY_MIN_DURATION:
        threshold = max(
            _DENSITY_MIN_ABS_CHARS, duration_sec * _DENSITY_CHARS_PER_SEC
        )
        if effective < threshold:
            return (
                "no_speech",
                f"音频约 {int(duration_sec // 60)} 分钟但仅识别到 {effective} 个字符，"
                "可能为纯音乐或无对白内容；可尝试更换更大的转写模型或指定源语言后重试",
            )
    poor_confidence = (
        (avg_logprob is not None and avg_logprob < _LOOP_BAD_LOGPROB)
        or (no_speech_mean is not None and no_speech_mean > _LOOP_BAD_NO_SPEECH)
    )
    lines = [
        normalized
        for normalized in (
            _normalize_line(seg_text) for seg_text in segment_texts if seg_text.strip()
        )
        if _effective_char_count(normalized) >= _LOOP_MIN_LINE_CHARS
    ]
    if poor_confidence and len(lines) >= _LOOP_MIN_LINES:
        top_count = Counter(lines).most_common(1)[0][1]
        if top_count / len(lines) >= _LOOP_TOP_LINE_RATIO:
            return (
                "repetitive_hallucination",
                "识别结果高度重复且置信度低，疑似幻听循环（常见于纯音乐或"
                "无人声片段）；可尝试指定源语言或更换转写模型后重试",
            )
    if (avg_logprob is not None and avg_logprob < _NOISE_LOGPROB) or (
        no_speech_mean is not None and no_speech_mean > _NOISE_NO_SPEECH
    ):
        return (
            "suspected_noise",
            "识别置信度极低，疑似非语音内容（如纯音乐/环境噪声）；"
            "可尝试更换更大的转写模型或检查视频音轨",
        )
    return None



def load_whisper_model(
    model_name: str,
    device: Device,
    model_dir: Optional[str] = None,
) -> Tuple[object, Device]:
    """加载 faster-whisper 模型；失败抛 EngineError。

    模型只使用本地缓存（local_files_only），未下载时不静默联网拉取，
    由用户在「设置 → 模型」主动选择下载。
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise EngineError("model_load_failed", f"faster-whisper 未安装: {e}")

    # Use the same local snapshot as the model inventory. This also preserves
    # legacy HF_HOME/hub caches after configuring an explicit download root.
    from .model_manager import ModelManager, MODELS
    local_model = model_name
    if model_name in {spec.name for spec in MODELS}:
        try:
            state = ModelManager(model_dir).state(model_name)
        except Exception as exc:
            raise EngineError("model_load_failed", "无法读取本地模型缓存，请重新检查") from exc
        if not state.verified:
            raise EngineError("model_missing", "模型尚未就绪：请到「设置 → 模型」下载或修复（" + (state.verification_error or "缓存不完整") + "）")
        local_model = state.path

    failures: list[str] = []
    for compute_type in _compute_type_candidates(device):
        try:
            model = WhisperModel(
                local_model,
                device=device,
                compute_type=compute_type,
                download_root=model_dir,
                local_files_only=True,
            )
            return model, device
        except Exception as exc:
            failures.append(f"{compute_type}: {exc}")
    detail = "; ".join(failures)
    if _is_missing_model_error(detail):
        raise EngineError(
            "model_missing",
            "模型尚未下载：请到「设置 → 模型」页下载后再转写",
        )
    if device == "cuda":
        raise EngineError("cuda_unavailable", f"CUDA 模型加载失败: {detail}")
    raise EngineError("model_load_failed", f"模型加载失败: {detail}")


def _is_missing_model_error(detail: str) -> bool:
    """识别 local_files_only 下模型未下载的常见报错，转成可操作提示。"""
    markers = (
        "not found",
        "could not find",
        "no such file",
        "local_files_only",
        "missing",
        "无法找到",
        "未找到",
    )
    lowered = detail.lower()
    return any(marker in lowered for marker in markers)


def _compute_type_candidates(device: Device) -> tuple[str, ...]:
    """Prefer full FP16 on CUDA, then reduce VRAM before falling back to CPU."""
    if device == "cpu":
        return ("int8",)
    preferred = ("float16", "int8_float16")
    try:
        import ctranslate2

        supported = ctranslate2.get_supported_compute_types("cuda")
    except Exception:
        return preferred
    selected = tuple(value for value in preferred if value in supported)
    return selected or preferred


def probe_cuda(cuda_dll_dir: Optional[str] = None) -> dict[str, object]:
    """Return a serializable CUDA capability report without loading a model."""
    from . import dll

    added_dirs = dll.add_runtime_dll_dirs(cuda_dll_dir)
    try:
        import ctranslate2

        device_count = int(ctranslate2.get_cuda_device_count())
        compute_types = sorted(ctranslate2.get_supported_compute_types("cuda"))
        return {
            "available": device_count > 0 and "float16" in compute_types,
            "device_count": device_count,
            "compute_types": compute_types,
            "dll_dirs": added_dirs,
            "error": "" if device_count > 0 else "未检测到 NVIDIA CUDA 设备",
        }
    except Exception as exc:
        return {
            "available": False,
            "device_count": 0,
            "compute_types": [],
            "dll_dirs": added_dirs,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _clean_segment_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).strip()


def transcribe_audio(
    audio_path: str,
    model,
    model_name: str = "unknown",
    language: str = "zh",
    cancel_check: Callable[[], bool] | None = None,
    progress: Callable[[float], None] | None = None,
    initial_prompt: Optional[str] = None,
    hotwords: str = "",
    use_batched: bool = False,
    batch_size: int = 8,
) -> TranscriptionResult:
    """用更快 whisper 转写音频，返回 `TranscriptionResult`。

    `model_name` 由调用方显式传入：faster-whisper 的 `WhisperModel` 实例
    并不暴露 `model_size_or_path`，若从模型对象反查会恒为 "unknown"。

    `initial_prompt` 为 None 时使用内置中文默认提示（仅标点引导，不含
    话题偏向）；传空字符串表示完全不用提示。`hotwords` 用于提升专有名词
    （人名、产品名）识别率。`use_batched` 启用 faster-whisper 的批量推理
    （GPU 上常见 2-4 倍提速）；批量在任何分段产出前失败时自动退回逐段
    推理，已产出分段后的失败不重跑，避免进度回退和重复计费时长。
    """
    if initial_prompt is None and language == "zh":
        initial_prompt = "以下是简体中文的语音内容，包含正确的标点符号。"
    language_arg = None if language == "auto" else language

    transcribe_kwargs = {
        "language": language_arg,
        "beam_size": 5,
        "vad_filter": True,
        "vad_parameters": dict(min_silence_duration_ms=500),
        "initial_prompt": initial_prompt or None,
    }
    if hotwords.strip():
        transcribe_kwargs["hotwords"] = hotwords.strip()

    runner = model
    if use_batched:
        try:
            from faster_whisper import BatchedInferencePipeline
            runner = BatchedInferencePipeline(model=model)
        except Exception:
            runner = model
        else:
            transcribe_kwargs["batch_size"] = max(1, int(batch_size))

    consumed = 0

    def _consume(consumer) -> tuple[list, object]:
        nonlocal consumed
        segs, meta = consumer.transcribe(audio_path, **transcribe_kwargs)
        duration = float(getattr(meta, "duration", 0.0) or 0.0)
        out = []
        for segment in segs:
            if cancel_check is not None and cancel_check():
                raise EngineError("cancelled", "作业已取消")
            out.append(segment)
            consumed += 1
            if progress is not None and duration > 0:
                segment_end = float(getattr(segment, "end", 0.0) or 0.0)
                progress(min(1.0, max(0.0, segment_end / duration)))
        return out, meta

    if cancel_check is not None and cancel_check():
        raise EngineError("cancelled", "作业已取消")
    try:
        seg_list, info = _consume(runner)
    except EngineError:
        raise
    except Exception as exc:
        if not use_batched or consumed > 0:
            raise
        print(
            f"[ASR] 批量推理不可用，退回逐段推理: {type(exc).__name__}: {exc}",
            flush=True,
        )
        transcribe_kwargs.pop("batch_size", None)
        consumed = 0
        seg_list, info = _consume(model)
    if cancel_check is not None and cancel_check():
        raise EngineError("cancelled", "作业已取消")
    if progress is not None:
        progress(1.0)

    # 中文/日文/韩文不补空格；其余语言按词用空格拼接并折叠多余空白。
    lang = info.language or ""
    texts: List[str] = [seg.text.strip() for seg in seg_list]
    if lang in ("zh", "ja", "ko"):
        joined_text = "".join(texts)
    else:
        joined_text = re.sub(r"\s+", " ", " ".join(texts)).strip()

    if not joined_text.strip():
        raise EngineError(
            "no_speech",
            "未识别到有效语音文本；可尝试更换更大的转写模型或指定源语言后重试",
        )

    # 聚合逐段置信度（faster-whisper Segment 携带 avg_logprob / no_speech_prob）。
    logprobs = [s.avg_logprob for s in seg_list if getattr(s, "avg_logprob", None) is not None]
    no_speech = [s.no_speech_prob for s in seg_list if getattr(s, "no_speech_prob", None) is not None]
    avg_logprob = sum(logprobs) / len(logprobs) if logprobs else None
    no_speech_prob = max(no_speech) if no_speech else None
    no_speech_prob_mean = sum(no_speech) / len(no_speech) if no_speech else None
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    invalid = _detect_invalid_transcription(
        joined_text,
        texts,
        duration,
        avg_logprob,
        no_speech_prob_mean,
    )
    if invalid is not None:
        raise EngineError(invalid[0], invalid[1])
    # 保留带时间轴的分段，供字幕导出（SRT/VTT）与后续资产化使用。
    segments = [
        {
            "start": max(0.0, float(getattr(seg, "start", 0.0) or 0.0)),
            "end": max(0.0, float(getattr(seg, "end", 0.0) or 0.0)),
            "text": _clean_segment_text(seg.text),
        }
        for seg in seg_list
        if _clean_segment_text(getattr(seg, "text", ""))
    ]

    return TranscriptionResult(
        text=joined_text,
        language=lang,
        language_probability=info.language_probability,
        duration_sec=duration,
        avg_logprob=avg_logprob,
        no_speech_prob=no_speech_prob,
        no_speech_prob_mean=no_speech_prob_mean,
        backend="local",
        model=str(model_name),
        segments=segments,
    )
