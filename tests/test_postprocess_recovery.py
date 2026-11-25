# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Offline failure paths for durable post-processing and atomic result publication."""
from __future__ import annotations

import subprocess
import sys
import sqlite3
import threading
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from app.bridge import Bridge
from app.controller import AppController
from app.translation_service import translate_article, translation_paths
from engine.agent_client import AgentStage
from engine.postprocess_store import PostprocessStore
from engine.settings import SettingsStore
from backup_fixtures import refresh_backup_checksums
from test_queue_store import _job, _manifest


@pytest.fixture
def content(tmp_path):
    controller = AppController(SettingsStore(base_dir=tmp_path / "data"))
    controller.settings.update(agent_enabled=True, agent_api_key="fixture", agent_billing_mode="byok")
    identifier = "123456abcdef"
    job = _job(identifier)
    job["output_dir"] = controller.settings["output_dir"]
    controller.queue.enqueue(job)
    controller.queue.claim_next("fixture")
    manifest = _manifest(identifier)
    root = Path(job["output_dir"]) / identifier
    root.mkdir(parents=True)
    for key, name in (("raw_path", "fixture_raw.md"), ("final_path", "fixture.md")):
        target = root / name
        target.write_text("原文第一版", encoding="utf-8")
        manifest[key] = str(target)
    article, sidecar = translation_paths(Path(manifest["raw_path"]), "en")
    article.write_text("Earlier translation\n", encoding="utf-8")
    sidecar.write_text('[{"src":"旧原文","dst":"Earlier translation"}]', encoding="utf-8")
    manifest.update(translation_status="done", translation_lang="en", translation_direction="zh-en",
                    translation_path=str(article), translation_segments_path=str(sidecar))
    controller.queue.save_manifest(manifest, worker_id="fixture")
    yield controller, identifier, manifest
    controller.close()


def bridge_for(controller):
    return Bridge(controller, None, controller.settings_store, lambda: None, lambda: None,
                  postprocess_store=PostprocessStore(controller.settings["database_path"]))


def test_failed_translation_survives_reopen_and_explicit_retry_has_new_identity(content):
    controller, identifier, previous = content
    bridge = bridge_for(controller)
    with mock.patch("app.translation_service.translate_article", side_effect=RuntimeError("synthetic provider failure")):
        bridge._translation_worker(identifier, "zh-en")
    first = bridge.get_translation_status("job:" + identifier)
    assert first["status"] == "failed" and first["has_translation"]
    bridge._postprocess.close()
    reopened = bridge_for(controller)
    restored = reopened.get_translation_status("job:" + identifier)
    assert restored["status"] == "failed" and restored["error"] == "synthetic provider failure"
    assert restored["run_id"] == first["run_id"]
    with mock.patch("app.translation_service.translate_article", return_value=previous):
        reopened._translation_worker(identifier, "zh-en")
    second = reopened.get_translation_status("job:" + identifier)
    assert second["status"] == "done" and second["run_id"] != first["run_id"]
    reopened._postprocess.close()


def test_process_exit_keeps_receipt_and_reopen_explains_interruption(content):
    controller, identifier, _ = content
    source = """import os,sys
from engine.postprocess_store import PostprocessStore
store=PostprocessStore(sys.argv[1])
store.start('translation',sys.argv[2],{'task_id':sys.argv[2],'direction':'zh-en','error':'','lang':''})
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", source, controller.settings["database_path"], identifier], check=True, timeout=10)
    bridge = bridge_for(controller)
    state = bridge.get_translation_status("job:" + identifier)
    assert state["status"] == "failed" and state["code"] == "interrupted" and state["retryable"]
    assert state["has_translation"]
    bridge._postprocess.close()


def test_interrupted_batch_preserves_completed_items_and_retry_set(tmp_path):
    path = str(tmp_path / "batch.db")
    store = PostprocessStore(path)
    run = store.start("batch", "", {"task_ids": ["job:a", "job:b", "job:c"],
                    "processed_ids": ["job:a"], "done": 1, "failed": 0, "failed_ids": [], "skipped": 0})
    store.close()
    store = PostprocessStore(path)
    store.interrupt_running()
    result = store.latest("batch")
    assert result["run_id"] == run["run_id"] and result["done"] == 1
    assert result["failed_ids"] == ["job:b", "job:c"] and result["failed"] == 2
    store.close()


def test_hosted_retranslation_routes_chat_through_stage_and_reuses_no_finished_intent(content, monkeypatch):
    controller, identifier, original = content
    controller.settings.update(agent_billing_mode="hosted", subscription_server_url="https://fixture.invalid")
    reservations = {}
    calls = []
    def post(stage, action, payload):
        key = stage.operation_id
        calls.append((key, action))
        if action == "begin":
            if key not in reservations:
                reservations[key] = {"request_id": key, "receipt": "fixture-receipt", "status": "pending",
                                     "units": 1, "charge": "credits", "model": "fixture"}
            return reservations[key]
        if action == "chat":
            assert reservations[key]["status"] == "pending"
            return {"reply": '{"translations":["English translation"]}'}
        reservations[key]["status"] = "succeeded" if action == "commit" else "refunded"
        return {"status": reservations[key]["status"]}
    monkeypatch.setattr(AgentStage, "_post", post)
    bridge = bridge_for(controller)
    bridge._translation_worker(identifier, "zh-en")
    first = bridge.get_translation_status("job:" + identifier)
    assert first["status"] == "done", first
    first_manifest = controller.queue.get_manifest(identifier)
    first_path = Path(first_manifest["translation_path"])
    Path(original["final_path"]).write_text("修改后的正文", encoding="utf-8")
    bridge._translation_worker(identifier, "zh-en")
    second = bridge.get_translation_status("job:" + identifier)
    assert second["status"] == "done" and second["run_id"] != first["run_id"]
    assert len(reservations) == 2 and all(value["status"] == "succeeded" for value in reservations.values())
    assert len([action for _, action in calls if action == "chat"]) == 2
    assert not first_path.exists()
    assert controller.queue.get_manifest(identifier)["translation_source_revision"] != first_manifest["translation_source_revision"]
    bridge._postprocess.close()


@pytest.mark.parametrize("failure", ["article", "manifest", "source_changed"])
def test_failed_publication_keeps_previous_pair_and_removes_unpublished_generation(content, monkeypatch, failure):
    controller, identifier, previous = content
    from app.agent_service import _atomic_write
    original_article = Path(previous["translation_path"]).read_bytes()
    original_pairs = Path(previous["translation_segments_path"]).read_bytes()
    def translate(*_args):
        if failure == "source_changed":
            Path(previous["final_path"]).write_text("编辑器保存的新版正文", encoding="utf-8")
        return ["New translation"]
    monkeypatch.setattr("app.translation_service._translate_batch", translate)
    if failure == "article":
        def write(path, content):
            if path.suffix == ".md":
                raise PermissionError("synthetic locked article")
            return _atomic_write(path, content)
        monkeypatch.setattr("app.agent_service._atomic_write", write)
    elif failure == "manifest":
        monkeypatch.setattr(controller.queue, "replace_manifest", mock.Mock(side_effect=RuntimeError("synthetic SQL failure")))
    bridge = bridge_for(controller)
    bridge._translation_worker(identifier, "zh-en")
    state = bridge.get_translation_status("job:" + identifier)
    assert state["status"] == "failed" and state["has_translation"]
    assert Path(previous["translation_path"]).read_bytes() == original_article
    assert Path(previous["translation_segments_path"]).read_bytes() == original_pairs
    assert not list(Path(previous["raw_path"]).parent.glob("*_译文_en.*.md"))
    assert not list(Path(previous["raw_path"]).parent.glob("*_译文_en.*.segments.json"))
    bridge._postprocess.close()


def test_translation_cancelled_after_reply_does_not_publish_pair(content, monkeypatch):
    controller, _, previous = content
    stopped = [False]
    def translate(*_args):
        stopped[0] = True
        return ["new translation"]
    monkeypatch.setattr("app.translation_service._translate_batch", translate)
    with pytest.raises(RuntimeError, match="取消"):
        translate_article(previous, {"agent_billing_mode": "byok", "_cancel_check": lambda: stopped[0]}, "zh-en")
    assert Path(previous["translation_path"]).read_text(encoding="utf-8") == "Earlier translation\n"


def test_translation_cancelled_at_stage_exit_discards_new_pair(content, monkeypatch):
    from app.agent_service import _atomic_write
    _, _, previous = content
    stopped = [False]
    monkeypatch.setattr("app.translation_service._translate_batch", lambda *_: ["new translation"])
    def write(path, text):
        _atomic_write(path, text)
        if path.suffix == ".md":
            stopped[0] = True
    monkeypatch.setattr("app.agent_service._atomic_write", write)
    with pytest.raises(RuntimeError, match="取消"):
        translate_article(previous, {"agent_billing_mode": "byok", "_cancel_check": lambda: stopped[0]}, "zh-en")
    assert Path(previous["translation_path"]).read_text(encoding="utf-8") == "Earlier translation\n"
    assert not list(Path(previous["raw_path"]).parent.glob("*_译文_en.*.md"))
    assert not list(Path(previous["raw_path"]).parent.glob("*_译文_en.*.segments.json"))


def test_reopen_recognizes_published_translation_before_receipt_checkpoint(content):
    controller, identifier, previous = content
    store = PostprocessStore(controller.settings["database_path"])
    run = store.start("translation", identifier, {"task_id": identifier, "error": "", "lang": ""})
    updated = {**previous, "translation_run_id": run["run_id"]}
    controller.queue.replace_manifest(updated)
    store.close()
    bridge = bridge_for(controller)
    state = bridge.get_translation_status("job:" + identifier)
    assert state["status"] == "done" and state["run_id"] == run["run_id"]
    assert state["has_translation"] and not state["retryable"]
    bridge.close_postprocessing()


def test_older_backup_without_receipts_can_be_imported_and_polled(content, tmp_path):
    from app.maintenance import export_backup
    controller, identifier, _ = content
    archive = tmp_path / "old-client.zip"
    # Export before the new receipt table has ever been created.
    assert export_backup(controller, controller.settings_store, str(archive))["ok"]
    bridge = bridge_for(controller)
    with mock.patch("app.translation_service.translate_article", side_effect=RuntimeError("discarded state")):
        bridge._translation_worker(identifier, "zh-en")
    assert bridge.import_backup(str(archive))["ok"]
    assert bridge.get_translation_status("job:" + identifier)["status"] == "done"
    assert not bridge.get_batch_agent_status()["running"]
    bridge.close_postprocessing()


def test_corrupt_imported_receipt_is_rejected_before_publishing(content, tmp_path):
    from app.maintenance import export_backup
    controller, identifier, previous = content
    store = PostprocessStore(controller.settings["database_path"])
    run = store.start("translation", identifier, {"task_id": identifier, "error": "", "lang": ""})
    store.update(run["run_id"], running=False, status="done")
    valid_archive = tmp_path / "valid-receipts.zip"
    assert export_backup(controller, controller.settings_store, str(valid_archive))["ok"]
    with zipfile.ZipFile(valid_archive) as backup:
        contents = {name: backup.read(name) for name in backup.namelist()}
    database = tmp_path / "corrupt-receipt.db"
    database.write_bytes(contents["library.db"])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE postprocess_runs SET state_json='[]'")
    connection.close()
    contents["library.db"] = database.read_bytes()
    refresh_backup_checksums(contents)
    archive = tmp_path / "bad-receipts.zip"
    with zipfile.ZipFile(archive, "w") as backup:
        for name, payload in contents.items():
            backup.writestr(name, payload)
    with store._conn:
        store._conn.execute("DELETE FROM postprocess_runs")
    store.close()
    bridge = bridge_for(controller)
    result = bridge.import_backup(str(archive))
    assert not result["ok"] and "后台处理记录损坏" in result["error"]
    assert controller.queue.get_manifest(identifier) == previous
    assert bridge._postprocess.latest("translation") is None
    assert run["run_id"]
    bridge.close_postprocessing()


def test_unfinished_restore_freezes_receipt_writes_and_close_keeps_journal_fact(content, monkeypatch):
    controller, identifier, _ = content
    bridge = bridge_for(controller)
    run = bridge._postprocess.start("translation", identifier, {"task_id": identifier, "error": "", "lang": ""})
    def incomplete(*_):
        controller._backup_restore_blocked = True
        return {"ok": False, "recovery_required": True}
    monkeypatch.setattr("app.bridge._import_backup", incomplete)
    assert bridge.import_backup("fixture.zip")["recovery_required"]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        bridge._postprocess.update(run["run_id"], status="failed", running=False)
    bridge.close_postprocessing()
    with sqlite3.connect(controller.settings["database_path"]) as connection:
        assert connection.execute("SELECT status FROM postprocess_runs WHERE run_id=?", (run["run_id"],)).fetchone()[0] == "running"


def test_normal_desktop_entry_persists_receipts_without_gui_or_worker(tmp_path, monkeypatch):
    from app import desktop_web
    root = tmp_path / "desktop"
    settings = SettingsStore(base_dir=root)
    value = settings.load()
    value["local_api_enabled"] = False
    settings.save(value)
    monkeypatch.setattr(desktop_web, "register_identity", lambda *_: False)
    tray = mock.Mock()
    tray.start.return_value = False
    monkeypatch.setattr(desktop_web, "TrayController", mock.Mock(return_value=tray))
    app = desktop_web.WebDesktopApp(data_dir=str(root), start_worker=False, create_window=False)
    try:
        run = app.bridge._postprocess.start("translation", "fixture", {"task_id": "fixture", "error": "", "lang": ""})
        app.bridge._translation = run
    finally:
        app._quit_watchdog_armed = True
        app._close()
    with sqlite3.connect(value["database_path"]) as connection:
        row = connection.execute("SELECT status,state_json FROM postprocess_runs WHERE run_id=?", (run["run_id"],)).fetchone()
        assert row[0] == "failed" and "interrupted" in row[1]
    reopened = desktop_web.WebDesktopApp(data_dir=str(root), start_worker=False, create_window=False)
    try:
        state = reopened.bridge.get_translation_status("job:fixture")
        assert state["run_id"] == run["run_id"] and state["code"] == "interrupted"
    finally:
        reopened._quit_watchdog_armed = True
        reopened._close()


def test_late_settlement_cannot_replace_a_newer_translation_run(content, monkeypatch):
    controller, identifier, _ = content
    monkeypatch.setattr("app.translation_service._translate_batch", lambda *_: ["new translation"])
    entered = threading.Event()
    released = threading.Event()
    calls = []
    def settle(*_, **__):
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert released.wait(5)
        return {}
    monkeypatch.setattr("app.translation_service.settle_agent_stages", settle)
    bridge = bridge_for(controller)
    first = threading.Thread(target=bridge._translation_worker, args=(identifier, "zh-en"))
    first.start()
    try:
        assert entered.wait(5)
        first_id = bridge.get_translation_status("job:" + identifier)["run_id"]
        bridge._translation_worker(identifier, "zh-en")
        second_id = bridge.get_translation_status("job:" + identifier)["run_id"]
        assert second_id != first_id
    finally:
        released.set()
        first.join(5)
    assert not first.is_alive()
    assert bridge.get_translation_status("job:" + identifier)["run_id"] == second_id
    assert controller.queue.get_manifest(identifier)["translation_run_id"] == second_id
    bridge.close_postprocessing()
