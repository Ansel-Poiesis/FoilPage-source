# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Text-only deletion preserves task/media ownership and rejects stale writers."""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from app.bridge import Bridge
from app.controller import AppController
from app.maintenance import export_backup, import_backup
from engine.media_store import MediaStoreError
from engine.queue_store import QueueStateError, QueueStore
from engine.settings import SettingsStore


@pytest.fixture
def controller(tmp_path):
    instance = AppController(SettingsStore(base_dir=tmp_path / "data"))
    try:
        yield instance
    finally:
        instance.close()


def _complete(controller, *, job_id=None, media_id=None):
    if media_id is None:
        source = Path(controller.settings["download_dir"]) / "sample.mp4"
        source.write_bytes(b"isolated-video-fixture")
        media_id = controller.import_files([str(source)])[0].item["id"]
        job_id = controller.enqueue_media([media_id]).created[0]
    job = controller.queue.get_job(job_id)
    assert controller.queue.claim_next("text-fixture")["id"] == job_id
    directory = Path(job["output_dir"]) / job_id
    directory.mkdir(parents=True, exist_ok=True)
    raw, final, segments = (directory / name for name in
                            ("sample_raw.md", "sample.md", "sample_segments.json"))
    raw.write_text("raw fixture", encoding="utf-8")
    final.write_text("final fixture", encoding="utf-8")
    segments.write_text('[{"start":0,"end":1,"text":"fixture"}]', encoding="utf-8")
    manifest = {
        "job_id": job_id, "source_path": job["source_path"], "source_sha256": "fixture",
        "backend": "local", "model": "tiny", "language": "en", "duration_sec": 1.0,
        "raw_path": str(raw), "final_path": str(final), "segments_path": str(segments),
        "content_mode": "raw", "prompt_version": "v1", "created_at": job["created_at"],
        "source_title": "Fixture title", "original_source_title": "Original fixture title",
        "title_status": "done", "title_skill_version": "1", "agent_status": "done",
        "body_enabled_snapshot": True, "body_status": "done", "body_usage_units": 2,
    }
    (directory / "sample_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    controller.queue.save_manifest(manifest, worker_id="text-fixture")
    controller.queue.set_tags(job_id, ["fixture tag"])
    return job_id, media_id, directory, manifest


def _bridge(controller):
    bridge = Bridge(controller, SimpleNamespace(running=False, pid=None),
                    controller.settings_store, lambda: None, lambda: None)
    # No local server, network, real model or user process is involved.
    bridge._media_preview = SimpleNamespace(urls=lambda _: {})
    return bridge


def test_bridge_text_only_deletion_keeps_task_video_download_and_manifest(controller):
    job_id, media_id, directory, manifest = _complete(controller)
    download = controller.downloads.create("https://example.invalid/video", "fixture", "other",
                                           {"transcribe_after_download": True})
    for status in ("resolving", "downloading", "imported", "done"):
        controller.downloads.transition(download["id"], status, media_id=media_id,
                                        job_id=job_id, downloaded_path=manifest["source_path"])
    for name in ("sample.srt", "sample.vtt"):
        (directory / name).write_text("subtitle fixture", encoding="utf-8")
    preserved = {}
    for name in ("audio.wav", "cached-video.mp4", "notes.md", "other.srt", "extra.json"):
        path = directory / name
        preserved[path] = b"unrelated fixture"
        path.write_bytes(preserved[path])
    before_download = controller.downloads.get(download["id"])
    before_media = controller.media.get_media(media_id)
    bridge = _bridge(controller)
    assert bridge.get_documents()[0]["task_id"] == "job:" + job_id
    assert bridge.get_tag_index() == [{"tag": "fixture tag", "count": 1}]

    assert bridge.delete_texts(["job:" + job_id]) == {"ok": True, "removed": [job_id]}
    assert bridge.get_documents() == []
    assert bridge.get_tag_index() == []
    assert [task["task_id"] for task in bridge.get_tasks()] == ["job:" + job_id]
    assert bridge.get_downloaded_videos()["items"][0]["id"] == media_id
    assert controller.media.get_media(media_id) == before_media
    assert controller.downloads.get(download["id"]) == before_download
    job = controller.queue.get_job(job_id)
    assert job["status"] == "done" and job["tags"] == ["fixture tag"]
    assert job["raw_path"] is None and job["final_path"] is None
    kept = controller.queue.get_manifest(job_id)
    assert kept is not None and kept["source_path"] == manifest["source_path"]
    assert kept["raw_path"] == kept["final_path"] == kept["segments_path"] == ""
    assert kept["text_outputs_cleared"] is True
    assert kept["title_skill_version"] == "1" and kept["body_usage_units"] == 2
    for name in ("sample_raw.md", "sample.md", "sample_segments.json", "sample_manifest.json", "sample.srt", "sample.vtt"):
        assert not (directory / name).exists()
    assert directory.is_dir() and Path(manifest["source_path"]).is_file()
    assert all(path.read_bytes() == content for path, content in preserved.items())
    assert controller.delete_texts([job_id, job_id, "missing"]) == []
    reopened = QueueStore(controller.settings["database_path"])
    try:
        assert reopened.get_manifest(job_id) == kept
    finally:
        reopened.close()


@pytest.mark.parametrize("status", ["queued", "running", "retryable"])
def test_active_job_rejects_text_removal_even_with_force(controller, status):
    source = Path(controller.settings["download_dir"]) / "active.mp4"
    source.write_bytes(b"active-fixture")
    media = controller.import_files([str(source)])[0].item["id"]
    job_id = controller.enqueue_media([media]).created[0]
    if status != "queued":
        controller.queue.claim_next("active-fixture")
    if status == "retryable":
        controller.queue.update_status(job_id, "retryable", worker_id="active-fixture")
    cleanup = mock.Mock()
    with pytest.raises(QueueStateError):
        controller.queue.clear_text_outputs(job_id, remove_files=cleanup)
    cleanup.assert_not_called()
    assert controller.delete_texts([job_id], force=True) == []
    result = _bridge(controller).delete_texts(["job:" + job_id])
    assert result["ok"] is False and result["removed"] == [] and result["error"]
    assert controller.queue.get_job(job_id)["status"] == status


@pytest.mark.parametrize("field", ["raw_path", "final_path", "segments_path"])
def test_outside_recorded_text_path_rejects_entire_cleanup(controller, tmp_path, field):
    job_id, _, directory, manifest = _complete(controller)
    outside = tmp_path / ("outside_segments.json" if field == "segments_path" else "outside.md")
    outside.write_text("must survive", encoding="utf-8")
    changed = {**manifest, field: str(outside)}
    controller.queue.replace_manifest(changed)
    before = controller.queue.get_manifest(job_id)
    assert controller.delete_texts([job_id]) == []
    result = _bridge(controller).delete_texts(["job:" + job_id])
    assert result["ok"] is False and result["removed"] == [] and result["error"]
    assert outside.read_text(encoding="utf-8") == "must survive"
    assert (directory / "sample_raw.md").is_file() and (directory / "sample.md").is_file()
    assert controller.queue.get_manifest(job_id) == before


def test_sidecar_cannot_authorize_deletion_outside_job_directory(controller, tmp_path):
    job_id, _, directory, manifest = _complete(controller)
    outside = tmp_path / "outside.md"
    outside.write_text("must survive", encoding="utf-8")
    sidecar = directory / "sample_manifest.json"
    sidecar.write_text(json.dumps({**manifest, "raw_path": str(outside)}), encoding="utf-8")
    assert controller.delete_texts([job_id]) == [job_id]
    assert outside.is_file()
    assert not directory.exists()
    assert controller.queue.get_job(job_id)["raw_path"] is None


def test_symlink_to_other_text_is_not_followed_or_deleted(controller):
    job_id, _, directory, manifest = _complete(controller)
    raw = Path(manifest["raw_path"])
    other = directory / "other.md"
    other.write_text("keep", encoding="utf-8")
    raw.unlink()
    try:
        raw.symlink_to(other)
    except (OSError, NotImplementedError):
        pytest.skip("This Windows fixture cannot create a file symlink")
    assert controller.delete_texts([job_id]) == []
    assert other.read_text(encoding="utf-8") == "keep" and raw.is_symlink()
    assert (directory / "sample.md").is_file()


@pytest.mark.parametrize("field,name", [("raw_path", "audio.wav"), ("segments_path", "extra.json")])
def test_wrong_artifact_type_does_not_delete_arbitrary_file(controller, field, name):
    job_id, _, directory, manifest = _complete(controller)
    protected = directory / name
    protected.write_bytes(b"preserve")
    controller.queue.replace_manifest({**manifest, field: str(protected)})
    assert controller.delete_texts([job_id]) == []
    assert protected.read_bytes() == b"preserve"
    assert (directory / "sample.md").is_file()


def test_stale_manifest_cannot_restore_cleared_text_but_retranscription_can(controller):
    job_id, media_id, _, stale = _complete(controller)
    assert controller.delete_texts([job_id]) == [job_id]
    with pytest.raises(QueueStateError, match="文本已删除"):
        controller.queue.replace_manifest(stale)
    result = controller.retranscribe_jobs([job_id])
    assert len(result.created) == 1 and not result.skipped
    new_id = result.created[0]
    _complete(controller, job_id=new_id, media_id=media_id)
    assert [doc["task_id"] for doc in _bridge(controller).get_documents()] == ["job:" + new_id]
    assert controller.queue.get_job(job_id) is not None
    assert controller.queue.get_manifest(new_id).get("text_outputs_cleared") is None


def test_initial_empty_manifest_is_not_treated_as_user_deleted(controller):
    job_id, _, _, manifest = _complete(controller)
    # Existing legacy empty references are distinct from explicit user clearing.
    with controller.queue._conn:
        controller.queue._conn.execute("UPDATE manifests SET raw_path='', final_path='' WHERE job_id=?", (job_id,))
        controller.queue._conn.execute("UPDATE jobs SET raw_path=NULL, final_path=NULL WHERE id=?", (job_id,))
    controller.queue.replace_manifest(manifest)
    assert controller.queue.get_job(job_id)["raw_path"] == manifest["raw_path"]


def test_cleanup_exception_rolls_back_database_references(controller):
    job_id, _, _, manifest = _complete(controller)
    before = controller.queue.get_job(job_id)
    with pytest.raises(OSError, match="fixture failure"):
        controller.queue.clear_text_outputs(
            job_id, remove_files=mock.Mock(side_effect=OSError("fixture failure")))
    assert controller.queue.get_job(job_id) == before
    assert controller.queue.get_manifest(job_id)["raw_path"] == manifest["raw_path"]
    assert not controller.queue._conn.in_transaction


def test_missing_text_files_can_clear_references_without_removing_the_job(controller):
    job_id, _, directory, manifest = _complete(controller)
    for key in ("raw_path", "final_path", "segments_path"):
        Path(manifest[key]).unlink()
    assert controller.delete_texts([job_id]) == [job_id]
    assert controller.queue.get_manifest(job_id)["text_outputs_cleared"] is True
    assert controller.queue.get_job(job_id)["status"] == "done"
    assert not directory.exists()


def test_text_cleanup_uses_recorded_output_root_and_cleans_generated_translations(controller, tmp_path):
    from app.translation_service import translation_paths

    job_id, _, directory, manifest = _complete(controller)
    translated = [path for lang in ("zh", "en")
                  for path in translation_paths(Path(manifest["raw_path"]), lang)]
    for path in translated:
        path.write_text("fixture translation", encoding="utf-8")
    controller.queue.replace_manifest({**manifest, "translation_status": "done",
        "translation_path": str(translated[-2]), "translation_segments_path": str(translated[-1])})
    replacement_root = tmp_path / "new-outputs"
    replacement_root.mkdir()
    controller.settings["output_dir"] = str(replacement_root)
    bridge = _bridge(controller)
    reader = bridge.get_reader("job:" + job_id)
    assert reader["ok"] and Path(reader["path"]).parent == directory
    saved = bridge.update_reader_text("job:" + job_id, "final", "edited", reader["revision"])
    assert saved["ok"] and Path(reader["path"]).read_text(encoding="utf-8") == "edited"
    if os.name == "nt":
        with mock.patch("app.bridge.subprocess.Popen") as explorer:
            assert bridge.open_path(reader["path"])["ok"]
            explorer.assert_called_once_with(["explorer.exe", "/select,", reader["path"]])
    assert controller.delete_texts([job_id]) == [job_id]
    assert not directory.exists()
    assert Path(manifest["source_path"]).is_file()
    kept = controller.queue.get_manifest(job_id)
    assert kept["text_outputs_cleared"] is True
    assert not any(key.startswith("translation_") for key in kept)


def test_text_cleanup_removes_registered_and_historical_translation_runs_only(controller):
    from app.translation_service import translation_paths

    job_id, _, directory, manifest = _complete(controller)
    generated = [path for lang in ("zh", "en") for run_id in ("a" * 24, "b" * 32)
                 for path in translation_paths(Path(manifest["raw_path"]), lang, run_id)]
    registered = [directory / "explicit-translation.md", directory / "explicit-translation.segments.json"]
    kept = [directory / "sample_译文_en.notes.md", directory / ("sample_译文_en." + "c" * 25 + ".md"),
            directory / ("other_译文_en." + "d" * 24 + ".md")]
    for path in generated + registered + kept:
        path.write_text("fixture translation or personal notes", encoding="utf-8")
    controller.queue.replace_manifest({**manifest, "translation_status": "done",
        "translation_path": str(registered[0]), "translation_segments_path": str(registered[1])})
    assert controller.delete_texts([job_id]) == [job_id]
    assert not any(path.exists() for path in generated + registered)
    assert all(path.read_text(encoding="utf-8") == "fixture translation or personal notes" for path in kept)
    assert controller.queue.get_manifest(job_id)["text_outputs_cleared"] is True


def test_batch_article_run_cleanup_keeps_personal_files_and_other_output_roots(controller, tmp_path):
    from app.data_health import _known_output_contents

    job_id, _, directory, manifest = _complete(controller)
    generated = [directory / ("旧标题_文章.foilpage-" + "a" * 32 + ".md"),
                 directory / ("旧版标题.foilpage-" + "b" * 24 + ".md"),
                 directory / ("新标题_文章.foilpage-" + "c" * 32 + ".md")]
    for path in generated:
        path.write_text("published article run", encoding="utf-8")
    controller.queue.replace_manifest({**manifest, "final_path": str(generated[-1])})
    assert _known_output_contents(directory, job_id, False)
    preserved = [directory / "个人笔记.md", directory / ("notes." + "d" * 32 + ".md"),
                 directory / ("文章.foilpage-" + "e" * 25 + ".md"),
                 tmp_path / ("其他目录_文章.foilpage-" + "a" * 32 + ".md")]
    for path in preserved:
        path.write_text("personal file", encoding="utf-8")
    assert not _known_output_contents(directory, job_id, False)
    assert controller.delete_texts([job_id]) == [job_id]
    assert not any(path.exists() for path in generated)
    assert all(path.read_text(encoding="utf-8") == "personal file" for path in preserved)


def test_restored_output_can_be_removed_even_when_sidecar_keeps_old_paths(controller, tmp_path):
    job_id, _, directory, _ = _complete(controller)
    archive = tmp_path / "to-restore.zip"
    assert export_backup(controller, controller.settings_store, str(archive))["ok"]
    restored = AppController(SettingsStore(base_dir=tmp_path / "restored-outputs"))
    try:
        result = import_backup(restored, restored.settings_store, str(archive))
        assert result["ok"], result
        restored_dir = Path(restored.settings["output_dir"]) / job_id
        sidecar = json.loads((restored_dir / "sample_manifest.json").read_text(encoding="utf-8"))
        assert Path(sidecar["raw_path"]).parent == directory
        assert restored.delete_texts([job_id]) == [job_id]
        assert not restored_dir.exists()
        assert (directory / "sample.md").is_file()
    finally:
        restored.close()


def test_bridge_rejects_deletion_while_batch_agent_is_writing(controller):
    job_id, _, directory, _ = _complete(controller)
    bridge = _bridge(controller)
    bridge._batch_agent["running"] = True
    result = bridge.delete_texts(["job:" + job_id])
    assert result["ok"] is False and "正在整理" in result["error"]
    assert (directory / "sample.md").is_file()
    assert controller.queue.get_job(job_id)["final_path"]


def test_bridge_partial_text_deletion_reports_failure_and_keeps_removed_ids(controller, tmp_path):
    good_id, _, _, _ = _complete(controller)
    bad_id, _, bad_dir, manifest = _complete(controller)
    protected = tmp_path / "outside.md"
    protected.write_text("must survive", encoding="utf-8")
    controller.queue.replace_manifest({**manifest, "raw_path": str(protected)})
    result = _bridge(controller).delete_texts(["job:" + good_id, "job:" + good_id, "job:" + bad_id])
    assert result["ok"] is False and result["removed"] == [good_id] and result["error"]
    assert controller.queue.get_job(good_id)["raw_path"] is None
    assert controller.queue.get_job(bad_id)["raw_path"] == str(protected)
    assert (bad_dir / "sample.md").is_file() and protected.is_file()


def test_backup_restore_preserves_cleared_registration_without_reviving_library(controller, tmp_path):
    job_id, media_id, directory, manifest = _complete(controller)
    assert controller.delete_texts([job_id]) == [job_id]
    archive = tmp_path / "cleared-text-backup.zip"
    assert export_backup(controller, controller.settings_store, str(archive))["ok"]
    restored = AppController(SettingsStore(base_dir=tmp_path / "restored"))
    try:
        result = import_backup(restored, restored.settings_store, str(archive))
        assert result["ok"], result
        assert restored.queue.get_job(job_id)["status"] == "done"
        kept = restored.queue.get_manifest(job_id)
        assert kept["text_outputs_cleared"] is True
        assert kept["raw_path"] == kept["final_path"] == kept["segments_path"] == ""
        assert restored.media.get_media(media_id) is not None
        assert _bridge(restored).get_documents() == []
        assert _bridge(restored).get_tag_index() == []
        with pytest.raises(QueueStateError, match="文本已删除"):
            restored.queue.replace_manifest(manifest)
        sidecar = Path(restored.settings["output_dir"]) / job_id / "sample_manifest.json"
        assert not sidecar.exists()
    finally:
        restored.close()


def test_parallel_deletion_is_single_flight(controller):
    job_id, _, _, _ = _complete(controller)
    start = threading.Barrier(3)
    results = []
    def remove():
        start.wait(timeout=3)
        results.append(controller.delete_texts([job_id]))
    threads = [threading.Thread(target=remove) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=3)
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert sorted(results, key=len) == [[], [job_id]]


def test_separate_connection_cannot_replace_manifest_after_clear_transaction(controller):
    job_id, _, _, stale = _complete(controller)
    other = QueueStore(controller.settings["database_path"])
    entered, finish, attempting = threading.Event(), threading.Event(), threading.Event()
    other._conn.set_trace_callback(lambda sql: attempting.set()
                                  if sql.strip().startswith(("BEGIN", "UPDATE manifests")) else None)
    results, failures = [], []
    def cleanup(job, manifest):
        entered.set()
        assert finish.wait(3)
        return controller._remove_job_text_outputs(job, manifest)
    def clear():
        try:
            results.append(controller.queue.clear_text_outputs(job_id, remove_files=cleanup))
        except Exception as exc:
            failures.append(exc)
    def replace():
        try:
            other.replace_manifest(stale)
        except Exception as exc:
            failures.append(exc)
    clear_thread = threading.Thread(target=clear)
    replace_thread = threading.Thread(target=replace)
    try:
        clear_thread.start()
        assert entered.wait(3)
        replace_thread.start()
        assert attempting.wait(3)
        finish.set()
        clear_thread.join(timeout=3)
        replace_thread.join(timeout=3)
        assert not clear_thread.is_alive() and not replace_thread.is_alive()
        assert results == [True]
        assert len(failures) == 1 and isinstance(failures[0], QueueStateError)
        assert other.get_manifest(job_id)["text_outputs_cleared"] is True
        assert other.get_job(job_id)["raw_path"] is None
    finally:
        finish.set()
        clear_thread.join(timeout=3)
        if replace_thread.ident is not None:
            replace_thread.join(timeout=3)
        other.close()


def test_save_finishes_before_delete_and_cannot_recreate_cleared_asset(controller):
    job_id, _, directory, _ = _complete(controller)
    writing, finish = threading.Event(), threading.Event()
    real_write = controller._atomic_write_text
    def paused_write(path, text):
        if path.name == "sample.md":
            writing.set()
            assert finish.wait(3)
        real_write(path, text)
    errors, removed = [], []
    def save():
        try:
            controller.update_document_text(job_id, "final", "edited fixture")
        except Exception as exc:
            errors.append(exc)
    with mock.patch.object(controller, "_atomic_write_text", side_effect=paused_write):
        writer = threading.Thread(target=save)
        writer.start()
        assert writing.wait(3)
        deleter = threading.Thread(target=lambda: removed.extend(controller.delete_texts([job_id])))
        deleter.start()
        finish.set()
        writer.join(timeout=3)
        deleter.join(timeout=3)
    assert not writer.is_alive() and not deleter.is_alive() and not errors
    assert removed == [job_id] and not (directory / "sample.md").exists()
    with pytest.raises(ValueError, match="文本文件不可用"):
        controller.update_document_text(job_id, "final", "late save")
    assert not (directory / "sample.md").exists()


def _file_hashes(directory):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.iterdir() if path.is_file()}


@pytest.mark.parametrize("failure", ["second_rename", "sidecar_rename", "sql_update", "sql_commit"])
def test_failed_text_transaction_restores_exact_files_sidecar_and_references(controller, failure):
    job_id, _, directory, _ = _complete(controller)
    (directory / "sample.srt").write_text("subtitle", encoding="utf-8")
    (directory / "audio.wav").write_bytes(b"preserved-audio")
    before_files = _file_hashes(directory)
    before_job = controller.queue.get_job(job_id)
    before_manifest = controller.queue.get_manifest(job_id)
    rename = Path.replace
    moves = 0
    def fail_move(path, target):
        nonlocal moves
        if Path(target).parent.name.startswith(".text-delete-"):
            moves += 1
            if ((failure == "second_rename" and moves == 2)
                    or (failure == "sidecar_rename" and path.name.endswith("_manifest.json"))):
                raise OSError("fixture second rename failure")
        return rename(path, target)
    if failure == "sql_update":
        controller.queue._conn.execute("""CREATE TEMP TRIGGER fail_text_clear
            BEFORE UPDATE OF raw_path ON manifests
            BEGIN SELECT RAISE(ABORT, 'fixture SQL update failure'); END""")
    if failure == "sql_commit":
        controller.queue._conn.set_authorizer(
            lambda operation, first, second, db, source:
            sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_TRANSACTION and first == "COMMIT"
            else sqlite3.SQLITE_OK)
    try:
        with mock.patch.object(Path, "replace", autospec=True, side_effect=fail_move):
            if failure.startswith("sql_"):
                with pytest.raises(sqlite3.DatabaseError):
                    controller.delete_texts([job_id])
            else:
                assert controller.delete_texts([job_id]) == []
    finally:
        controller.queue._conn.set_authorizer(None)
    assert _file_hashes(directory) == before_files
    assert controller.queue.get_job(job_id) == before_job
    assert controller.queue.get_manifest(job_id) == before_manifest
    assert not list(directory.glob(".text-delete-*"))
    assert not controller.queue._conn.in_transaction


def test_final_cleanup_failure_keeps_controlled_stage_and_reports_warning(controller, caplog, tmp_path):
    job_id, _, directory, _ = _complete(controller)
    unlink = Path.unlink
    def blocked_stage_unlink(path, *args, **kwargs):
        if path.parent.name.startswith(".text-delete-"):
            raise OSError("fixture final cleanup failure")
        return unlink(path, *args, **kwargs)
    with mock.patch.object(Path, "unlink", autospec=True, side_effect=blocked_stage_unlink), \
         caplog.at_level("WARNING", logger="app.controller"):
        assert controller.delete_texts([job_id]) == [job_id]
    stages = list(directory.glob(".deletion-cache-*"))
    assert len(stages) == 1 and stages[0].resolve().parent == directory.resolve()
    assert (stages[0] / "sample_raw.md").is_file()
    assert (stages[0] / "sample_manifest.json").is_file()
    assert "暂存尚未清理" in caplog.text
    assert controller.queue.get_manifest(job_id)["text_outputs_cleared"] is True
    assert _bridge(controller).get_documents() == []
    archive = tmp_path / "cleared-staging-backup.zip"
    assert export_backup(controller, controller.settings_store, str(archive))["ok"]
    with zipfile.ZipFile(archive) as backup:
        assert not any(".deletion-cache-" in name for name in backup.namelist())
        assert not any(name.endswith(("/sample.md", "/sample_raw.md")) for name in backup.namelist())


def test_agent_article_deletion_removes_initial_engine_copy_without_touching_notes(controller, tmp_path):
    from app.agent_service import _write_manifest_file
    job_id, _, directory, manifest = _complete(controller)
    article = directory / "sample_文章.md"
    article.write_text("generated article fixture", encoding="utf-8")
    notes = directory / "notes.md"
    notes.write_text("arbitrary retained notes", encoding="utf-8")
    updated = {**manifest, "final_path": str(article)}
    _write_manifest_file(updated)
    controller.queue.replace_manifest(updated)
    assert controller.delete_texts([job_id]) == [job_id]
    assert not article.exists()
    assert not (directory / "sample.md").exists()
    assert not (directory / "sample_raw.md").exists()
    assert notes.read_text(encoding="utf-8") == "arbitrary retained notes"
    archive = tmp_path / "agent-cleared.zip"
    assert export_backup(controller, controller.settings_store, str(archive))["ok"]
    with zipfile.ZipFile(archive) as backup:
        names = set(backup.namelist())
        assert not any(name.endswith(("/sample.md", "/sample_raw.md", "/sample_文章.md")) for name in names)
        assert any(name.endswith("/notes.md") for name in names)


def _register_managed_download(controller, job_id, media_id, manifest):
    video = Path(manifest["source_path"])
    cover = video.with_suffix(".jpg")
    cover.write_bytes(b"fixture managed cover")
    download = controller.downloads.create("https://example.invalid/managed", "Managed fixture", "other",
                                           {"managed_download_root": str(video.parent)})
    for status in ("resolving", "downloading", "imported", "done"):
        controller.downloads.transition(download["id"], status, media_id=media_id,
                                        job_id=job_id, downloaded_path=str(video), cover_path=str(cover))
    return download["id"], video, cover


@pytest.mark.parametrize("remove_records", [False, True])
def test_library_removal_cleans_text_media_cover_and_process_files(controller, remove_records):
    job_id, media_id, directory, manifest = _complete(controller)
    download_id, video, cover = _register_managed_download(controller, job_id, media_id, manifest)
    (directory / ".sample_raw-a1b2c3d4.tmp").write_text("interrupted atomic write", encoding="utf-8")
    original = video.parent / "other.mp4"
    original.write_bytes(b"neighbor to retain")
    bridge = _bridge(controller)
    assert bridge.delete_library_items(["job:" + job_id], remove_records) == {"ok": True, "removed": [job_id]}
    assert bridge.get_documents() == []
    for path in (directory, video, cover):
        assert not path.exists()
    assert original.is_file()
    with pytest.raises(MediaStoreError):
        controller.media.get_media(media_id)
    if remove_records:
        assert controller.queue.get_job(job_id) is None
        assert controller.queue.get_manifest(job_id) is None
        assert controller.downloads.get(download_id) is None
    else:
        assert controller.queue.get_job(job_id)["status"] == "done"
        assert controller.queue.get_manifest(job_id)["text_outputs_cleared"] is True
        download = controller.downloads.get(download_id)
        assert download["job_id"] == job_id and download["status"] == "done"
        assert download["media_id"] is download["downloaded_path"] is download["cover_path"] is None


def test_library_removal_preserves_unregistered_media_even_in_current_download_folder(controller):
    job_id, media_id, directory, manifest = _complete(controller)
    protected = directory / "notes.md"
    protected.write_text("my own notes", encoding="utf-8")
    assert controller.delete_library_items([job_id], remove_records=True) == [job_id]
    assert Path(manifest["source_path"]).is_file()
    assert controller.media.get_media(media_id)["id"] == media_id
    assert list(directory.iterdir()) == [protected]
    assert controller.queue.get_job(job_id) is None


def test_removing_first_of_shared_library_items_preserves_download_ownership(controller):
    first, media_id, _, manifest = _complete(controller)
    download_id, video, cover = _register_managed_download(controller, first, media_id, manifest)
    second = controller.retranscribe_jobs([first]).created[0]
    _complete(controller, job_id=second, media_id=media_id)
    assert controller.delete_library_items([first], remove_records=True) == [first]
    assert controller.downloads.get(download_id)["job_id"] == second
    assert video.is_file() and cover.is_file()
    assert controller.delete_library_items([second], remove_records=True) == [second]
    assert not video.exists() and not cover.exists()
    assert controller.downloads.get(download_id) is None


def test_library_removal_rolls_back_all_paths_and_registrations_if_video_is_in_use(controller):
    job_id, media_id, directory, manifest = _complete(controller)
    download_id, video, cover = _register_managed_download(controller, job_id, media_id, manifest)
    before = _file_hashes(directory)
    rename = Path.replace

    def locked(path, target):
        if path == video:
            raise PermissionError("synthetic sharing violation")
        return rename(path, target)

    with mock.patch.object(Path, "replace", autospec=True, side_effect=locked):
        result = _bridge(controller).delete_library_items(["job:" + job_id], True)
    assert result["ok"] is False and result["removed"] == []
    assert _file_hashes(directory) == before
    assert set(path.name for path in directory.iterdir()) == before.keys()
    assert video.is_file() and cover.is_file()
    assert controller.queue.get_job(job_id)["final_path"] == manifest["final_path"]
    assert controller.downloads.get(download_id)["media_id"] == media_id
    assert controller.media.get_media(media_id)["id"] == media_id
    assert controller.delete_library_items([job_id], remove_records=True) == [job_id]
