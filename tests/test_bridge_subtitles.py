# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""export_subtitles 桥接测试（资产化第一刀）。"""
from __future__ import annotations

import json
from pathlib import Path

from app.bridge import Bridge
from tests.test_bridge import FakeController, FakeStore, FakeWorker


def _task(row_id: str, raw_path: str) -> dict:
    return {
        "task_id": row_id,
        "kind": "transcription",
        "status": "done",
        "raw_path": raw_path,
        "final_path": str(Path(raw_path).with_suffix("")) + ".md",
        "display_title": "测试标题",
        "updated_at": "2026-08-23T10:00:00",
        "source_path": "C:/tmp/sample.mp4",
    }


def _make_bridge(rows):
    controller = FakeController(rows=rows)
    worker = FakeWorker(running=True)
    return Bridge(
        controller,
        worker,
        FakeStore(),
        window_provider=lambda: None,
        on_quit=lambda: None,
    )


def _write_job(tmp_path, job_name: str, segments) -> tuple[Path, str]:
    raw = tmp_path / job_name / "标题_raw.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("转写文本", encoding="utf-8")
    seg = raw.with_name(raw.name[: -len("_raw.md")] + "_segments.json")
    seg.write_text(
        json.dumps(segments, ensure_ascii=False),
        encoding="utf-8",
    )
    return raw, str(seg)


def test_export_subtitles_writes_srt(tmp_path) -> None:
    raw, _ = _write_job(
        tmp_path,
        "j1",
        [
            {"start": 0.0, "end": 1.0, "text": "这一段是完整的第一句话"},
            {"start": 2.5, "end": 4.0, "text": "这一段是完整的第二句话"},
        ],
    )
    bridge = _make_bridge([_task("j1", str(raw))])
    dest = tmp_path / "out.srt"
    res = bridge.export_subtitles("job:j1", "srt", str(dest))
    assert res["ok"] is True
    assert res["count"] == 2
    text = dest.read_text(encoding="utf-8-sig")
    assert "00:00:00,000 --> 00:00:01,000" in text
    assert "第一句话" in text
    assert "00:00:02,500 --> 00:00:04,000" in text


def test_export_subtitles_writes_vtt(tmp_path) -> None:
    raw, _ = _write_job(
        tmp_path,
        "j2",
        [{"start": 0.0, "end": 1.0, "text": "hello"}],
    )
    bridge = _make_bridge([_task("j2", str(raw))])
    dest = tmp_path / "out.vtt"
    res = bridge.export_subtitles("job:j2", "vtt", str(dest))
    assert res["ok"] is True
    assert res["format"] == "vtt"
    text = dest.read_text(encoding="utf-8-sig")
    assert text.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.000" in text


def test_export_subtitles_appends_extension(tmp_path) -> None:
    raw, _ = _write_job(
        tmp_path,
        "j3",
        [{"start": 0.0, "end": 1.0, "text": "ok"}],
    )
    bridge = _make_bridge([_task("j3", str(raw))])
    res = bridge.export_subtitles("job:j3", "srt", str(tmp_path / "subtitle"))
    assert res["ok"] is True
    assert Path(res["path"]).suffix == ".srt"


def test_export_subtitles_missing_segments_reports_guidance(tmp_path) -> None:
    raw = tmp_path / "old" / "标题_raw.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("旧转写无时间轴", encoding="utf-8")
    bridge = _make_bridge([_task("old", str(raw))])
    res = bridge.export_subtitles("job:old", "srt", str(tmp_path / "x.srt"))
    assert res["ok"] is False
    assert res["code"] == "no_segments"
    assert "重新转写" in res["error"]


def test_export_subtitles_rejects_bad_format(tmp_path) -> None:
    raw, _ = _write_job(
        tmp_path,
        "j4",
        [{"start": 0.0, "end": 1.0, "text": "ok"}],
    )
    bridge = _make_bridge([_task("j4", str(raw))])
    res = bridge.export_subtitles("job:j4", "ass", str(tmp_path / "x.ass"))
    assert res["ok"] is False
    assert res["code"] == "bad_format"


def test_export_subtitles_missing_task(tmp_path) -> None:
    bridge = _make_bridge([])
    res = bridge.export_subtitles("job:nope", "srt", str(tmp_path / "x.srt"))
    assert res["ok"] is False
    assert res["code"] == "no_task"
