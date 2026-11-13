# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""字幕导出纯函数（资产化第一刀：SRT/VTT）。

数据源是转写时保留的带时间轴分段（``TranscriptionResult.segments``，
落盘为任务目录的 ``*_segments.json``）。本模块只做格式转换与可读性合并，
不读写用户目录、不依赖 GUI。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Sequence

from .contracts import SubtitleSegment

_MIN_SEGMENT_SECONDS = 0.5
_MAX_GAP_SECONDS = 0.8
_SHORT_TEXT_CHARS = 12
_MAX_LINE_CHARS = 80


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).strip()


def _is_cjk(ch: str) -> bool:
    return bool(ch) and "\u4e00" <= ch <= "\u9fff"


def _join_text(a: str, b: str) -> str:
    """合并两段文本：中日韩相邻直接拼接，其余语言补空格。"""
    if not a:
        return b
    if not b:
        return a
    if _is_cjk(a[-1]) and _is_cjk(b[0]):
        return a + b
    return a + " " + b


def merge_segments(
    segments: Sequence[SubtitleSegment],
    *,
    min_seconds: float = _MIN_SEGMENT_SECONDS,
    max_gap: float = _MAX_GAP_SECONDS,
    short_chars: int = _SHORT_TEXT_CHARS,
    max_chars: int = _MAX_LINE_CHARS,
) -> list[SubtitleSegment]:
    """把过碎的时间轴合并成可读字幕行。

    规则（对中文口语转写友好）：
    - 与下一段间隔 <= max_gap 且累计文本仍短，或本段时长太短时并入下一段；
    - 合并结果单行不超过 max_chars，避免超长行。
    """
    if not segments:
        return []
    merged: list[SubtitleSegment] = []
    current = dict(segments[0])
    for next_seg in segments[1:]:
        text = _clean_text(str(current.get("text") or ""))
        next_text = _clean_text(str(next_seg.get("text") or ""))
        start = float(current.get("start") or 0.0)
        end = float(current.get("end") or start)
        n_start = float(next_seg.get("start") or end)
        n_end = float(next_seg.get("end") or n_start)
        gap = max(0.0, n_start - end)
        duration = max(0.0, end - start)
        merged_len = len(text) + len(next_text)
        if (
            gap <= max_gap
            and merged_len <= max_chars
            and (len(text) < short_chars or duration < min_seconds)
        ):
            current = {
                "start": start,
                "end": max(end, n_end),
                "text": _join_text(text, next_text),
            }
        else:
            merged.append(_normalize(current))
            current = dict(next_seg)
    merged.append(_normalize(current))
    return merged


def _normalize(segment: dict) -> SubtitleSegment:
    start = max(0.0, float(segment.get("start") or 0.0))
    end = max(start, float(segment.get("end") or start))
    return SubtitleSegment(
        start=round(start, 3),
        end=round(end, 3),
        text=_clean_text(str(segment.get("text") or "")),
    )


def format_srt_timestamp(seconds: float) -> str:
    """SRT 时间戳：HH:MM:SS,mmm（毫秒用逗号）。"""
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def format_vtt_timestamp(seconds: float) -> str:
    """VTT 时间戳：HH:MM:SS.mmm（毫秒用点）。"""
    return format_srt_timestamp(seconds).replace(",", ".")


def segments_to_srt(segments: Sequence[SubtitleSegment]) -> str:
    rows = merge_segments(segments)
    if not rows:
        raise ValueError("无字幕段可导出")
    lines: list[str] = []
    for index, seg in enumerate(rows, start=1):
        lines.append(str(index))
        lines.append(
            f"{format_srt_timestamp(seg['start'])} --> "
            f"{format_srt_timestamp(seg['end'])}"
        )
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


def segments_to_vtt(segments: Sequence[SubtitleSegment]) -> str:
    rows = merge_segments(segments)
    if not rows:
        raise ValueError("无字幕段可导出")
    lines: list[str] = ["WEBVTT", ""]
    for seg in rows:
        lines.append(
            f"{format_vtt_timestamp(seg['start'])} --> "
            f"{format_vtt_timestamp(seg['end'])}"
        )
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


def build_subtitles(
    segments: Sequence[SubtitleSegment],
    fmt: str,
) -> tuple[str, str]:
    """按格式生成字幕文本与扩展名。fmt: 'srt' | 'vtt'。"""
    fmt = (fmt or "").lower().lstrip(".")
    if fmt == "srt":
        return segments_to_srt(segments), "srt"
    if fmt == "vtt":
        return segments_to_vtt(segments), "vtt"
    raise ValueError(f"不支持的字幕格式: {fmt!r}")


def load_segments_file(path: str | Path) -> list[SubtitleSegment]:
    """读取落盘的 ``*_segments.json``，容忍缺失时间轴字段。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("字幕数据格式非法：应为列表")
    rows: list[SubtitleSegment] = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        rows.append(_normalize(item))
    return rows
