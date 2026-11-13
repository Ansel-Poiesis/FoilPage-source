# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""CUDA 运行时 DLL 搜索路径注入（冻结项 §3.2 / §六）。

与旧 video_to_article.add_runtime_dll_dirs 的区别：
- 删除对 `C:\\Ansel_Work\\...\\ollama\\cuda_v12` 的硬编码（原 line 54）。
- `cuda_dll_dir` 改由设置项传入（来自 settings.json），默认自动探测。
- 找不到任何候选 DLL 时不抛错，仅返回空列表，交由上层决定回退 CPU。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional


_DLL_DIRECTORY_HANDLES: list[object] = []
_REGISTERED_DLL_DIRS: set[str] = set()


def _candidate_dirs(cuda_dll_dir: Optional[str]) -> List[Path]:
    candidates: List[Path] = []
    if cuda_dll_dir:
        candidates.append(Path(cuda_dll_dir))

    # PyInstaller onedir distributions place collected NVIDIA wheels below the
    # worker directory. Source runs use site-packages discovery below.
    frozen_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    candidates.extend((frozen_root / "cuda", frozen_root / "nvidia"))
    candidates.extend(frozen_root.glob("nvidia/*/bin"))

    cuda_path = os.environ.get("CUDA_PATH", "")
    if cuda_path:
        candidates.append(Path(cuda_path) / "bin")

    env_dir = os.environ.get("VIDEO_TO_ARTICLE_CUDA_DLL_DIR", "")
    if env_dir:
        candidates.append(Path(env_dir))

    try:
        import site

        for root in site.getsitepackages():
            root_path = Path(root)
            candidates.extend(root_path.glob("nvidia/*/bin"))
            candidates.extend(root_path.glob("torch/lib"))
            ct2_dir = root_path / "ctranslate2"
            if ct2_dir.exists():
                candidates.append(ct2_dir)
    except Exception:
        pass

    return [p for p in candidates if p.exists()]


def add_runtime_dll_dirs(cuda_dll_dir: Optional[str] = None) -> List[str]:
    """把可用的 CUDA runtime DLL 目录加入搜索路径，返回实际加入的目录列表。"""
    added: List[str] = []
    current = [p.lower() for p in os.environ.get("PATH", "").split(os.pathsep) if p]

    for path in _candidate_dirs(cuda_dll_dir):
        has_cuda_lib = any(path.glob("cublas64_*.dll")) or any(
            path.glob("cudnn64_*.dll")
        ) or any(
            path.glob("nvrtc64_*.dll")
        )
        if not has_cuda_lib:
            continue
        path_str = str(path)
        normalized = path_str.lower()
        changed = False
        if normalized not in current:
            os.environ["PATH"] = path_str + os.pathsep + os.environ.get("PATH", "")
            current.append(normalized)
            changed = True
        if hasattr(os, "add_dll_directory") and normalized not in _REGISTERED_DLL_DIRS:
            try:
                handle = os.add_dll_directory(path_str)
                _DLL_DIRECTORY_HANDLES.append(handle)
                _REGISTERED_DLL_DIRS.add(normalized)
                changed = True
            except OSError:
                pass
        if changed:
            added.append(path_str)

    return added
