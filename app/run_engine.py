# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""v1 真实引擎驱动（无头 / CLI，同时供独立 Worker 调用）。

完成一次完整的「识别 → 提取音频 → 转写 → 繁简转换 → 写 raw/final/manifest」
闭环，验证引擎层（engine.*）在打包后可用。

用法:
  python app/run_engine.py --input sample.mp4 --output-dir out --model tiny
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

# 让 app/ 下的入口脚本也能找到项目根目录的 engine 包（直接 python app/run_engine.py 时）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 打包 EXE 控制台编码常为 cp936，中文路径/帮助会乱码；
# 启动早期把 stdout/stderr 设为 UTF-8（P2）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from engine import audio, asr, dll, text
from engine.contracts import EngineError, Manifest, TranscriptionResult
from engine import validate


@functools.lru_cache(maxsize=4)
def _load_model_cached(model: str, device: str, model_dir: str):
    """Keep loaded models for the lifetime of a worker process."""
    return asr.load_whisper_model(model, device, model_dir or None)


def clear_model_cache() -> None:
    """Release cached model references; primarily useful for tests and shutdown."""
    _load_model_cached.cache_clear()


def _transcribe_with_device_fallback(
    wav: str,
    model: str,
    requested_device: str,
    language: str,
    model_dir: str,
    cancel_check: Callable[[], bool] | None,
    progress: Callable[[float], None] | None = None,
    *,
    initial_prompt: str = "",
    hotwords: str = "",
    use_batched: bool = False,
) -> tuple[TranscriptionResult, str]:
    """Run CUDA when requested, retrying once on CPU for CUDA-only failures."""
    # 批量推理只随 CUDA 主路径启用：CPU 回退路径追求内存 footprint 最小。
    try:
        model_obj, actual_device = _load_model_cached(
            model, requested_device, model_dir or ""
        )
        result = asr.transcribe_audio(
            wav, model_obj, model_name=model, language=language,
            cancel_check=cancel_check,
            progress=progress,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            use_batched=use_batched,
        )
        return result, actual_device
    except EngineError as exc:
        if requested_device != "cuda" or exc.code not in (
            "cuda_unavailable", "model_load_failed",
        ):
            raise
        reason = f"{exc.code}: {exc.message}"
    except Exception as exc:
        if requested_device != "cuda":
            raise
        reason = f"{type(exc).__name__}: {exc}"

    print(f"[GPU] CUDA 不可用，自动回退 CPU: {reason}", flush=True)
    # A CUDA model may have loaded before inference failed. Release it before
    # allocating the CPU model so retries do not retain VRAM indefinitely.
    clear_model_cache()
    model_obj, actual_device = _load_model_cached(model, "cpu", model_dir or "")
    result = asr.transcribe_audio(
        wav, model_obj, model_name=model, language=language,
        cancel_check=cancel_check,
        progress=progress,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
    )
    return result, actual_device


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(
    input_path: str,
    output_dir: str,
    model: str,
    device: str,
    language: str,
    model_dir: str,
    job_id: str | None = None,
    *,
    source_title: str = "",
    cancel_check: Callable[[], bool] | None = None,
    progress: Callable[[float], None] | None = None,
    initial_prompt: str = "",
    hotwords: str = "",
    use_batched: bool = False,
) -> Manifest:
    if device not in ("cpu", "cuda"):
        raise ValueError("device 必须是 cpu 或 cuda")

    in_p = Path(input_path)
    out_p = Path(output_dir).expanduser().resolve()
    out_p.mkdir(parents=True, exist_ok=True)
    if not in_p.exists():
        raise EngineError("corrupt_media", f"输入文件不存在: {input_path}")

    job_id = job_id or uuid.uuid4().hex[:12]
    if Path(job_id).name != job_id or job_id in (".", ".."):
        raise ValueError("job_id 必须是单一安全路径段")

    def check_cancelled() -> None:
        if cancel_check is not None and cancel_check():
            raise EngineError("cancelled", "作业已取消")

    def report(value: float) -> None:
        if progress is not None:
            progress(min(1.0, max(0.0, float(value))))

    check_cancelled()
    report(0.01)

    # CUDA 运行库可来自安装包、Python NVIDIA wheels 或用户系统路径。
    dll.add_runtime_dll_dirs()

    # 视频 → 临时目录下的音频；音频直接转写。
    # 临时目录唯一命名，避免 -y 覆盖输出目录下同名 WAV（P0）。
    temp_dir = None
    staging_dir = None
    try:
        # ---- 识别 → 提取音频（视频） ----
        if audio.is_audio_file(str(in_p)):
            wav = str(in_p)
        else:
            temp_dir = tempfile.mkdtemp(prefix="v2a_")
            wav = os.path.join(temp_dir, f"{in_p.stem}.wav")
            audio.extract_audio(str(in_p), wav)
        report(0.12)

        # ---- 转写 ----
        check_cancelled()
        result, actual_device = _transcribe_with_device_fallback(
            wav, model, device, language, model_dir, cancel_check,
            lambda value: report(0.12 + value * 0.78),
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            use_batched=use_batched and device == "cuda",
        )
        print(
            f"[ASR] 请求设备={device} 实际设备={actual_device} 模型={model}",
            flush=True,
        )
        # 闭环校验：ASR 产物落库/回传前必须先过运行时校验（P0）。
        validate.validate_transcription_result(result)
        check_cancelled()
        report(0.92)

        # ---- 繁简 + 落盘 ----
        simplified = text.convert_to_simplified(result["text"])

        stem = _safe_output_stem(source_title) or in_p.stem
        job_out = out_p / job_id
        raw_path = job_out / f"{stem}_raw.md"
        final_path = job_out / f"{stem}.md"
        segments = result.get("segments") or []
        segments_path = job_out / f"{stem}_segments.json"

        manifest: Manifest = {
            "job_id": job_id,
            "source_path": str(in_p),
            "source_sha256": _sha256(str(in_p)),
            "backend": result["backend"],
            "model": result["model"],
            "language": result["language"],
            "duration_sec": result["duration_sec"],
            "raw_path": str(raw_path),
            "final_path": str(final_path),
            "content_mode": "raw",
            "prompt_version": "v1",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if source_title.strip():
            manifest["source_title"] = source_title.strip()
        if segments:
            manifest["segments_path"] = str(segments_path)
        # 转写置信度指标随 manifest 持久化（无效转写检测 1.3.12）：低置信度
        # 只做非阻断警示，硬拦截已在 asr.transcribe_audio 内完成。
        if result.get("avg_logprob") is not None:
            manifest["asr_avg_logprob"] = result["avg_logprob"]
        if result.get("no_speech_prob") is not None:
            manifest["asr_no_speech_prob"] = result["no_speech_prob"]
        if "avg_logprob" in result or "no_speech_prob" in result:
            manifest["asr_low_confidence"] = asr.low_confidence_warning(
                result.get("avg_logprob"), result.get("no_speech_prob")
            )
        validate.validate_manifest(manifest)

        # Write a complete job directory before making it visible.
        staging_dir = tempfile.mkdtemp(prefix=f".{job_id}-", dir=str(out_p))
        staging = Path(staging_dir)
        (staging / raw_path.name).write_text(simplified, encoding="utf-8")
        (staging / final_path.name).write_text(simplified, encoding="utf-8")
        if segments:
            (staging / segments_path.name).write_text(
                json.dumps(segments, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        (staging / f"{stem}_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report(0.98)
        check_cancelled()
        _commit_output_dir(staging, job_out)
        staging_dir = None
        report(1.0)
        return manifest
    finally:
        # 成功 / 失败 / 取消三条路径都清理临时目录（P0）。
        if temp_dir and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        if staging_dir and os.path.isdir(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)


def _commit_output_dir(staging: Path, destination: Path) -> None:
    """Atomically replace one job's output directory, restoring on failure."""
    backup: Path | None = None
    if destination.exists():
        backup = destination.parent / f".{destination.name}-old-{uuid.uuid4().hex}"
        os.replace(destination, backup)
    try:
        os.replace(staging, destination)
    except Exception:
        if backup is not None and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def _safe_output_stem(title: str) -> str:
    """Use a browser title for output names while keeping paths portable."""
    value = " ".join(str(title or "").replace("\x00", "").split()).strip()
    value = value.replace("/", " ").replace("\\", " ")
    value = "".join(" " if char in '<>:"|?*' else char for char in value).strip(" .")
    return value[:100]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Video/audio → text (CPU / NVIDIA GPU)")
    parser.add_argument("--input", required=True, help="视频或音频文件")
    parser.add_argument("--output-dir", default="out", help="产物输出目录")
    parser.add_argument("--model", default="tiny", help="whisper 模型名")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"],
                        help="推理设备；CUDA 失败时自动回退 CPU")
    parser.add_argument("--language", default="zh", help="zh / en / auto ...")
    parser.add_argument("--model-dir", default="", help="模型缓存根目录")
    parser.add_argument("--initial-prompt", default="", help="转写初始提示（空=内置默认）")
    parser.add_argument("--hotwords", default="", help="热词表（空格分隔，提升专有名词识别）")
    parser.add_argument("--no-batched", action="store_true", help="禁用 CUDA 批量推理")
    args = parser.parse_args(argv)

    try:
        manifest = run(
            args.input, args.output_dir, args.model,
            args.device, args.language, args.model_dir,
            initial_prompt=args.initial_prompt,
            hotwords=args.hotwords,
            use_batched=not args.no_batched,
        )
    except EngineError as e:
        print(f"[引擎错误] {e.code}: {e.message}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"[未预期错误] {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    print(f"[完成] 模型={manifest['model']} 语言={manifest['language']} "
          f"时长={manifest['duration_sec']:.1f}s")
    print(f"  raw : {manifest['raw_path']}")
    print(f"  meta: {Path(manifest['raw_path']).parent / (Path(args.input).stem + '_manifest.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
