# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Document backups preserve historical assets and fail without replacing good copies."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app.controller import AppController
from app.maintenance import export_backup, import_backup
from engine.library_location import read_library_identity
from engine.postprocess_store import PostprocessStore
from engine.settings import SettingsStore
from test_queue_store import _job, _manifest


@pytest.fixture
def library(tmp_path):
    controller = AppController(SettingsStore(base_dir=tmp_path / "library"))
    try:
        yield controller
    finally:
        controller.close()


def _complete(controller, job_id, root, text):
    root = Path(root)
    directory = root / job_id
    directory.mkdir(parents=True, exist_ok=True)
    job = _job(job_id)
    job["output_dir"] = str(root)
    controller.queue.enqueue(job)
    controller.queue.claim_next("fixture")
    manifest = _manifest(job_id)
    for field, name in (("raw_path", "sample_raw.md"), ("final_path", "sample.md"),
                        ("segments_path", "sample_segments.json"),
                        ("translation_path", "sample_译文_en.md"),
                        ("translation_segments_path", "sample_译文_en.segments.json")):
        path = directory / name
        path.write_text(text + ":" + field, encoding="utf-8")
        manifest[field] = str(path)
    controller.queue.save_manifest(manifest, worker_id="fixture")
    (directory / "sample_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def _rewrite_archive(source, target, change):
    with zipfile.ZipFile(source) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    change(contents)
    with zipfile.ZipFile(target, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)


def test_historical_outputs_and_nested_current_root_restore_without_collisions(library, tmp_path):
    old_root = tmp_path / "old-output"
    current = old_root / "current-output"
    library.save_settings({"output_dir": str(current)})
    historical_id, current_id, other_id = "111111111111", "222222222222", "333333333333"
    expected = {
        historical_id: _complete(library, historical_id, old_root, "old"),
        current_id: _complete(library, current_id, current, "new"),
        other_id: _complete(library, other_id, tmp_path / "other-output", "other"),
    }
    # A former output root is not permission to sweep all unrelated user files.
    (old_root / "unrelated.txt").write_text("not app-owned", encoding="utf-8")
    archive = tmp_path / "historical.zip"
    result = export_backup(library, library.settings_store, str(archive))
    assert result["ok"] and result["complete"] and not result["includes_media"], result
    with zipfile.ZipFile(archive) as backup:
        manifest = json.loads(backup.read("backup_manifest.json"))
        assert manifest["kind"] == "document_backup"
        assert f"output/{current_id}/sample.md" in backup.namelist()
        assert not any(name.endswith("unrelated.txt") for name in backup.namelist())
        for record in expected.values():
            for key in ("raw_path", "final_path", "segments_path", "translation_path", "translation_segments_path"):
                name = manifest["references"][record[key]]
                assert backup.read(name) == Path(record[key]).read_bytes()
                assert manifest["files"][name]["sha256"] == hashlib.sha256(backup.read(name)).hexdigest()
    restored = AppController(SettingsStore(base_dir=tmp_path / "restored"))
    try:
        result = import_backup(restored, restored.settings_store, str(archive))
        assert result["ok"], result
        assert restored.settings_store.load()["library_id"] == read_library_identity(library.settings["database_path"])
        assert read_library_identity(restored.settings["database_path"]) == read_library_identity(library.settings["database_path"])
        for job_id, original in expected.items():
            record = restored.queue.get_manifest(job_id)
            for key in ("raw_path", "final_path", "segments_path", "translation_path", "translation_segments_path"):
                assert Path(record[key]).is_relative_to(Path(restored.settings["output_dir"]))
                assert Path(record[key]).read_bytes() == Path(original[key]).read_bytes()
        assert restored.queue.get_job(current_id)["output_dir"] == restored.settings["output_dir"]
        assert restored.queue.get_job(historical_id)["output_dir"] != restored.settings["output_dir"]
        assert restored.settings["library_id"] == read_library_identity(restored.settings["database_path"])
        reexport = tmp_path / "reexport.zip"
        assert export_backup(restored, restored.settings_store, str(reexport))["ok"]
        with zipfile.ZipFile(reexport) as backup:
            assert json.loads(backup.read("settings.json"))["library_id"] == restored.settings["library_id"]
    finally:
        restored.close()
    reopened = AppController(SettingsStore(base_dir=tmp_path / "restored"))
    reopened.close()


def test_missing_registered_text_reports_all_paths_and_keeps_old_archive(library, tmp_path):
    record = _complete(library, "missing", library.settings["output_dir"], "fixture")
    archive = tmp_path / "backup.zip"
    assert export_backup(library, library.settings_store, str(archive))["ok"]
    previous = archive.read_bytes()
    Path(record["raw_path"]).unlink()
    Path(record["translation_path"]).unlink()
    result = export_backup(library, library.settings_store, str(archive))
    assert not result["ok"] and not result["complete"]
    assert set(result["missing_files"]) == {record["raw_path"], record["translation_path"]}
    assert archive.read_bytes() == previous
    assert not list(tmp_path.glob(".foilpage-backup-*"))


def test_failed_archive_write_keeps_existing_backup(library, tmp_path, monkeypatch):
    record = _complete(library, "write-failure", library.settings["output_dir"], "fixture")
    archive = tmp_path / "backup.zip"
    assert export_backup(library, library.settings_store, str(archive))["ok"]
    before = archive.read_bytes()
    write = zipfile.ZipFile.write

    def fail_write(backup, path, *args, **kwargs):
        if Path(path) == Path(record["final_path"]):
            raise PermissionError("fixture locked output")
        return write(backup, path, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "write", fail_write)
    result = export_backup(library, library.settings_store, str(archive))
    assert not result["ok"] and "fixture locked output" in result["error"]
    assert archive.read_bytes() == before
    assert not list(tmp_path.glob(".foilpage-backup-*"))


@pytest.mark.parametrize("damage", ["text", "missing_member", "missing_reference", "unknown_version", "missing_version",
                                    "incomplete", "missing_identity", "identity_mismatch"])
def test_damaged_new_archive_rejected_before_publication(library, tmp_path, damage):
    record = _complete(library, "damage", library.settings["output_dir"], "fixture")
    archive, damaged = tmp_path / "good.zip", tmp_path / "damaged.zip"
    assert export_backup(library, library.settings_store, str(archive))["ok"]

    def damage_archive(contents):
        manifest = json.loads(contents["backup_manifest.json"])
        name = manifest["references"][record["final_path"]]
        if damage == "text":
            contents[name] = b"changed outside export"
        elif damage == "missing_member":
            del contents[name]
        elif damage == "missing_reference":
            del manifest["references"][record["final_path"]]
        elif damage == "missing_version":
            del manifest["format_version"]
        elif damage == "incomplete":
            manifest["complete"] = False
        elif damage == "missing_identity":
            del manifest["library_id"]
        elif damage == "identity_mismatch":
            manifest["library_id"] = "0" * 32
        else:
            manifest["format_version"] = 999
        contents["backup_manifest.json"] = json.dumps(manifest).encode()

    _rewrite_archive(archive, damaged, damage_archive)
    before = list(library.queue._conn.iterdump())
    result = import_backup(library, library.settings_store, str(damaged))
    assert not result["ok"] and not result["recovery_required"], result
    assert list(library.queue._conn.iterdump()) == before
    assert Path(record["final_path"]).read_text(encoding="utf-8") == "fixture:final_path"


def test_valid_member_cannot_be_reassigned_to_another_document(library, tmp_path):
    first = _complete(library, "111111111111", library.settings["output_dir"], "first")
    second = _complete(library, "222222222222", library.settings["output_dir"], "second")
    original, damaged = tmp_path / "original.zip", tmp_path / "damaged.zip"
    assert export_backup(library, library.settings_store, str(original))["ok"]

    def reassign(contents):
        manifest = json.loads(contents["backup_manifest.json"])
        manifest["references"][first["final_path"]] = manifest["references"][second["final_path"]]
        contents["backup_manifest.json"] = json.dumps(manifest).encode()

    _rewrite_archive(original, damaged, reassign)
    result = import_backup(library, library.settings_store, str(damaged))
    assert not result["ok"] and "串稿" in result["error"]
    assert Path(first["final_path"]).read_text(encoding="utf-8") == "first:final_path"
    assert Path(second["final_path"]).read_text(encoding="utf-8") == "second:final_path"


def test_export_blocks_other_sqlite_writers_until_files_are_copied(library, tmp_path, monkeypatch):
    _complete(library, "sqlite-gate", library.settings["output_dir"], "fixture")
    write = zipfile.ZipFile.write
    checked = []

    def probe_writer(archive, path, *args, **kwargs):
        connection = sqlite3.connect(library.settings["database_path"], timeout=0.01)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                connection.execute("BEGIN IMMEDIATE")
            checked.append(True)
        finally:
            connection.close()
        return write(archive, path, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "write", probe_writer)
    result = export_backup(library, library.settings_store, str(tmp_path / "backup.zip"))
    assert result["ok"] and checked, result
    connection = sqlite3.connect(library.settings["database_path"], timeout=0.01)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
    finally:
        connection.close()


@pytest.mark.parametrize("writer", ["worker", "download", "postprocess"])
def test_direct_export_rejects_active_file_writers(library, tmp_path, writer):
    postprocess = None
    if writer == "worker":
        library.queue.enqueue(_job("running"))
        library.queue.claim_next("fixture")
    elif writer == "download":
        item = library.downloads.create("https://example.invalid/fixture", "fixture", "other", {})
        library.downloads.transition(item["id"], "resolving")
        library.downloads.transition(item["id"], "downloading")
    else:
        postprocess = PostprocessStore(library.settings["database_path"])
        postprocess.start("translation", "fixture", {})
    try:
        result = export_backup(library, library.settings_store, str(tmp_path / "backup.zip"))
        assert not result["ok"] and not result["complete"], result
        assert not (tmp_path / "backup.zip").exists()
    finally:
        if postprocess is not None:
            postprocess.close()


def test_export_rejects_relative_historical_output_instead_of_scanning_cwd(library, tmp_path):
    library.queue.enqueue(_job("relative"))
    library.queue._conn.execute("UPDATE jobs SET output_dir='.' WHERE id='relative'")
    library.queue._conn.commit()
    result = export_backup(library, library.settings_store, str(tmp_path / "backup.zip"))
    assert not result["ok"] and "绝对路径" in result["error"]
