# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Batch publication never overwrites a saved article or writes after shutdown."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, RLock
from types import SimpleNamespace

import pytest

from app import agent_service
from engine.agent_client import AgentError


class Queue:
    def __init__(self, manifest, stopped, lock):
        self.manifest = dict(manifest)
        self.stopped = stopped
        self.lock = lock
        self.effects = []

    def get_job(self, job_id):
        return {"id": job_id, "status": "done", "source_title": "标题", "source_path": self.manifest["source_path"]}

    def get_manifest(self, _job_id):
        return dict(self.manifest)

    def effect(self, name):
        self.effects.append((name, self.stopped.is_set(), self.lock._is_owned()))

    def replace_manifest(self, value):
        self.effect("manifest")
        self.manifest = dict(value)

    def set_tags(self, _job_id, _tags):
        self.effect("tags")

    def set_source_title(self, _job_id, _title):
        self.effect("title")


@pytest.fixture
def batch(tmp_path, monkeypatch):
    raw = tmp_path / "sample_raw.md"
    article = tmp_path / "标题_文章.md"
    sidecar = tmp_path / "sample_manifest.json"
    raw.write_text("原始素材", encoding="utf-8")
    article.write_text("已保存的旧文章", encoding="utf-8")
    sidecar.write_text("已保存的旧 manifest", encoding="utf-8")
    stopped, lock = Event(), RLock()
    manifest = {"job_id": "000000000001", "raw_path": str(raw), "final_path": str(article),
                "source_path": str(tmp_path / "source.mp4"), "source_title": "标题"}
    queue = Queue(manifest, stopped, lock)
    settings = {"agent_billing_mode": "byok", "title_optimization_enabled": False}
    rewritten = SimpleNamespace(markdown="新生成文章", profile="knowledge", model="synthetic", tags=["新标签"])
    def rewrite(*_args, **_kwargs):
        assert not lock._is_owned(), "model work must not hold the desktop publication lock"
        return rewritten
    monkeypatch.setattr(agent_service, "rewrite_transcript", rewrite)
    def run(**kwargs):
        return agent_service.batch_apply_agent_translation(
            [manifest["job_id"]], queue, settings, cancel_check=stopped.is_set,
            publication_lock=lock, **kwargs,
        )
    return SimpleNamespace(root=tmp_path, raw=raw, article=article, sidecar=sidecar,
                           stopped=stopped, lock=lock, queue=queue, settings=settings, run=run)


def assert_old_outputs(batch):
    assert batch.article.read_text(encoding="utf-8") == "已保存的旧文章"
    assert batch.sidecar.read_text(encoding="utf-8") == "已保存的旧 manifest"


def test_cancel_during_staged_article_write_preserves_old_output_and_never_publishes(batch, monkeypatch):
    original = agent_service._atomic_write
    def write_then_stop(path, content):
        original(path, content)
        batch.stopped.set()
    monkeypatch.setattr(agent_service, "_atomic_write", write_then_stop)
    result = batch.run()
    assert result["done"] == 0 and result["failed"] == 1
    assert_old_outputs(batch)
    assert batch.queue.effects == []
    assert set(batch.root.glob("*.md")) == {batch.raw, batch.article}


def test_shutdown_wins_publication_lock_before_generated_output_can_commit(batch, monkeypatch):
    ready = Event()
    original = agent_service._atomic_write
    def staged_write(path, content):
        original(path, content)
        ready.set()
    monkeypatch.setattr(agent_service, "_atomic_write", staged_write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with batch.lock:
            future = pool.submit(batch.run)
            assert ready.wait(3)
            batch.stopped.set()
        result = future.result(timeout=3)
    assert result["failed"] == 1
    assert batch.queue.effects == []
    assert_old_outputs(batch)
    assert set(batch.root.glob("*.md")) == {batch.raw, batch.article}


def test_success_publishes_independent_article_and_progress_without_sidecar_write(batch):
    snapshots = []
    result = batch.run(progress=snapshots.append)
    assert result["done"] == 1 and result["failed"] == 0
    assert_old_outputs(batch)
    current = Path(batch.queue.manifest["final_path"])
    assert current != batch.article and current.parent == batch.article.parent
    assert current.read_text(encoding="utf-8") == "# 标题\n\n新生成文章"
    assert len(snapshots) == 1 and snapshots[0]["processed_ids"] == ["job:000000000001"]
    assert snapshots[0]["done"] == 1
    assert all(not stopped and held for _, stopped, held in batch.queue.effects)


def test_body_failure_title_fallback_is_also_private_when_shutdown_arrives(batch, monkeypatch):
    batch.settings["title_optimization_enabled"] = True
    def failed_body(*_args, **_kwargs):
        raise AgentError("synthetic body failure")
    monkeypatch.setattr(agent_service, "rewrite_transcript", failed_body)
    monkeypatch.setattr("engine.title_optimizer.optimize_title", lambda *_args, **_kwargs: "新标题")
    original = agent_service._atomic_write
    def write_then_stop(path, content):
        original(path, content)
        batch.stopped.set()
    monkeypatch.setattr(agent_service, "_atomic_write", write_then_stop)
    result = batch.run()
    assert result["failed"] == 1 and batch.queue.effects == []
    assert_old_outputs(batch)
    assert set(batch.root.glob("*.md")) == {batch.raw, batch.article}


def test_side_effects_publish_under_lock_and_late_settlement_cannot_write_after_shutdown(batch, monkeypatch):
    batch.settings.update(title_optimization_enabled=True, delete_source_after_translation=True)
    monkeypatch.setattr("engine.title_optimizer.optimize_title", lambda *_args, **_kwargs: "新标题")
    monkeypatch.setattr("app.worker.Worker._delete_downloaded_source", lambda *_args: batch.queue.effect("source_delete"))
    def late_settlement(_stages, *, success):
        assert success is True and not batch.lock._is_owned()
        with batch.lock:
            batch.stopped.set()
        return {"body_usage_units": 0}
    monkeypatch.setattr(agent_service, "settle_agent_stages", late_settlement)
    result = batch.run(delete_source=True)
    assert result["done"] == 1 and result["failed"] == 0
    assert [name for name, _, _ in batch.queue.effects] == ["manifest", "tags", "title", "source_delete"]
    assert all(not stopped and held for _, stopped, held in batch.queue.effects)
    assert Path(batch.queue.manifest["final_path"]).is_file()
    assert_old_outputs(batch)


def test_database_publication_failure_discards_only_new_article(batch, monkeypatch):
    def reject(_manifest):
        raise OSError("synthetic database failure")
    monkeypatch.setattr(batch.queue, "replace_manifest", reject)
    result = batch.run()
    assert result["failed"] == 1
    assert_old_outputs(batch)
    assert set(batch.root.glob("*.md")) == {batch.raw, batch.article}


def test_failed_body_only_records_failure_and_preserves_source_and_user_tags(batch, monkeypatch):
    batch.settings["delete_source_after_translation"] = True
    batch.queue.manifest["agent_tags"] = ["上一轮模型标签"]
    def failed_body(*_args, **_kwargs):
        raise AgentError("synthetic provider failure")
    monkeypatch.setattr(agent_service, "rewrite_transcript", failed_body)
    monkeypatch.setattr("app.worker.Worker._delete_downloaded_source", lambda *_args: batch.queue.effect("source_delete"))
    result = batch.run(delete_source=True)
    assert result["failed"] == 1 and result["done"] == 0
    assert [name for name, _, _ in batch.queue.effects] == ["manifest"]
    assert batch.queue.manifest["body_status"] == "skipped"
    assert_old_outputs(batch)


def test_title_fallback_on_previous_run_keeps_one_run_suffix_and_preserves_previous_file(batch, monkeypatch):
    previous = batch.root / ("标题_文章.foilpage-" + "a" * 32 + ".md")
    previous.write_text("上一轮独立文章", encoding="utf-8")
    batch.queue.manifest["final_path"] = str(previous)
    batch.settings["title_optimization_enabled"] = True
    def failed_body(*_args, **_kwargs):
        raise AgentError("synthetic provider failure")
    monkeypatch.setattr(agent_service, "rewrite_transcript", failed_body)
    monkeypatch.setattr("engine.title_optimizer.optimize_title", lambda *_args, **_kwargs: "新标题")
    batch.run()
    current = Path(batch.queue.manifest["final_path"])
    assert current != previous and len(current.stem.split(".")) == 2
    assert current.read_text(encoding="utf-8").startswith("# 新标题")
    assert previous.read_text(encoding="utf-8") == "上一轮独立文章"
