# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""数据自检/备份迁移/诊断包 测试（底二层）。"""
from __future__ import annotations

import json
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
            orphan_dir = out_root / "no-such-job"
            orphan_dir.mkdir(parents=True, exist_ok=True)
            (orphan_dir / "raw.txt").write_text("x", encoding="utf-8")
            orphans = orphan_output_dirs(controller)
            assert [o["name"] for o in orphans] == ["no-such-job"]
            assert controller.purge_orphan_output(str(orphan_dir)) is True
            assert not orphan_dir.exists()
            assert controller.purge_orphan_output(str(out_root)) is False
            assert controller.purge_orphan_output(str(out_root.parent)) is False
        finally:
            controller.close()


def test_orphan_download_files_detected():
    with tempfile.TemporaryDirectory(prefix="health_orphan_dl_") as temp:
        controller = _make_controller(temp)
        try:
            dl_root = Path(controller.settings["download_dir"])
            dl_root.mkdir(parents=True, exist_ok=True)
            known = dl_root / "known.mp4"
            orphan = dl_root / "orphan.mp4"
            known.write_bytes(b"k")
            orphan.write_bytes(b"o")
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
            assert names == {"orphan.mp4"}
            assert controller.purge_orphan_download(str(orphan)) is True
            assert not orphan.exists()
            # purge_orphan_download 只做路径边界校验；"已知性"由前端只传
            # 孤儿列表保证，因此这里验证目录与越界路径被拒绝。
            assert controller.purge_orphan_download(str(dl_root)) is False
            assert controller.purge_orphan_download(str(dl_root.parent)) is False
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
        manifest.update({"segments_path": str(segments), "title_status": "done",
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
        source.queue.save_manifest(_manifest("legacy"), worker_id="worker")
        current_archive = tmp_path / "current.zip"
        assert export_backup(source, source.settings_store, str(current_archive))["ok"]
        legacy_db = tmp_path / "legacy.db"
        with zipfile.ZipFile(current_archive) as archive:
            legacy_db.write_bytes(archive.read("library.db"))
        with sqlite3.connect(str(legacy_db)) as database:
            database.execute("ALTER TABLE manifests DROP COLUMN metadata_json")
            database.execute("PRAGMA user_version=6")
        legacy_archive = tmp_path / "legacy.zip"
        with zipfile.ZipFile(current_archive) as original, zipfile.ZipFile(legacy_archive, "w") as legacy:
            for name in original.namelist():
                legacy.writestr(name, legacy_db.read_bytes() if name == "library.db" else original.read(name))
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
