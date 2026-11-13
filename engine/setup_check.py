# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""首次使用前的环境自检、模型档位推荐与一键配置决策。

纯函数模块：不做网络请求、不建线程，输入由调用方（bridge）采集，
保证无 GPU / 无模型的环境也能完整测试推荐矩阵。
档位表与 engine.model_manager.MODELS 同名同序，由测试守护两表不漂移。
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"

# 磁盘告警线：低于该值时即使最小档也提示空间紧张。
MIN_FREE_BYTES = 2 * 1024**3

TIER_LABELS = {1: "极速", 2: "轻量", 3: "均衡", 4: "进阶", 5: "旗舰"}


@dataclass(frozen=True)
class ModelTier:
    name: str
    tier: int
    # 显存需求含加载余量；int8_float16 约为 float16 的一半（asr 加载失败的回退档）。
    vram_gb_fp16: float
    vram_gb_int8: float
    disk_gb: float
    cpu_ok: bool
    cpu_note: str


MODEL_TIERS: tuple[ModelTier, ...] = (
    ModelTier("tiny", 1, 1.0, 0.6, 0.2, True, "CPU 也能快速出稿"),
    ModelTier("base", 2, 1.0, 0.6, 0.3, True, "CPU 可用，速度一般"),
    ModelTier("small", 3, 2.0, 1.2, 0.7, True, "CPU 可用，长音频较慢"),
    ModelTier("medium", 4, 5.0, 3.0, 2.0, False, "无 GPU 时很慢，不建议"),
    ModelTier("large-v3", 5, 10.0, 5.5, 4.0, False, "无 GPU 时极慢，不建议"),
)

VERDICT_READY = "ready"        # 本机配置满足
VERDICT_USABLE = "usable"      # 仅 CPU 也可用（速度有折扣）
VERDICT_CPU_SLOW = "cpu_slow"  # 无 GPU / 显存不足，实际不可用
VERDICT_NO_DISK = "no_disk"    # 磁盘空间不足
_ELIGIBLE = (VERDICT_READY, VERDICT_USABLE)


def tier_label(tier: int) -> str:
    return TIER_LABELS.get(tier, "档位 " + str(tier))


@dataclass(frozen=True)
class TierVerdict:
    name: str
    tier: int
    tier_label: str
    verdict: str
    detail: str
    recommended: bool


def _gb(value: float) -> str:
    text = f"{value:g}"
    return text + " GB"


def model_verdicts(
    *,
    cuda_available: bool,
    vram_mb: Optional[int],
    disk_free_bytes: Optional[int],
    installed: dict[str, bool],
) -> list[TierVerdict]:
    """按档位表给出每档模型在本机的判定与推荐标记。

    installed 为 {模型名: 已完整安装}；vram_mb / disk_free_bytes 未知时传 None，
    对应维度不设限（宁可乐观展示，也不因探测失败挡住下载）。
    """
    verdicts: list[TierVerdict] = []
    for spec in MODEL_TIERS:
        need_bytes = int(spec.disk_gb * 1024**3)
        if disk_free_bytes is not None and disk_free_bytes < need_bytes:
            verdict, detail = VERDICT_NO_DISK, f"空间不足：约需 {_gb(spec.disk_gb)}"
        elif cuda_available and (vram_mb is None or vram_mb >= spec.vram_gb_int8 * 1024):
            if vram_mb is not None and vram_mb >= spec.vram_gb_fp16 * 1024:
                verdict, detail = VERDICT_READY, "GPU 可直接运行"
            elif vram_mb is not None:
                verdict, detail = VERDICT_READY, f"GPU 以 int8 量化运行（约需 {_gb(spec.vram_gb_int8)} 显存）"
            else:
                verdict, detail = VERDICT_READY, "GPU 可运行（显存未知，按需自动降精度）"
        elif cuda_available:
            # 有 GPU 但显存不够这一档：加载会回退 CPU，按 CPU 档判定。
            if spec.cpu_ok:
                verdict, detail = VERDICT_USABLE, f"显存不足（约需 {_gb(spec.vram_gb_int8)}），回退 CPU；{spec.cpu_note}"
            else:
                verdict, detail = VERDICT_CPU_SLOW, f"显存不足（约需 {_gb(spec.vram_gb_int8)}）且回退 CPU 后极慢，不建议"
        else:
            verdict = VERDICT_USABLE if spec.cpu_ok else VERDICT_CPU_SLOW
            detail = spec.cpu_note
        verdicts.append(
            TierVerdict(spec.name, spec.tier, tier_label(spec.tier), verdict, detail, False)
        )

    recommended = _recommend(verdicts, installed)
    return [
        TierVerdict(v.name, v.tier, v.tier_label, v.verdict, v.detail, v.name == recommended)
        for v in verdicts
    ]


def _recommend(verdicts: list[TierVerdict], installed: dict[str, bool]) -> Optional[str]:
    """推荐规则：优先已安装且可用的最高档；全新安装优先 small，磁盘受限再降档。"""
    eligible = [v for v in verdicts if v.verdict in _ELIGIBLE]
    if not eligible:
        return None
    installed_eligible = [v for v in eligible if installed.get(v.name)]
    if installed_eligible:
        return max(installed_eligible, key=lambda v: v.tier).name
    by_name = {v.name: v for v in eligible}
    if "small" in by_name:
        return "small"
    return min(eligible, key=lambda v: v.tier).name


def check_rows(
    *,
    cuda_probe: dict,
    vram_mb: Optional[int],
    disk_free_bytes: Optional[int],
    cache_root: Path,
    settings: dict,
    installed: dict[str, bool],
) -> list[dict]:
    """环境自检行（准备页第一区块）。status: ok / warn / info。"""
    rows: list[dict] = []
    cuda_ok = bool(cuda_probe.get("available"))
    if cuda_ok:
        count = cuda_probe.get("device_count") or 1
        rows.append({
            "key": "gpu", "label": "GPU / CUDA", "status": "ok",
            "value": f"NVIDIA CUDA（{count} 台）", "hint": "转写可走 GPU，高画质档也可用",
        })
    else:
        rows.append({
            "key": "gpu", "label": "GPU / CUDA", "status": "warn",
            "value": "未检测到可用 CUDA",
            "hint": str(cuda_probe.get("error") or "转写将使用 CPU，建议选择轻量档模型"),
        })
    if vram_mb is not None:
        rows.append({
            "key": "vram", "label": "显存", "status": "info",
            "value": _gb(round(vram_mb / 1024, 1)),
            "hint": "决定可直跑的模型档位；不足时自动降精度或回退 CPU",
        })
    if disk_free_bytes is not None:
        enough = disk_free_bytes >= MIN_FREE_BYTES
        rows.append({
            "key": "disk", "label": "磁盘剩余", "status": "ok" if enough else "warn",
            "value": _gb(round(disk_free_bytes / 1024**3, 1)),
            "hint": "" if enough else "空间紧张：旗舰档约需 4 GB，请先清理或换缓存盘",
        })
    endpoint = str(settings.get("hf_endpoint") or "").strip()
    rows.append({
        "key": "mirror", "label": "下载镜像", "status": "ok" if endpoint else "warn",
        "value": endpoint or "未配置",
        "hint": "" if endpoint else "镜像为空时走官方源，国内下载会很慢；一键配置可补默认镜像",
    })
    exists = cache_root.exists()
    rows.append({
        "key": "cache_dir", "label": "缓存目录", "status": "ok" if exists else "warn",
        "value": str(cache_root),
        "hint": "" if exists else "目录尚不存在；一键配置可创建",
    })
    default_model = str(settings.get("whisper_model") or "tiny")
    default_ok = bool(installed.get(default_model))
    rows.append({
        "key": "default_model", "label": "默认模型", "status": "ok" if default_ok else "warn",
        "value": default_model + ("（已安装）" if default_ok else "（未下载）"),
        "hint": "" if default_ok else "转写前需先下载默认模型，或切到已安装的档位",
    })
    return rows


def plan_fixes(
    *,
    cache_root: Path,
    settings: dict,
    states: Iterable,
) -> list[dict]:
    """一键配置的修复计划；全部幂等，绝不自动下载模型。"""
    fixes: list[dict] = []
    if not cache_root.exists():
        fixes.append({"key": "cache_dir", "label": "创建模型缓存目录", "action": "mkdir"})
    if not str(settings.get("hf_endpoint") or "").strip():
        fixes.append({
            "key": "mirror", "label": "补默认下载镜像 " + DEFAULT_HF_ENDPOINT,
            "action": "set_endpoint",
        })
    default_model = str(settings.get("whisper_model") or "tiny")
    installed_verified = [s for s in states if getattr(s, "verified", False)]
    if not any(s.spec.name == default_model for s in installed_verified):
        best = max(
            (s for s in installed_verified),
            key=lambda s: _tier_of(s.spec.name),
            default=None,
        )
        if best is not None:
            fixes.append({
                "key": "default_model",
                "label": f"默认模型 {default_model} 未下载，切到已安装的 {best.spec.name}",
                "action": "switch_default",
                "value": best.spec.name,
            })
    return fixes


def _tier_of(name: str) -> int:
    for spec in MODEL_TIERS:
        if spec.name == name:
            return spec.tier
    return 0


def detect_vram_mb() -> Optional[int]:
    """nvidia-smi 可用时读取最大一块显卡的显存（MB）；失败返回 None。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    values = []
    for line in (result.stdout or "").splitlines():
        text = line.strip()
        if text.isdigit():
            values.append(int(text))
    return max(values) if values else None
