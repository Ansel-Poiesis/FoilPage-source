# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""TASK-8.24（先生 2026-10-25）：重新转写模式覆盖位、文章翻译服务与 bridge 翻译 API。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from app.controller import AppController
from app.bridge import Bridge
from app.translation_service import (
    detect_language,
    split_segments,
    translate_article,
    _parse_translations,
)
from engine.queue_store import QueueStore
from engine.settings import SettingsStore


def _enqueue(queue: QueueStore, base: dict, **extra) -> str:
    job = {**base, **extra}
    queue.enqueue(job)
    return job["id"]


def _finish_job(queue: QueueStore, job_id: str, tmp: Path) -> None:
    claimed = queue.claim_next("test-worker")
    assert claimed and claimed["id"] == job_id
    raw = tmp / "output" / job_id / "a_raw.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("# 标题\n\n正文", encoding="utf-8")
    queue.save_manifest({
        "job_id": job_id, "source_path": claimed["source_path"], "source_sha256": None,
        "backend": "local", "model": "tiny", "language": "zh", "duration_sec": 1.0,
        "raw_path": str(raw), "final_path": str(raw), "content_mode": "raw",
        "prompt_version": "t", "created_at": "2026-10-25T20:00:00",
    }, worker_id="test-worker")


def test_queue_body_enabled_override_roundtrip(tmp_path: Path) -> None:
    queue = QueueStore(str(tmp_path / "q.db"))
    base = {"source_path": "x.mp4", "output_dir": str(tmp_path), "status": "queued",
            "whisper_model": "tiny", "device": "cpu", "language": "zh", "content_mode": "raw",
            "created_at": "2026-10-25T20:00:00", "updated_at": "2026-10-25T20:00:00"}
    _enqueue(queue, base, id="off123456789", body_enabled_override=False)
    _enqueue(queue, base, id="onn123456789", body_enabled_override=True)
    _enqueue(queue, base, id="none123456789")
    assert queue.get_job("off123456789")["body_enabled_override"] is False
    assert queue.get_job("onn123456789")["body_enabled_override"] is True
    assert queue.get_job("none123456789")["body_enabled_override"] is None
    queue.close()


def test_retranscribe_modes_carry_override(tmp_path: Path) -> None:
    controller = AppController(SettingsStore(base_dir=tmp_path / "data"))
    try:
        # 单源同时只允许一个活跃任务：每种模式各用独立源。
        job_ids = []
        for index in range(3):
            video = tmp_path / f"video{index}.mp4"
            video.write_bytes(b"video")
            base = {"source_path": str(video), "output_dir": controller.settings["output_dir"],
                    "status": "queued", "whisper_model": "tiny", "device": "cpu", "language": "zh",
                    "content_mode": "raw", "created_at": "2026-10-25T20:00:00", "updated_at": "2026-10-25T20:00:00"}
            job_id = _enqueue(controller.queue, base, id=f"mode1234567{index}")
            _finish_job(controller.queue, job_id, tmp_path)
            job_ids.append(job_id)

        raw = controller.retranscribe_jobs([job_ids[0]], mode="raw")
        assert len(raw.created) == 1
        raw_job = controller.queue.get_job(raw.created[0])
        assert raw_job["body_enabled_override"] is False and raw_job["content_mode"] == "raw"

        article = controller.retranscribe_jobs([job_ids[1]], mode="article")
        assert len(article.created) == 1
        article_job = controller.queue.get_job(article.created[0])
        assert article_job["body_enabled_override"] is True and article_job["content_mode"] == "article"

        default = controller.retranscribe_jobs([job_ids[2]])
        assert len(default.created) == 1
        assert controller.queue.get_job(default.created[0])["body_enabled_override"] is None

        with pytest.raises(ValueError):
            controller.retranscribe_jobs([job_ids[0]], mode="unknown")
    finally:
        controller.close()


def test_split_segments_keeps_fences_whole() -> None:
    markdown = "# 标题\n\n第一段。\n\n```python\nprint(1)\n\nprint(2)\n```\n\n第二段。"
    segments = split_segments(markdown)
    assert segments == ["# 标题", "第一段。", "```python\nprint(1)\n\nprint(2)\n```", "第二段。"]


def test_detect_language_by_cjk_ratio() -> None:
    assert detect_language("今天我们聊聊中文转写") == "zh"
    assert detect_language("This is an English transcript") == "en"
    assert detect_language("") == "zh"


def test_parse_translations_rejects_count_mismatch() -> None:
    from engine.agent_client import AgentError

    assert _parse_translations('{"translations":["a","b"]}', 2) == ["a", "b"]
    with pytest.raises(AgentError):
        _parse_translations('{"translations":["a"]}', 2)
    with pytest.raises(AgentError):
        _parse_translations('{"translations":["a",""]}', 2)


def _translate_settings(tmp_path: Path) -> dict:
    return {"agent_api_key": "fixture-key", "agent_billing_mode": "byok",
            "agent_model": "fixture-model", "agent_timeout_seconds": 5,
            "database_path": str(tmp_path / "data" / "library.db")}


def test_translate_article_writes_outputs(tmp_path: Path, monkeypatch) -> None:
    raw = tmp_path / "output" / "job" / "fixture_raw.md"
    raw.parent.mkdir(parents=True)
    markdown = "# 标题\n\n第一段。\n\n第二段。"
    raw.write_text(markdown, encoding="utf-8")
    calls = []

    def fake_chat(settings, messages, *, json_mode=False, cancel_check=None):
        calls.append(messages)
        payload = json.loads(messages[1]["content"].rsplit("：\n", 1)[1])
        return json.dumps({"translations": [f"EN: {segment}" for segment in payload]},
                          ensure_ascii=False)

    monkeypatch.setattr("app.translation_service.chat_completion", fake_chat)
    manifest = {"job_id": "job12345678", "raw_path": str(raw), "final_path": "",
                "source_title": "标题"}
    updated = translate_article(manifest, _translate_settings(tmp_path), "zh-en")
    assert updated["translation_status"] == "done"
    assert updated["translation_lang"] == "en"
    article = Path(updated["translation_path"])
    segments_file = Path(updated["translation_segments_path"])
    assert article.is_file() and article.name == "fixture_译文_en.md"
    assert article.read_text(encoding="utf-8") == "EN: # 标题\n\nEN: 第一段。\n\nEN: 第二段。\n"
    pairs = json.loads(segments_file.read_text(encoding="utf-8"))
    assert [pair["dst"] for pair in pairs] == ["EN: # 标题", "EN: 第一段。", "EN: 第二段。"]
    assert pairs[1]["src"] == "第一段。"
    assert calls and calls[0][0]["role"] == "system"


def test_translate_article_rejects_bad_direction_or_empty(tmp_path: Path) -> None:
    from engine.agent_client import AgentError

    raw = tmp_path / "empty_raw.md"
    raw.write_text("   \n", encoding="utf-8")
    with pytest.raises(AgentError):
        translate_article({"raw_path": str(raw)}, _translate_settings(tmp_path), "zh-en")
    raw.write_text("正文", encoding="utf-8")
    with pytest.raises(AgentError):
        translate_article({"raw_path": str(raw)}, _translate_settings(tmp_path), "fr-de")


class TranslationBridgeController:
    """bridge 翻译 API 的最小替身：真实锁＋可控 queue/settings。"""

    def __init__(self, tmp_path: Path, settings: dict | None = None) -> None:
        self.media_action_lock = threading.RLock()
        self.settings = {"output_dir": str(tmp_path / "output"), "agent_enabled": True,
                         "agent_billing_mode": "byok", "agent_api_key": "k",
                         **(settings or {})}
        self.queue = mock.Mock()


def _make_bridge(controller) -> Bridge:
    return Bridge(controller, worker=None, settings_store=mock.Mock(),
                  window_provider=lambda: None, on_quit=lambda: None)


def _done_manifest(tmp_path: Path, translation: bool = False) -> dict:
    manifest = {"job_id": "job12345678", "raw_path": str(tmp_path / "raw.md"),
                "final_path": str(tmp_path / "final.md"),
                "translation_status": "done" if translation else "",
                "translation_lang": "en" if translation else "",
                "translation_direction": "zh-en" if translation else "",
                "translation_path": str(tmp_path / "out" / "fixture_译文_en.md"),
                "translation_segments_path": str(tmp_path / "out" / "fixture_译文_en.segments.json"),
                "translation_error": ""}
    return manifest


def test_translate_text_gates(tmp_path: Path) -> None:
    controller = TranslationBridgeController(tmp_path)
    bridge = _make_bridge(controller)
    controller.queue.get_job.return_value = {"id": "job12345678", "status": "running"}
    assert bridge.translate_text("job:job12345678")["ok"] is False
    controller.queue.get_job.return_value = {"id": "job12345678", "status": "done"}
    controller.queue.get_manifest.return_value = None
    assert bridge.translate_text("job:job12345678")["ok"] is False
    controller.queue.get_manifest.return_value = _done_manifest(tmp_path)
    controller.settings["agent_enabled"] = False
    assert bridge.translate_text("job:job12345678")["ok"] is False
    controller.settings["agent_enabled"] = True
    assert bridge.translate_text("job:job12345678", "fr-de")["ok"] is False


def test_translation_worker_persists_manifest_and_state(tmp_path: Path, monkeypatch) -> None:
    controller = TranslationBridgeController(tmp_path)
    bridge = _make_bridge(controller)
    controller.queue.get_manifest.return_value = _done_manifest(tmp_path)
    updated = {**_done_manifest(tmp_path), "translation_status": "done", "translation_lang": "en"}

    def fake_translate(manifest, settings, direction):
        assert direction == "auto"
        return updated

    monkeypatch.setattr("app.translation_service.translate_article", fake_translate)
    bridge._translation_worker("job12345678", "auto")
    controller.queue.replace_manifest.assert_called_once_with(updated)
    assert bridge.get_translation_status("job:job12345678")["status"] == "done"
    assert bridge._translation["running"] is False

    controller.queue.get_manifest.return_value = None
    bridge._translation_worker("job12345678", "auto")
    assert bridge._translation["status"] == "failed"
    assert bridge._translation["running"] is False


def test_translate_text_rejects_concurrent_run(tmp_path: Path, monkeypatch) -> None:
    controller = TranslationBridgeController(tmp_path)
    bridge = _make_bridge(controller)
    controller.queue.get_job.return_value = {"id": "job12345678", "status": "done"}
    controller.queue.get_manifest.return_value = _done_manifest(tmp_path)
    # 慢速 runner 上 worker 可能瞬间完成把 running 置回 False，第二次调用
    # 就不再被并发挡住——用事件把 worker 挡住，测试完再放行。
    import threading as _threading
    release = _threading.Event()
    def blocking_translate(manifest, settings, direction):
        release.wait(timeout=5)
        return {**_done_manifest(tmp_path), "translation_status": "done", "translation_lang": "en"}
    monkeypatch.setattr("app.translation_service.translate_article", blocking_translate)
    with mock.patch.object(bridge, "get_subscription_status", return_value={}):
        assert bridge.translate_text("job:job12345678")["ok"] is True
        assert bridge.translate_text("job:job12345678")["ok"] is False  # 已在跑
        release.set()
        for _ in range(100):
            if not bridge._translation.get("running"):
                break
            time.sleep(0.02)
        assert bridge._translation["status"] == "done"
