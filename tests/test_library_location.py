# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Library location failures must never turn an existing profile into an empty one."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from agent.client import AgentClient, AgentError, default_data_dir
from app import desktop_web, worker
from engine.library_location import (
    INITIALIZED_MARKER, LibraryLocationError, bind_library_identity, default_library_root,
    mark_library_initialized, preflight_library, read_library_identity, validate_storage_path,
)
from engine.settings import SettingsError, SettingsStore, SettingsValidationError, default_app_data_dir


def test_empty_new_root_does_not_hide_legacy_library(tmp_path, monkeypatch):
    current, legacy = tmp_path / "FoilPage", tmp_path / "VideoToArticle"
    (current / "logs").mkdir(parents=True)
    legacy.mkdir()
    (legacy / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_library_root() == legacy
    assert default_app_data_dir() == legacy
    assert default_data_dir() == legacy
    assert (legacy / "settings.json").read_text(encoding="utf-8") == "{}"


def test_distinct_persistent_roots_are_not_selected_by_age_or_name(tmp_path, monkeypatch):
    for name in ("FoilPage", "VideoToArticle"):
        root = tmp_path / name
        root.mkdir()
        (root / INITIALIZED_MARKER).touch()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(LibraryLocationError, match="两个不同"):
        default_library_root()
    with pytest.raises(SettingsError, match="两个不同"):
        default_app_data_dir()
    with pytest.raises(AgentError, match="两个不同"):
        default_data_dir()


def test_aliases_of_one_library_are_not_a_conflict(tmp_path):
    current, legacy = tmp_path / "FoilPage", tmp_path / "VideoToArticle"
    current.mkdir()
    (current / INITIALIZED_MARKER).touch()
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(current), str(legacy))
    else:
        legacy.symlink_to(current, target_is_directory=True)
    assert default_library_root({"LOCALAPPDATA": str(tmp_path)}) == current.resolve()


def test_explicit_development_root_does_not_inspect_conflicting_default_roots(tmp_path, monkeypatch):
    for name in ("FoilPage", "VideoToArticle"):
        root = tmp_path / "local" / name
        root.mkdir(parents=True)
        (root / INITIALIZED_MARKER).touch()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    store = SettingsStore(base_dir=tmp_path / "development")
    preflight_library(store, store.load())
    assert not store.base_dir.exists()


def test_existing_data_without_settings_is_not_a_new_profile(tmp_path):
    sqlite3.connect(tmp_path / "library.db").close()
    before = (tmp_path / "library.db").read_bytes()
    with pytest.raises(SettingsError, match="设置文件缺失"):
        SettingsStore(base_dir=tmp_path).load()
    assert not (tmp_path / "settings.json").exists()
    assert (tmp_path / "library.db").read_bytes() == before


def test_receipt_protects_a_profile_whose_database_is_elsewhere(tmp_path):
    store = SettingsStore(base_dir=tmp_path)
    mark_library_initialized(store)
    with pytest.raises(SettingsError, match="设置文件缺失"):
        store.load()
    assert not (tmp_path / "library.db").exists()
    assert (tmp_path / INITIALIZED_MARKER).is_file()


def test_first_custom_database_configuration_remains_possible(tmp_path):
    store = SettingsStore(base_dir=tmp_path / "profile")
    settings = store.load()
    settings["database_path"] = str(tmp_path / "custom" / "library.db")
    store.save(settings)
    preflight_library(store, settings)
    assert not Path(settings["database_path"]).exists()


@pytest.mark.parametrize("used", [{"library_id": "a" * 32}, {"setup_completed": True}, {"onboarding_completed_version": "0.1.5"}])
def test_missing_used_database_is_not_recreated(tmp_path, used):
    store = SettingsStore(base_dir=tmp_path)
    settings = {**store.load(), **used}
    store.save(settings)
    before = store.path.read_bytes()
    with pytest.raises(LibraryLocationError, match="停止创建空库"):
        preflight_library(store, settings)
    assert not (tmp_path / "library.db").exists()
    assert store.path.read_bytes() == before


def test_binding_an_old_database_preserves_existing_rows_and_identity(tmp_path):
    database = tmp_path / "library.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE example(id TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO example VALUES ('stable-id','original')")
    assert read_library_identity(database) == ""
    identity = bind_library_identity(database)
    assert len(identity) == 32
    assert bind_library_identity(database, identity) == identity
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM example").fetchall() == [("stable-id", "original")]
        assert read_library_identity(connection) == identity


def test_unexpected_database_is_never_given_the_expected_identity(tmp_path):
    database = tmp_path / "other.db"
    sqlite3.connect(database).close()
    with pytest.raises(LibraryLocationError, match="不是配置记录"):
        bind_library_identity(database, "a" * 32)
    assert read_library_identity(database) == ""
    with pytest.raises(LibraryLocationError):
        bind_library_identity(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


def test_supplied_identity_connection_uses_its_callers_transaction(tmp_path):
    with sqlite3.connect(tmp_path / "library.db") as connection:
        connection.execute("BEGIN IMMEDIATE")
        identity = bind_library_identity(connection)
        assert read_library_identity(connection) == identity
        connection.rollback()
        assert read_library_identity(connection) == ""


def test_expected_identity_is_checked_before_profile_writes(tmp_path):
    store = SettingsStore(base_dir=tmp_path)
    settings = store.load()
    store.save(settings)
    database = Path(settings["database_path"])
    sqlite3.connect(database).close()
    original_id = bind_library_identity(database)
    settings["library_id"] = "b" * 32 if original_id != "b" * 32 else "c" * 32
    store.save(settings)
    before = database.read_bytes()
    with pytest.raises(LibraryLocationError, match="不是配置记录"):
        preflight_library(store, settings)
    assert database.read_bytes() == before


@pytest.mark.parametrize("field", ["database_path", "output_dir", "download_dir", "browser_download_dir", "model_dir"])
def test_settings_reject_data_paths_inside_installation(tmp_path, monkeypatch, field):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    store = SettingsStore(base_dir=tmp_path / "profile")
    settings = store.load()
    settings[field] = str(tmp_path / "Programs" / "FoilPage" / "user-data")
    with pytest.raises(SettingsValidationError, match="安装目录"):
        store.save(settings)
    assert not store.path.exists()


@pytest.mark.parametrize("name", ["FoilPage", "VideoToArticle"])
def test_backup_destination_cannot_be_inside_installation(tmp_path, name):
    destination = tmp_path / "Programs" / name / "backup.zip"
    with pytest.raises(LibraryLocationError, match="安装目录"):
        validate_storage_path(destination, environment={"LOCALAPPDATA": str(tmp_path)})


def test_frozen_worker_protects_the_actual_custom_installation(tmp_path, monkeypatch):
    install = tmp_path / "custom-install"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install / "worker" / "FoilPage-Worker.exe"))
    with pytest.raises(LibraryLocationError, match="安装目录"):
        validate_storage_path(install / "backups" / "archive.zip")
    assert validate_storage_path(tmp_path / "independent-library") == tmp_path / "independent-library"


def test_agent_refuses_an_identity_mismatch_without_connecting(tmp_path):
    database = tmp_path / "library.db"
    sqlite3.connect(database).close()
    bind_library_identity(database)
    (tmp_path / "settings.json").write_text(json.dumps({
        "database_path": str(database), "library_id": "d" * 32,
        "local_api_port": 18765, "local_api_token": "synthetic-token",
    }), encoding="utf-8")
    with pytest.raises(AgentError, match="不是配置记录"):
        AgentClient.from_data_dir(tmp_path)


def test_worker_without_a_library_never_creates_a_cwd_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert worker.main(["--oneshot"]) == 2
    assert "不会创建空库" in capsys.readouterr().err
    assert not (tmp_path / "queue.db").exists()
    assert not (tmp_path / "local" / "FoilPage" / "library.db").exists()


def test_worker_rejects_a_different_database_before_queue_creation(tmp_path, monkeypatch, capsys):
    store = SettingsStore(base_dir=tmp_path / "profile")
    settings = store.load()
    store.save(settings)
    Path(settings["database_path"]).parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(settings["database_path"]).close()
    other = tmp_path / "other.db"
    sqlite3.connect(other).close()
    monkeypatch.setattr(worker.queue_store, "QueueStore", lambda *_: pytest.fail("opened wrong DB"))
    assert worker.main(["--db", str(other), "--settings-dir", str(store.base_dir), "--oneshot"]) == 2
    assert "位置不同" in capsys.readouterr().err


def test_explicit_development_logs_stay_in_the_selected_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "production"))
    root = tmp_path / "development"
    assert desktop_web._runtime_log_dir(root) == root / "logs"
    assert not (tmp_path / "production").exists()
