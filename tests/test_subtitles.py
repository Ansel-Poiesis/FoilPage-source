# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""字幕导出纯函数测试（资产化第一刀：SRT/VTT）。"""
from __future__ import annotations

import json

import pytest

from engine.contracts import SubtitleSegment
from engine.subtitles import (
    build_subtitles,
    format_srt_timestamp,
    format_vtt_timestamp,
    load_segments_file,
    merge_segments,
    segments_to_srt,
    segments_to_vtt,
)


def _seg(start: float, end: float, text: str) -> SubtitleSegment:
    return SubtitleSegment(start=start, end=end, text=text)


def test_srt_timestamp_format() -> None:
    assert format_srt_timestamp(0.0) == "00:00:00,000"
    assert format_srt_timestamp(3661.5) == "01:01:01,500"


def test_vtt_timestamp_format() -> None:
    assert format_vtt_timestamp(3661.5) == "01:01:01.500"


def test_segments_to_srt_structure() -> None:
    srt = segments_to_srt([_seg(1.0, 2.5, "第一段内容"), _seg(5.0, 7.0, "第二段内容")])
    assert srt.startswith("1\n00:00:01,000 --> 00:00:02,500\n第一段内容")
    assert "2\n00:00:05,000 --> 00:00:07,000\n第二段内容" in srt
    assert srt.rstrip().endswith("第二段内容")


def test_segments_to_vtt_header() -> None:
    vtt = segments_to_vtt([_seg(1.0, 2.5, "你好")])
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.500" in vtt


def test_merge_short_segments_joins_cjk_without_space() -> None:
    segs = [_seg(0.0, 0.4, "你"), _seg(0.4, 1.2, "好"), _seg(1.2, 2.0, "世界")]
    merged = merge_segments(segs)
    assert len(merged) == 1
    assert merged[0]["text"] == "你好世界"
    assert merged[0]["start"] == 0.0
    assert merged[0]["end"] == pytest.approx(2.0)


def test_merge_keeps_space_for_english() -> None:
    segs = [_seg(0.0, 0.4, "hello"), _seg(0.4, 1.2, "world")]
    merged = merge_segments(segs)
    assert merged[0]["text"] == "hello world"


def test_merge_does_not_join_long_lines() -> None:
    segs = [_seg(0.0, 10.0, "甲" * 60), _seg(10.0, 20.0, "乙" * 60)]
    merged = merge_segments(segs)
    assert len(merged) == 2


def test_build_subtitles_format_and_extension() -> None:
    content, ext = build_subtitles([_seg(0.0, 1.0, "hi")], "srt")
    assert ext == "srt" and content
    content, ext = build_subtitles([_seg(0.0, 1.0, "hi")], "VTT")
    assert ext == "vtt" and content


def test_build_subtitles_rejects_unknown_format() -> None:
    with pytest.raises(ValueError):
        build_subtitles([_seg(0.0, 1.0, "hi")], "ass")


def test_empty_segments_raise() -> None:
    with pytest.raises(ValueError):
        segments_to_srt([])
    with pytest.raises(ValueError):
        segments_to_vtt([])


def test_load_segments_file_skips_blank_and_normalizes_time(tmp_path) -> None:
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            [
                {"start": 0.0, "end": 1.0, "text": "ok"},
                {"start": 1.0, "end": 2.0, "text": "  "},
                {"start": 3.0, "end": 2.0, "text": "bad time"},
                {"start": 5.0, "end": 6.0, "text": "  spaced  text  "},
            ]
        ),
        encoding="utf-8",
    )
    rows = load_segments_file(path)
    assert [r["text"] for r in rows] == ["ok", "bad time", "spaced text"]
    assert rows[1]["start"] <= rows[1]["end"]
