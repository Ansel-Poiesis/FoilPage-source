# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""任务终态提示音（1.3.12）：预设音频组 + 自定义 WAV。

解析语义（resolve_notification_sound）：
- silent  完全静音（连系统音也不响）
- file    播放 WAV（内置预设组或用户自选文件）
- system  现状系统音（MessageBeep，成功/失败不同）

播放（play_notification_sound）只依赖 winsound（非 Windows 静默跳过），
任何失败回退系统音，绝不向调用方抛错——提示音不允许影响任务通知链路。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping, Optional, Tuple

from engine.settings import NOTIFICATION_SOUND_PACKS

BUNDLED_PACKS = ("crisp", "soft", "deep")


def _default_resource_root() -> Path:
    # PyInstaller 冻结后资源在 _MEIPASS；开发态为仓库根目录。
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent.parent


def resolve_notification_sound(
    pack: object,
    kind: str,
    custom_path: str = "",
    resource_root: Optional[Path] = None,
) -> Tuple[str, Optional[Path]]:
    """把设置里的预设组解析成具体播放动作。

    返回 ("silent", None) / ("file", wav 路径) / ("system", None)。
    未知预设组、缺失文件、非 WAV 自定义路径一律回退 system。
    """
    if kind not in ("success", "error"):
        return ("system", None)
    pack_name = pack if pack in NOTIFICATION_SOUND_PACKS else "system"
    if pack_name == "silent":
        return ("silent", None)
    if pack_name in BUNDLED_PACKS:
        root = resource_root or _default_resource_root()
        candidate = root / "assets" / "sounds" / pack_name / f"{kind}.wav"
        if candidate.is_file():
            return ("file", candidate)
        return ("system", None)
    if pack_name == "custom":
        path = Path(str(custom_path or ""))
        if path.suffix.lower() == ".wav" and path.is_file():
            return ("file", path)
        return ("system", None)
    return ("system", None)


def play_notification_sound(
    kind: str,
    settings: Mapping[str, object],
    resource_root: Optional[Path] = None,
) -> None:
    """按设置播放终态提示音；失败静默回退，不抛错。"""
    mode, path = resolve_notification_sound(
        settings.get("notification_sound_pack", "system"),
        kind,
        str(settings.get(f"notification_sound_{kind}_path", "") or ""),
        resource_root=resource_root,
    )
    try:
        import winsound

        if mode == "silent":
            return
        if mode == "file" and path is not None:
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        if kind == "error":
            winsound.MessageBeep(winsound.MB_ICONHAND)
        else:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass
