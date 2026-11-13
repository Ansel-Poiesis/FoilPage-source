# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""yt-dlp 版本维护防线：pin 与安装环境必须一致（升级节拍的回归闸门）。"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _pins(path: str) -> str:
    text = (_ROOT / path).read_text(encoding="utf-8")
    # extra 标记（如 yt-dlp[default]）是发布能力的一部分，一并校验两处一致。
    # uv --generate-hashes 锁的行尾带 " \" 续行符，与手写 pin 行同样接受。
    match = re.search(r"^yt-dlp(?:\[[a-z,\-]+\])?==(\d+\.\d+\.\d+)\s*(?:\\\s*)?$", text, re.M)
    assert match, f"{path} 中缺少 yt-dlp pin"
    return match.group(1)


def test_ytdlp_pin_matches_installed_version() -> None:
    """开发/构建环境里实际安装的 yt-dlp 必须与 requirements pin 一致。

    漂移是升级节拍最常见的失误：只跑了 pip install 忘了改 pin（或反之），
    导致「本机能用、发布包失效」或反向的不可复现构建。
    """
    import yt_dlp

    def normalize(version: str) -> tuple[int, ...]:
        # yt-dlp 输出规范化版本（2026.08.19 → 2026.8.19），按数值段比较。
        return tuple(int(part) for part in version.split("."))

    pinned = _pins("requirements-base.txt")
    assert pinned == _pins("requirements-base.lock.txt"), "两个 requirements 文件 pin 不一致"
    assert normalize(yt_dlp.version.__version__) == normalize(pinned), (
        f"venv 中 yt-dlp {yt_dlp.version.__version__} 与 pin {pinned} 不一致；"
        "请执行 pip install -r requirements-base.txt 对齐"
    )
