# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Collect locally installed vendor material, preserving its original bytes."""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import stat
import sys


GPU = ("nvidia-cublas-cu12", "nvidia-cuda-nvrtc-cu12", "nvidia-cudnn-cu12")


def collect(root: Path, output: Path, gpu_only: bool = False) -> None:
    output = output.absolute()
    for parent in (output, *output.parents):
        attributes = getattr(parent.lstat(), "st_file_attributes", 0) if parent.exists() else 0
        if parent.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("Vendor material output cannot traverse a filesystem link")
    output = output.resolve()
    if not output.is_relative_to(root / "dist"):
        raise ValueError("Vendor material output must stay inside this checkout's dist directory")
    rows = []
    sources = []
    for name in GPU if gpu_only else ("imageio-ffmpeg", "pyinstaller", *GPU):
        distribution = metadata.distribution(name)
        files = [path for path in distribution.files or ()
                 if path.name.lower() in {"license", "license.txt", "copying.txt"}]
        if not files:
            raise ValueError(f"Local vendor license is missing: {name}")
        for path in files:
            sources.append((name, distribution.version, Path(distribution.locate_file(path)),
                            f"{name}/{path.name}"))
    if not gpu_only:
        sources.extend([
            ("LXGW WenKai Screen", "bundled", root / "assets/fonts/OFL.txt", "fonts/OFL.txt"),
            ("MicroModal", "bundled", root / "app/webview_ui/vendor/micromodal-LICENSE.txt", "micromodal/LICENSE.txt"),
        ])
        # Official originals added by the release maintainer are included as-is.
        for path in sorted((root / "docs/licenses").glob("*")):
            if path.is_file() and "ffmpeg" in path.name.lower():
                sources.append(("FFmpeg materials", "bundled", path, f"ffmpeg/{path.name}"))
        for path in sorted((root / "assets/third-party").glob("*")):
            if path.is_file() and "ffmpeg" in path.name.lower():
                sources.append(("FFmpeg materials", "bundled", path, f"ffmpeg/{path.name}"))
    output.mkdir(parents=True, exist_ok=False)
    for name, version, source, relative in sources:
        payload = source.read_bytes()
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(payload)
        rows.append({"component": name, "version": version, "file": relative,
                     "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    (output / "inventory.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 随包组件材料索引", "", "本目录保存构建环境中已有的第三方原始许可及随附材料；inventory.json 记录版本、文件和 SHA-256。", "",
             "NVIDIA 原件对应可选 GPU 运行库；imageio-ffmpeg 原件对应 Python 封装，FFmpeg 二进制材料单列。此索引只记录随包文件，不替代完整许可审查。", ""]
    lines += [f"- {row['component']} {row['version']}：[{row['file']}]({row['file']})" for row in rows]
    if not gpu_only and not any(row["component"] == "FFmpeg materials" for row in rows):
        lines += ["", "本地依赖未提供 FFmpeg 二进制的独立许可及对应源码材料，当前目录未据封装库许可补作该证明。"]
    (output / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--gpu-only", action="store_true")
    args = parser.parse_args()
    try:
        collect(Path(__file__).resolve().parents[1], args.output, args.gpu_only)
        return 0
    except (OSError, ValueError, metadata.PackageNotFoundError) as exc:
        print(f"Vendor material collection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
