# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""文本规整（冻结项 S5）：OpenCC 繁→简。复用 video_to_article.convert_to_simplified。"""
from __future__ import annotations


def convert_to_simplified(text: str) -> str:
    """繁体转简体；未安装 opencc 时原样返回并提示。"""
    try:
        from opencc import OpenCC

        cc = OpenCC("t2s")
        result = cc.convert(text)
        if result != text:
            print("[OpenCC] 已转换繁体->简体")
        return result
    except ImportError:
        print("[警告] 未安装 opencc-python-reimplemented，跳过繁简转换；pip install opencc-python-reimplemented")
        return text
