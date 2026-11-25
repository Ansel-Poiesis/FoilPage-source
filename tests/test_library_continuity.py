# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Library identity and historical text remain continuous across normal operations."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from app.bridge import Bridge
from app.controller import AppController
from engine.library_location import LibraryLocationError, read_library_identity
from engine.settings import SettingsError, SettingsStore, SettingsValidationError
from test_text_output_deletion import _complete


def test_new_library_reopens_with_same_identity_and_records(tmp_path):
    settings = SettingsStore(base_dir=tmp_path / "profile")
    controller = AppController(settings)
    job_id, _, _, _ = _complete(controller)
    identity = controller.settings["library_id"]
    controller.close()

    reopened = AppController(settings)
    try:
        assert reopened.settings["library_id"] == identity
        assert read_library_identity(reopened.settings["database_path"]) == identity
        assert reopened.queue.get_job(job_id)["status"] == "done"
        assert reopened.read_document_text(job_id)["text"] == "final fixture"
    finally:
        reopened.close()


@pytest.mark.parametrize("missing", ["database", "settings"])
def test_bound_library_missing_file_cannot_be_replaced_by_defaults(tmp_path, missing):
    store = SettingsStore(base_dir=tmp_path / "profile")
    controller = AppController(store)
    database = Path(controller.settings["database_path"])
    controller.close()
    absent = database if missing == "database" else store.path
    absent.unlink()

    with pytest.raises((LibraryLocationError, SettingsError)):
        AppController(store)
    assert not absent.exists()
    if missing == "settings":
        assert database.is_file()


def test_historical_output_keeps_search_preview_reader_and_editor_consistent(tmp_path):
    controller = AppController(SettingsStore(base_dir=tmp_path / "profile"))
    try:
        job_id, _, directory, _ = _complete(controller)
        current = tmp_path / "next-output"
        current.mkdir()
        controller.save_settings({"output_dir": str(current)})
        bridge = Bridge(controller, SimpleNamespace(running=False, pid=None),
                        controller.settings_store, lambda: None, lambda: None)
        documents = bridge.get_documents("final fixture")
        assert len(documents) == 1
        assert documents[0]["task_id"] == "job:" + job_id
        assert documents[0]["preview"] == "final fixture"
        assert bridge.get_reader("job:" + job_id)["text"] == "final fixture"
        read = controller.read_document_text(job_id)
        controller.update_document_text(job_id, "final", "edited historical fixture",
                                        expected_revision=read["revision"])
        assert bridge.get_documents("edited historical")[0]["preview"] == "edited historical fixture"
        assert (directory / "sample.md").read_text(encoding="utf-8") == "edited historical fixture"
        assert not list(current.iterdir())

        task = controller.task_rows()[0]
        outside = tmp_path / "unregistered.md"
        outside.write_text("private fixture", encoding="utf-8")
        task["final_path"], task["raw_path"] = str(outside), ""
        assert bridge._document_preview(task) == ""
    finally:
        controller.close()


@pytest.mark.parametrize("key,value", [("library_id", ""), ("database_path", "other.db")])
def test_regular_settings_cannot_rebind_active_library(tmp_path, key, value):
    controller = AppController(SettingsStore(base_dir=tmp_path / "profile"))
    try:
        before = controller.settings_store.path.read_bytes()
        with pytest.raises(SettingsValidationError):
            controller.save_settings({key: value})
        assert controller.settings_store.path.read_bytes() == before
        bridge = Bridge(controller, SimpleNamespace(running=False, pid=None),
                        controller.settings_store, lambda: None, lambda: None)
        assert "library_id" not in bridge._agent_editable_settings()
    finally:
        controller.close()


@pytest.mark.parametrize("activity", ["worker", "translation", "batch"])
def test_bridge_refuses_backup_before_background_writer_stops(tmp_path, activity):
    controller = AppController(SettingsStore(base_dir=tmp_path / "profile"))
    try:
        bridge = Bridge(controller, SimpleNamespace(running=activity == "worker", pid=None),
                        controller.settings_store, lambda: None, lambda: None)
        bridge._translation["running"] = activity == "translation"
        bridge._batch_agent["running"] = activity == "batch"
        with mock.patch("app.bridge._export_backup") as export:
            result = bridge.export_backup(str(tmp_path / "backup.zip"))
        assert result["ok"] is False
        assert "备份" in result["error"]
        export.assert_not_called()
        assert not (tmp_path / "backup.zip").exists()
    finally:
        controller.close()


def test_missing_settings_with_external_database_does_not_create_local_database(tmp_path):
    store = SettingsStore(base_dir=tmp_path / "profile")
    configured = store.defaults()
    configured["database_path"] = str(tmp_path / "external" / "library.db")
    store.save(configured)
    controller = AppController(store)
    external = Path(controller.settings["database_path"])
    controller.close()
    expected_id = json.loads(store.path.read_text(encoding="utf-8"))["library_id"]
    store.path.unlink()

    with pytest.raises((LibraryLocationError, SettingsError)):
        AppController(store)
    assert not (store.base_dir / "library.db").exists()
    assert not store.path.exists()
    assert read_library_identity(external) == expected_id
