# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""数据自检/备份迁移/诊断包 测试（底二层）。"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path

import pytest

from app.controller import AppController
from app.data_health import (
    orphan_download_files,
    orphan_output_dirs,
    reconcile,
    stale_browser_downloads,
)
from app.maintenance import _redact_line, export_backup, export_diagnostics, import_backup
from engine.settings import SettingsStore
from backup_fixtures import refresh_backup_checksums
from test_queue_store import _job, _manifest


def _make_controller(temp: str) -> AppController:
    return AppController(SettingsStore(base_dir=Path(temp) / "data"))


def _seed_stale_download(controller) -> str:
    item = controller.downloads.create(
        "https://www.douyin.com/video/1234567890123456789",
        "悬空任务",
        "douyin",
        {"transcribe_after_download": False},
    )
    controller.downloads._conn.execute(
        "UPDATE download_jobs SET updated_at=? WHERE id=?",
        ("2020-01-01 00:00:00", item["id"]),
    )
    controller.downloads._conn.commit()
    return item["id"]


def test_stale_browser_download_detected_and_fixed():
    with tempfile.TemporaryDirectory(prefix="health_stale_") as temp:
        controller = _make_controller(temp)
        try:
            download_id = _seed_stale_download(controller)
            stale = stale_browser_downloads(controller)
            assert stale and stale[0]["id"] == download_id
            report = reconcile(controller)
            assert report["ok"] is True
            assert report["stale_fixed"][0]["id"] == download_id
            current = controller.downloads.get(download_id)
            assert current["status"] == "failed"
            assert current["error"]["code"] == "browser_timeout"
        finally:
            controller.close()


def test_stale_queued_download_converged_as_retryable():
    """2026-09-27 实机：约 20 条抖音任务停在 queued 永不收敛（应用未运行时
    提交、浏览器通道未启动）。超时后应收敛为 queue_timeout 可重试失败。"""
    with tempfile.TemporaryDirectory(prefix="health_queued_") as temp:
        controller = _make_controller(temp)
        try:
            item = controller.downloads.create(
                "https://www.douyin.com/video/1234567890123456789",
                "僵尸任务",
                "douyin",
                {"transcribe_after_download": False},
            )
            controller.downloads._conn.execute(
                "UPDATE download_jobs SET status='queued', updated_at=? WHERE id=?",
                ("2020-01-01 00:00:00", item["id"]),
            )
            controller.downloads._conn.commit()
            stale = stale_browser_downloads(controller)
            assert [s["id"] for s in stale] == [item["id"]]
            report = reconcile(controller)
            assert report["stale_fixed"][0]["id"] == item["id"]
            current = controller.downloads.get(item["id"])
            assert current["status"] == "failed"
            assert current["error"]["code"] == "queue_timeout"
            assert current["error"]["retryable"] is True
        finally:
            controller.close()


def test_fresh_queued_download_not_converged():
    """刚提交的 queued 任务不得被误收敛（阈值 24 小时）。"""
    with tempfile.TemporaryDirectory(prefix="health_queued_fresh_") as temp:
        controller = _make_controller(temp)
        try:
            item = controller.downloads.create(
                "https://www.douyin.com/video/1234567890123456789",
                "新任务",
                "douyin",
                {"transcribe_after_download": False},
            )
            controller.downloads._conn.execute(
                "UPDATE download_jobs SET status='queued' WHERE id=?",
                (item["id"],),
            )
            controller.downloads._conn.commit()
            assert stale_browser_downloads(controller) == []
        finally:
            controller.close()


def test_orphan_output_dirs_detected_and_purge_guarded():
    with tempfile.TemporaryDirectory(prefix="health_orphan_") as temp:
        controller = _make_controller(temp)
        try:
            out_root = Path(controller.settings["output_dir"])
            orphan_dir = out_root / "123456abcdef"
            orphan_dir.mkdir(parents=True, exist_ok=True)
            (orphan_dir / "sample_raw.md").write_text("x", encoding="utf-8")
            (orphan_dir / "sample_manifest.json").write_text(
                json.dumps(_manifest(orphan_dir.name)), encoding="utf-8"
            )
            unrelated = out_root / "saved-notes"
            unrelated.mkdir()
            (unrelated / "notes.md").write_text("保留", encoding="utf-8")
            orphans = orphan_output_dirs(controller)
            assert [o["name"] for o in orphans] == [orphan_dir.name]
            assert controller.purge_orphan_output(str(orphan_dir)) is True
            assert not orphan_dir.exists()
            assert controller.purge_orphan_output(str(unrelated)) is False
            assert controller.purge_orphan_output(str(out_root)) is False
            assert controller.purge_orphan_output(str(out_root.parent)) is False
        finally:
            controller.close()


def test_cleanup_can_retry_an_interrupted_directory_removal(tmp_path, monkeypatch):
    controller = _make_controller(tmp_path)
    try:
        folder = Path(controller.settings["output_dir"]) / "123456abcdef"
        folder.mkdir()
        (folder / "sample_manifest.json").write_text(json.dumps(_manifest(folder.name)), encoding="utf-8")
        (folder / "sample_segments.json").write_text("[]", encoding="utf-8")

        def partial_remove(path):
            (path / "sample_manifest.json").unlink()
            raise PermissionError("fixture: remaining file in use")

        with monkeypatch.context() as patch:
            patch.setattr("app.data_health.shutil.rmtree", partial_remove)
            assert controller.purge_orphan_output(str(folder)) is False
        candidates = orphan_output_dirs(controller)
        assert len(candidates) == 1 and candidates[0]["kind"] == "deletion_cache"
        assert controller.purge_orphan_output(candidates[0]["path"]) is True
        assert orphan_output_dirs(controller) == []
    finally:
        controller.close()


def test_orphan_download_files_detected():
    with tempfile.TemporaryDirectory(prefix="health_orphan_dl_") as temp:
        controller = _make_controller(temp)
        try:
            dl_root = Path(controller.settings["download_dir"])
            dl_root.mkdir(parents=True, exist_ok=True)
            known = dl_root / "browser-media-1660000000001.mp4"
            orphan = dl_root / "browser-media-1660000000002.mp4"
            known.write_bytes(b"k")
            orphan.write_bytes(b"o")
            unrelated = dl_root / "family.mp4"
            unrelated.write_bytes(b"u")
            os.utime(known, (1660000000, 1660000000))
            os.utime(orphan, (1660000000, 1660000000))
            item = controller.downloads.create(
                "https://www.douyin.com/video/9876543210987654321",
                "已知任务",
                "douyin",
                {"transcribe_after_download": False},
            )
            controller.downloads.transition(item["id"], "resolving")
            controller.downloads.transition(
                item["id"], "downloading", progress=0.0, downloaded_path=str(known),
            )
            names = {o["name"] for o in orphan_download_files(controller)}
            assert names == {orphan.name}
            assert controller.purge_orphan_download(str(known)) is False
            assert controller.purge_orphan_download(str(unrelated)) is False
            assert controller.purge_orphan_download(str(orphan)) is True
            assert not orphan.exists()
            assert controller.purge_orphan_download(str(dl_root)) is False
            assert controller.purge_orphan_download(str(dl_root.parent)) is False
        finally:
            controller.close()


def test_cleanup_rechecks_live_references_and_historical_text_clear(tmp_path):
    controller = _make_controller(tmp_path)
    try:
        root = Path(controller.settings["output_dir"])
        media = Path(controller.settings["download_dir"]) / "browser-media-1660000000003.mp4"
        media.write_bytes(b"media")
        os.utime(media, (1660000000, 1660000000))
        assert [o["path"] for o in orphan_download_files(controller)] == [str(media)]
        item = _job(status="queued", job_id="123456abcdef")
        item.update(output_dir=str(root), source_path=str(media))
        controller.queue.enqueue(item)
        assert controller.purge_orphan_download(str(media)) is False
        controller.queue.claim_next("fixture")
        job_dir = root / item["id"]
        job_dir.mkdir()
        sidecar = job_dir / "sample_manifest.json"
        manifest = {**_manifest(item["id"]), "source_path": str(media),
                    "raw_path": str(job_dir / "sample_raw.md"),
                    "final_path": str(job_dir / "sample.md")}
        controller.queue.save_manifest(manifest, worker_id="fixture")
        controller.queue.clear_text_outputs(item["id"])
        sidecar.write_text(json.dumps(manifest), encoding="utf-8")
        (job_dir / "sample_segments.json").write_text("[]", encoding="utf-8")
        stage = job_dir / ".text-delete-abcdefgh"
        stage.mkdir()
        (stage / "sample_raw.md").write_text("旧版清理暂存", encoding="utf-8")
        unrelated = job_dir / "keep.md"
        unrelated.write_text("保留", encoding="utf-8")
        assert controller.purge_orphan_output(str(job_dir)) is False
        unrelated.unlink()
        candidates = orphan_output_dirs(controller)
        assert [(entry["name"], entry["kind"]) for entry in candidates] == [(item["id"], "cleared_output")]
        assert controller.purge_orphan_output(str(job_dir)) is True
        assert not job_dir.exists()
        assert media.exists() and controller.queue.get_job(item["id"])
    finally:
        controller.close()


def test_cleanup_preserves_live_subtitles_and_rollback_directories(tmp_path):
    controller = _make_controller(tmp_path)
    try:
        root = Path(controller.settings["output_dir"])
        live = _job(job_id="123456abcdef")
        live["output_dir"] = str(root)
        controller.queue.enqueue(live)
        controller.queue.claim_next("fixture")
        old = root / (".123456abcdef-old-" + "a" * 32)
        old.mkdir()
        os.utime(old, (1660000000, 1660000000))
        running = root / ".123456abcdef-abcdefgh"
        running.mkdir()
        os.utime(running, (1660000000, 1660000000))
        assert orphan_output_dirs(controller) == []

        dangling = root / "a1b2c3d4e5f6"
        dangling.mkdir()
        subtitles = dangling / "sample_segments.json"
        subtitles.write_bytes(b"[]")
        (dangling / "sample_manifest.json").write_text(
            json.dumps(_manifest(dangling.name)), encoding="utf-8"
        )
        assert controller.purge_orphan_output(str(dangling / "sample_segments.json")) is False
        assert [row["path"] for row in orphan_output_dirs(controller)] == [str(dangling)]
        manifest = {**_manifest(live["id"]), "segments_path": str(subtitles)}
        controller.queue.save_manifest(manifest, worker_id="fixture")
        assert controller.purge_orphan_output(str(dangling)) is False
        assert controller.purge_orphan_output(str(old)) is False

        job_dir = root / live["id"]
        job_dir.mkdir()
        recovery = job_dir / ".text-delete-abcdefgh"
        recovery.mkdir()
        (recovery / "sample_raw.md").write_bytes(b"recovery")
        cache = job_dir / (".deletion-cache-123456abcdef-" + "b" * 32)
        cache.mkdir()
        (cache / "sample.md").write_bytes(b"cache")
        assert controller.purge_orphan_output(str(recovery)) is False
        assert controller.purge_orphan_output(str(cache)) is True
        assert recovery.exists() and old.exists() and subtitles.exists()
    finally:
        controller.close()


def test_cleanup_finds_historical_output_when_text_was_removed_outside_app(tmp_path):
    controller = _make_controller(tmp_path)
    try:
        root = Path(controller.settings["output_dir"])
        job = _job(job_id="123456abcdef")
        job.update(output_dir=str(root), source_path=str(tmp_path / "removed-video.mp4"))
        controller.queue.enqueue(job)
        controller.queue.claim_next("fixture")
        folder = root / job["id"]
        folder.mkdir()
        manifest = {**_manifest(job["id"]), "source_path": job["source_path"],
                    "raw_path": str(folder / "sample_raw.md"),
                    "final_path": str(folder / "sample.md"),
                    "segments_path": str(folder / "sample_segments.json")}
        controller.queue.save_manifest(manifest, worker_id="fixture")
        (folder / "sample_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (folder / "sample_segments.json").write_bytes(b"[]")
        recoverable = folder / ".text-delete-abcdefgh"
        recoverable.mkdir()
        (recoverable / "sample_raw.md").write_bytes(b"original")
        assert orphan_output_dirs(controller) == []
        (recoverable / "sample_raw.md").unlink()
        recoverable.rmdir()
        alternate = folder / "sample_raw.txt"
        alternate.write_bytes(b"draft")
        assert orphan_output_dirs(controller) == []
        alternate.unlink()
        assert [(row["path"], row["kind"]) for row in orphan_output_dirs(controller)] == [
            (str(folder), "cleared_output"),
        ]
        assert controller.purge_orphan_output(str(folder)) is True
        assert not folder.exists()
    finally:
        controller.close()


def test_export_backup_zip_contains_core_and_redacts_secrets():
    with tempfile.TemporaryDirectory(prefix="backup_export_") as temp:
        controller = _make_controller(temp)
        try:
            controller.save_settings({
                "local_api_token": "secret-token",
                "agent_api_key": "secret-key",
                "license_token": "secret-license",
            })
            dest = Path(temp) / "out" / "backup.zip"
            result = export_backup(controller, controller.settings_store, str(dest))
            assert result["ok"] is True, result
            with zipfile.ZipFile(dest) as zf:
                names = set(zf.namelist())
                assert {"settings.json", "library.db", "backup_manifest.json", "model_inventory.json"} <= names
                settings = json.loads(zf.read("settings.json").decode("utf-8"))
                assert settings["local_api_token"] == ""
                assert settings["agent_api_key"] == ""
                # license_token 与另两个密钥同为 DPAPI 加密字段，导出必须一并脱敏。
                assert settings["license_token"] == ""
        finally:
            controller.close()


def test_import_backup_restores_and_creates_pre_backup():
    with tempfile.TemporaryDirectory(prefix="backup_import_") as temp:
        controller = _make_controller(temp)
        try:
            controller.save_settings({"theme": "paper"})
            backup_path = Path(temp) / "backup.zip"
            result = export_backup(controller, controller.settings_store, str(backup_path))
            assert result["ok"] is True
            controller.save_settings({"theme": "dark", "font_scale": 1.3})
            imported = import_backup(controller, controller.settings_store, str(backup_path))
            assert imported["ok"] is True, imported
            assert imported["restart_required"] is True
            assert Path(imported["pre_backup"]).is_file()
            reloaded = SettingsStore(base_dir=Path(temp) / "data").load()
            assert reloaded["theme"] == "paper"
        finally:
            controller.close()


def test_import_backup_rejects_non_zip():
    with tempfile.TemporaryDirectory(prefix="backup_bad_") as temp:
        controller = _make_controller(temp)
        try:
            bad = Path(temp) / "not-a-zip.txt"
            bad.write_text("hello", encoding="utf-8")
            result = import_backup(controller, controller.settings_store, str(bad))
            assert result["ok"] is False
        finally:
            controller.close()


def test_export_diagnostics_zip_contains_manifest_and_settings():
    with tempfile.TemporaryDirectory(prefix="diag_export_") as temp:
        controller = _make_controller(temp)
        try:
            controller.save_settings({"license_token": "secret-license"})
            dest = Path(temp) / "diag.zip"
            result = export_diagnostics(controller, controller.settings_store, str(dest), app_version="0.7.22")
            assert result["ok"] is True, result
            with zipfile.ZipFile(dest) as zf:
                names = set(zf.namelist())
                assert "diagnostic_manifest.json" in names
                assert "settings.json" in names
                assert "library_stats.json" in names
                settings = json.loads(zf.read("settings.json").decode("utf-8"))
                assert settings["license_token"] == ""
                manifest = json.loads(zf.read("diagnostic_manifest.json").decode("utf-8"))
                assert manifest["app_version"] == "0.7.22"
                assert manifest["log_redaction"], "manifest must declare log redaction"
        finally:
            controller.close()


def test_restore_updates_live_wal_readers_and_preserves_local_credentials(tmp_path):
    controller = _make_controller(str(tmp_path))
    try:
        controller.queue.enqueue(_job("keep"))
        controller.queue.request_cancel("keep")
        backup = tmp_path / "backup.zip"
        assert export_backup(controller, controller.settings_store, str(backup))["ok"]
        controller.queue.enqueue(_job("later"))
        controller.queue.request_cancel("later")
        controller.save_settings({"agent_api_key": "local-key", "local_api_token": "local-token"})
        result = import_backup(controller, controller.settings_store, str(backup))
        assert result["ok"], result
        assert controller.queue.get_job("keep")["status"] == "cancelled"
        assert controller.queue.get_job("later") is None
        saved = controller.settings_store.load()
        assert saved["agent_api_key"] == "local-key"
        assert saved["local_api_token"] == "local-token"
    finally:
        controller.close()
    reopened = _make_controller(str(tmp_path))
    try:
        assert reopened.queue.get_job("keep")
        assert reopened.queue.get_job("later") is None
    finally:
        reopened.close()


def test_invalid_database_does_not_apply_settings_or_text(tmp_path):
    controller = _make_controller(str(tmp_path))
    try:
        controller.save_settings({"theme": "dark"})
        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("settings.json", json.dumps({**controller.settings, "theme": "paper"}))
            archive.writestr("library.db", b"not sqlite")
            archive.writestr("output/overwrite.md", "unexpected")
        result = import_backup(controller, controller.settings_store, str(bad))
        assert not result["ok"]
        assert Path(result["pre_backup"]).exists()
        assert controller.settings_store.load()["theme"] == "dark"
        assert not (Path(controller.settings["output_dir"]) / "overwrite.md").exists()
        assert controller.queue.list_jobs() == []
    finally:
        controller.close()


def test_restore_refuses_active_current_and_archived_tasks(tmp_path):
    controller = _make_controller(str(tmp_path))
    try:
        controller.queue.enqueue(_job("unfinished"))
        archive = tmp_path / "active.zip"
        assert export_backup(controller, controller.settings_store, str(archive))["ok"]
        result = import_backup(controller, controller.settings_store, str(archive))
        assert not result["ok"] and "进行中" in result["error"]
        controller.queue.request_cancel("unfinished")
        result = import_backup(controller, controller.settings_store, str(archive))
        assert not result["ok"] and "备份含未完成任务" in result["error"]
        assert controller.queue.get_job("unfinished")["status"] == "cancelled"
    finally:
        controller.close()


def test_migration_rebases_restored_text_paths(tmp_path):
    source = _make_controller(str(tmp_path / "source"))
    target = _make_controller(str(tmp_path / "target"))
    try:
        source.save_settings({"output_dir": str(tmp_path / "custom-source-output")})
        target.save_settings({"output_dir": str(tmp_path / "custom-target-output")})
        job = _job("portable")
        job["output_dir"] = source.settings["output_dir"]
        source.queue.enqueue(job)
        source.queue.claim_next("test")
        manifest = _manifest("portable")
        for key in ("raw_path", "final_path"):
            path = Path(source.settings["output_dir"]) / "portable" / Path(manifest[key]).name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("可迁移的正文", encoding="utf-8")
            manifest[key] = str(path)
        segments = Path(source.settings["output_dir"]) / "portable" / "sample_segments.json"
        segments.write_text('[{"start":0,"end":1,"text":"正文"}]', encoding="utf-8")
        translation = segments.with_name("sample_译文_en.md")
        translation.write_text("translated article", encoding="utf-8")
        translation_segments = segments.with_name("sample_译文_en.segments.json")
        translation_segments.write_text('[{"src":"正文","dst":"translated article"}]', encoding="utf-8")
        manifest.update({"segments_path": str(segments), "title_status": "done",
                         "translation_path": str(translation),
                         "translation_segments_path": str(translation_segments),
                         "translation_status": "done",
                         "source_title": "精确标题", "original_source_title": "原标题",
                         "title_skill_version": "concise-accurate-title-v1"})
        source.queue.save_manifest(manifest, worker_id="test")
        archive = tmp_path / "migrate.zip"
        assert export_backup(source, source.settings_store, str(archive))["ok"]
        result = import_backup(target, target.settings_store, str(archive))
        assert result["ok"], result
        for record in (target.queue.get_job("portable"), target.queue.get_manifest("portable")):
            for key in ("raw_path", "final_path"):
                assert Path(record[key]).is_relative_to(Path(target.settings["output_dir"]))
                assert Path(record[key]).read_text(encoding="utf-8") == "可迁移的正文"
        restored = target.queue.get_manifest("portable")
        assert restored["title_status"] == "done"
        assert restored["source_title"] == "精确标题"
        assert restored["original_source_title"] == "原标题"
        assert restored["title_skill_version"] == "concise-accurate-title-v1"
        assert Path(restored["segments_path"]).is_relative_to(Path(target.settings["output_dir"]))
        assert json.loads(Path(restored["segments_path"]).read_text(encoding="utf-8"))[0]["text"] == "正文"
        for key in ("translation_path", "translation_segments_path"):
            assert Path(restored[key]).is_relative_to(Path(target.settings["output_dir"]))
        assert Path(restored["translation_path"]).read_text(encoding="utf-8") == "translated article"
        assert json.loads(Path(restored["translation_segments_path"]).read_text(encoding="utf-8"))[0]["dst"] == "translated article"
    finally:
        source.close()
        target.close()


def test_legacy_backup_without_manifest_metadata_restores_and_migrates_after_restart(tmp_path):
    source = _make_controller(str(tmp_path / "source"))
    target_dir = str(tmp_path / "target")
    target = _make_controller(target_dir)
    try:
        source.queue.enqueue(_job("legacy"))
        source.queue.claim_next("worker")
        root = Path(source.settings["output_dir"])
        source.queue._conn.execute("UPDATE jobs SET output_dir=? WHERE id='legacy'", (str(root),))
        source.queue._conn.commit()
        manifest = _manifest("legacy")
        for key, name in (("raw_path", "sample_raw.md"), ("final_path", "sample.md")):
            path = root / "legacy" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("legacy fixture", encoding="utf-8")
            manifest[key] = str(path)
        source.queue.save_manifest(manifest, worker_id="worker")
        current_archive = tmp_path / "current.zip"
        assert export_backup(source, source.settings_store, str(current_archive))["ok"]
        legacy_db = tmp_path / "legacy.db"
        with zipfile.ZipFile(current_archive) as archive:
            legacy_db.write_bytes(archive.read("library.db"))
            legacy_settings = json.loads(archive.read("settings.json"))
        del legacy_settings["residue_cleanup_default"], legacy_settings["confirm_residue_cleanup"]
        legacy_settings.pop("library_id", None)
        with sqlite3.connect(str(legacy_db)) as database:
            database.execute("ALTER TABLE manifests DROP COLUMN metadata_json")
            database.execute("DROP TABLE library_identity")
            database.execute("PRAGMA user_version=6")
        legacy_archive = tmp_path / "legacy.zip"
        with zipfile.ZipFile(current_archive) as original, zipfile.ZipFile(legacy_archive, "w") as legacy:
            for name in original.namelist():
                if name == "library.db":
                    legacy.writestr(name, legacy_db.read_bytes())
                elif name == "settings.json":
                    legacy.writestr(name, json.dumps(legacy_settings))
                elif name == "backup_manifest.json":
                    legacy.writestr(name, json.dumps({"created_at": "2020-01-01", "content": ["settings.json", "library.db", "output/"]}))
                else:
                    legacy.writestr(name, original.read(name))
        result = import_backup(target, target.settings_store, str(legacy_archive))
        assert result["ok"], result
        assert target.queue.get_manifest("legacy")["job_id"] == "legacy"
        assert "title_status" not in target.queue.get_manifest("legacy")
    finally:
        source.close()
        target.close()
    reopened = _make_controller(target_dir)
    try:
        assert reopened.queue.get_manifest("legacy")["job_id"] == "legacy"
        assert reopened.settings["residue_cleanup_default"] == "outputs"
        assert reopened.settings["confirm_residue_cleanup"] is True
        assert "metadata_json" in {row["name"] for row in reopened.queue._conn.execute("PRAGMA table_info(manifests)")}
        from engine.queue_store import SCHEMA_VERSION
        assert reopened.queue._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        reopened.close()


@pytest.mark.parametrize("name", ["../escape.txt", "..\\escape.txt", "/absolute.txt", "C:/outside.txt"])
def test_zip_path_escape_rejected_before_extract(tmp_path, name):
    from app.maintenance import _safe_extract
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr(name, "x")
    with zipfile.ZipFile(archive) as incoming, pytest.raises(ValueError):
        _safe_extract(incoming, tmp_path / "safe")


def test_diagnostics_log_redaction_masks_watch_traces():
    # 诊断包会被用户转发：观看痕迹（平台链接路径、视频号、BV 号、用户名）不得外流。
    assert _redact_line("GET https://www.douyin.com/video/7687589184130813193?aid=6&x=1 200") == "GET https://www.douyin.com/… 200"
    assert _redact_line("https://v.douyin.com/short/?a=b plain") == "https://v.douyin.com/… plain"
    assert _redact_line("task 7687589184130813193 failed") == "task [id] failed"
    assert _redact_line("done BV1kcbR6wEen transcribe") == "done BV… transcribe"
    assert _redact_line(r"open C:\Users\mingc\AppData\Local\VideoToArticle") == r"open C:\Users\*\AppData\Local\VideoToArticle"
    assert _redact_line("worker heartbeat tick=1760000000.5 ok") == "worker heartbeat tick=1760000000.5 ok"


def _restore_fixture(controller, label):
    root = Path(controller.settings["output_dir"])
    folder = root / "shared"
    folder.mkdir(parents=True)
    job = _job("shared")
    job["output_dir"] = str(root)
    controller.queue.enqueue(job)
    controller.queue.claim_next("fixture")
    manifest = _manifest("shared")
    for key, name in (("raw_path", "a_raw.md"), ("final_path", "b.md")):
        path = folder / name
        path.write_text(label + ":" + name, encoding="utf-8")
        manifest[key] = str(path)
    manifest["source_title"] = label
    controller.queue.save_manifest(manifest, worker_id="fixture")
    return folder


def _restore_state(controller):
    output = Path(controller.settings["output_dir"])
    return {
        "text": {p.relative_to(output).as_posix(): p.read_bytes()
                 for p in output.rglob("*") if p.is_file()},
        "database": list(controller.queue._conn.iterdump()),
        "settings": controller.settings_store.path.read_bytes(),
    }


@pytest.mark.parametrize("failure", ["second_text", "database", "settings"])
def test_failed_restore_rolls_back_complete_set(tmp_path, monkeypatch, failure):
    from app import backup_restore

    source = _make_controller(tmp_path / "source")
    target = _make_controller(tmp_path / "target")
    try:
        _restore_fixture(source, "incoming")
        (Path(source.settings["output_dir"]) / "0_added.md").write_text("new file", encoding="utf-8")
        _restore_fixture(target, "current")
        source.save_settings({"theme": "paper"})
        target.save_settings({"theme": "dark"})
        before = _restore_state(target)
        archive = tmp_path / "backup.zip"
        assert export_backup(source, source.settings_store, str(archive))["ok"]
        if failure == "second_text":
            publish = backup_restore._publish_file
            def failing_publish(src, dst):
                if src.parent.name == "incoming" and dst.name == "a_raw.md":
                    raise PermissionError("fixture: second file locked")
                publish(src, dst)
            monkeypatch.setattr(backup_restore, "_publish_file", failing_publish)
        elif failure == "database":
            backup = backup_restore._backup_database
            def failing_database(src, dst):
                backup(src, dst)
                if src.name == "library.db":
                    raise OSError("fixture: DB publication interrupted")
            monkeypatch.setattr(backup_restore, "_backup_database", failing_database)
        else:
            save = target.settings_store.save
            def failing_settings(settings):
                save(settings)
                raise OSError("fixture: settings publication interrupted")
            monkeypatch.setattr(target.settings_store, "save", failing_settings)
        result = import_backup(target, target.settings_store, str(archive))
        assert result["ok"] is False, result
        assert result["recovery_required"] is False, result
        assert _restore_state(target) == before
        assert not (target.settings_store.base_dir / backup_restore.RECOVERY_DIRECTORY).exists()
    finally:
        source.close()
        target.close()


@pytest.mark.parametrize("failure", ["text", "database", "settings"])
def test_restart_recovers_interrupted_restore(tmp_path, monkeypatch, failure):
    from app import backup_restore

    class Interrupted(BaseException):
        pass

    source = _make_controller(tmp_path / "source")
    target = _make_controller(tmp_path / "target")
    store = target.settings_store
    try:
        _restore_fixture(source, "incoming")
        _restore_fixture(target, "current")
        source.save_settings({"theme": "paper"})
        target.save_settings({"theme": "dark"})
        before = _restore_state(target)
        archive = tmp_path / "backup.zip"
        assert export_backup(source, source.settings_store, str(archive))["ok"]
        with monkeypatch.context() as patch:
            if failure == "text":
                publish = backup_restore._publish_file
                def interrupt_text(src, dst):
                    publish(src, dst)
                    if src.parent.name == "incoming":
                        raise Interrupted()
                patch.setattr(backup_restore, "_publish_file", interrupt_text)
            elif failure == "database":
                backup = backup_restore._backup_database
                def interrupt_database(src, dst):
                    backup(src, dst)
                    if src.name == "library.db":
                        raise Interrupted()
                patch.setattr(backup_restore, "_backup_database", interrupt_database)
            else:
                save = store.save
                def interrupt_settings(settings):
                    save(settings)
                    raise Interrupted()
                patch.setattr(store, "save", interrupt_settings)
            with pytest.raises(Interrupted):
                import_backup(target, store, str(archive))
        target.close()
        target = None
        reopened = AppController(store)
        try:
            assert _restore_state(reopened) == before
            assert not (store.base_dir / backup_restore.RECOVERY_DIRECTORY).exists()
        finally:
            reopened.close()
    finally:
        source.close()
        if target is not None:
            target.close()


def test_restore_rollback_failure_blocks_instance_and_can_resume(tmp_path, monkeypatch):
    from app import backup_restore

    source = _make_controller(tmp_path / "source")
    target = _make_controller(tmp_path / "target")
    try:
        _restore_fixture(source, "incoming")
        _restore_fixture(target, "current")
        before = _restore_state(target)
        archive = tmp_path / "backup.zip"
        assert export_backup(source, source.settings_store, str(archive))["ok"]
        publish = backup_restore._publish_file
        with monkeypatch.context() as patch:
            def locked_rollback(src, dst):
                if src.parent.name == "original" or src.parent.name == "incoming" and dst.name == "b.md":
                    raise PermissionError("fixture: locked text")
                publish(src, dst)
            patch.setattr(backup_restore, "_publish_file", locked_rollback)
            result = import_backup(target, target.settings_store, str(archive))
            assert not result["ok"] and result["recovery_required"]
            assert target._backup_restore_blocked
            assert import_backup(target, target.settings_store, str(archive))["recovery_required"]
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                target.queue.enqueue(_job("must-not-start"))
            with pytest.raises(backup_restore.BackupRestoreError):
                AppController(target.settings_store)
        store = target.settings_store
        target.close()
        target = None
        reopened = AppController(store)
        try:
            assert _restore_state(reopened) == before
        finally:
            reopened.close()
    finally:
        source.close()
        if target is not None:
            target.close()


def test_archive_cannot_publish_into_local_recovery_directory(tmp_path):
    from app.backup_restore import RECOVERY_DIRECTORY

    source = _make_controller(tmp_path / "source")
    target = _make_controller(tmp_path / "target")
    try:
        _restore_fixture(source, "incoming")
        target.save_settings({"output_dir": str(target.settings_store.base_dir)})
        archive = tmp_path / "poison.zip"
        assert export_backup(source, source.settings_store, str(archive))["ok"]
        with zipfile.ZipFile(archive, "a") as zf:
            zf.writestr(f"output/{RECOVERY_DIRECTORY}/journal.json", '{"phase":"committed"}')
        before = _restore_state(target)
        result = import_backup(target, target.settings_store, str(archive))
        assert not result["ok"] and not result["recovery_required"]
        # The automatic pre-import archive is the only permitted added file.
        after = _restore_state(target)
        before["text"] = {k: v for k, v in before["text"].items() if not k.startswith("backups/")}
        after["text"] = {k: v for k, v in after["text"].items() if not k.startswith("backups/")}
        assert before == after
        assert not (target.settings_store.base_dir / RECOVERY_DIRECTORY).exists()
    finally:
        source.close()
        target.close()


def test_import_requires_a_complete_pre_import_archive(tmp_path, monkeypatch):
    source = _make_controller(tmp_path / "source")
    target = _make_controller(tmp_path / "target")
    try:
        _restore_fixture(source, "incoming")
        folder = _restore_fixture(target, "current")
        before = _restore_state(target)
        archive = tmp_path / "source.zip"
        assert export_backup(source, source.settings_store, str(archive))["ok"]
        write = zipfile.ZipFile.write
        def unreadable_text(zf, filename, *args, **kwargs):
            if Path(filename) == folder / "a_raw.md":
                raise PermissionError("fixture: existing article cannot be backed up")
            return write(zf, filename, *args, **kwargs)
        monkeypatch.setattr(zipfile.ZipFile, "write", unreadable_text)
        result = import_backup(target, target.settings_store, str(archive))
        assert not result["ok"] and "导入前备份失败" in result["error"]
        assert _restore_state(target) == before
    finally:
        source.close()
        target.close()


def test_import_future_schema_is_rejected_before_current_data_changes(tmp_path):
    from engine.queue_store import SCHEMA_VERSION
    source = _make_controller(tmp_path / "source")
    target = _make_controller(tmp_path / "target")
    try:
        _restore_fixture(source, "incoming")
        _restore_fixture(target, "current")
        before = _restore_state(target)
        original = tmp_path / "original.zip"
        assert export_backup(source, source.settings_store, str(original))["ok"]
        future_db = tmp_path / "future.db"
        future_zip = tmp_path / "future.zip"
        with zipfile.ZipFile(original) as zf:
            future_db.write_bytes(zf.read("library.db"))
            database = sqlite3.connect(future_db)
            try:
                database.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
                database.commit()
            finally:
                database.close()
            contents = {name: future_db.read_bytes() if name == "library.db" else zf.read(name)
                        for name in zf.namelist()}
            refresh_backup_checksums(contents)
            with zipfile.ZipFile(future_zip, "w") as modified:
                for name, content in contents.items():
                    modified.writestr(name, content)
        result = import_backup(target, target.settings_store, str(future_zip))
        assert not result["ok"] and "高于当前支持版本" in result["error"]
        assert not result["recovery_required"]
        assert _restore_state(target) == before
    finally:
        source.close()
        target.close()


@pytest.mark.parametrize("damage", ["journal_path", "drive_relative_path", "database_snapshot", "text_snapshot"])
def test_invalid_local_recovery_records_block_startup_without_further_changes(tmp_path, monkeypatch, damage):
    from app import backup_restore

    class Interrupted(BaseException):
        pass
    source = _make_controller(tmp_path / "source")
    target = _make_controller(tmp_path / "target")
    try:
        _restore_fixture(source, "incoming")
        _restore_fixture(target, "current")
        archive = tmp_path / "source.zip"
        assert export_backup(source, source.settings_store, str(archive))["ok"]
        publish = backup_restore._publish_file
        with monkeypatch.context() as patch:
            def interrupt(src, dst):
                publish(src, dst)
                if src.parent.name == "incoming":
                    raise Interrupted()
            patch.setattr(backup_restore, "_publish_file", interrupt)
            with pytest.raises(Interrupted):
                import_backup(target, target.settings_store, str(archive))
        partial = _restore_state(target)
        directory = target.settings_store.base_dir / backup_restore.RECOVERY_DIRECTORY
        if damage in {"journal_path", "drive_relative_path"}:
            journal_path = directory / "journal.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["files"][0]["path"] = "../escape.md" if damage == "journal_path" else "C:escape.md"
            journal_path.write_text(json.dumps(journal), encoding="utf-8")
        elif damage == "database_snapshot":
            (directory / "library.original.db").unlink()
        else:
            (directory / "original" / "0").write_bytes(b"corrupted snapshot")
        with pytest.raises(backup_restore.BackupRestoreError):
            AppController(target.settings_store)
        assert _restore_state(target) == partial
        assert (directory / "journal.json").exists()
    finally:
        source.close()
        target.close()


def test_completed_rollback_cleanup_failure_never_replays_over_later_edits(tmp_path, monkeypatch):
    from app import backup_restore

    source = _make_controller(tmp_path / "source")
    target = _make_controller(tmp_path / "target")
    try:
        _restore_fixture(source, "incoming")
        folder = _restore_fixture(target, "current")
        archive = tmp_path / "source.zip"
        assert export_backup(source, source.settings_store, str(archive))["ok"]
        publish = backup_restore._publish_file
        remove = backup_restore.shutil.rmtree
        with monkeypatch.context() as patch:
            def fail_second_file(src, dst):
                if src.parent.name == "incoming" and dst.name == "b.md":
                    raise PermissionError("fixture: failed text publication")
                publish(src, dst)
            def leave_completed_journal(path, *args, **kwargs):
                if Path(path).name == backup_restore.RECOVERY_DIRECTORY:
                    return
                return remove(path, *args, **kwargs)
            patch.setattr(backup_restore, "_publish_file", fail_second_file)
            patch.setattr(backup_restore.shutil, "rmtree", leave_completed_journal)
            result = import_backup(target, target.settings_store, str(archive))
        assert not result["ok"] and not result["recovery_required"]
        journal = target.settings_store.base_dir / backup_restore.RECOVERY_DIRECTORY / "journal.json"
        assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "rolled_back"
        (folder / "a_raw.md").write_text("edited after complete rollback", encoding="utf-8")
        store = target.settings_store
        target.close()
        target = None
        reopened = AppController(store)
        try:
            assert (folder / "a_raw.md").read_text(encoding="utf-8") == "edited after complete rollback"
        finally:
            reopened.close()
    finally:
        source.close()
        if target is not None:
            target.close()
