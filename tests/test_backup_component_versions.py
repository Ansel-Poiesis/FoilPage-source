# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Reject unknown component formats before publishing an imported backup."""
from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from app.controller import AppController
from app.maintenance import export_backup, import_backup
from engine.settings import SettingsStore
from backup_fixtures import refresh_backup_checksums


@pytest.mark.parametrize("component", ["media", "downloads"])
def test_future_component_backup_import_rejected_without_touching_live_data(tmp_path, component):
    controller = AppController(SettingsStore(base_dir=tmp_path / "data"))
    try:
        output = Path(controller.settings["output_dir"]) / "personal.md"
        output.write_text("keep current content", encoding="utf-8")
        archive = tmp_path / "current.zip"
        assert export_backup(controller, controller.settings_store, str(archive))["ok"]
        with zipfile.ZipFile(archive) as incoming:
            contents = {name: incoming.read(name) for name in incoming.namelist()}
        database = tmp_path / "future.db"
        database.write_bytes(contents["library.db"])
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE app_schema SET version=99 WHERE component=?", (component,))
        connection.close()
        contents["library.db"] = database.read_bytes()
        contents["output/personal.md"] = b"future backup content"
        refresh_backup_checksums(contents)
        future = tmp_path / "future.zip"
        with zipfile.ZipFile(future, "w") as target:
            for name, content in contents.items():
                target.writestr(name, content)
        old_settings = dict(controller.settings)
        with mock.patch("app.maintenance.apply_backup_import") as apply:
            result = import_backup(controller, controller.settings_store, str(future))
        assert not result["ok"]
        assert component in result["error"] and "99" in result["error"]
        apply.assert_not_called()
        assert output.read_text(encoding="utf-8") == "keep current content"
        assert controller.settings == old_settings
        assert controller.queue.list_jobs() == []
        assert controller.media.list_media() == []
        assert controller.downloads._conn.execute(
            "SELECT version FROM app_schema WHERE component=?", (component,)
        ).fetchone()[0] == 2
    finally:
        controller.close()
