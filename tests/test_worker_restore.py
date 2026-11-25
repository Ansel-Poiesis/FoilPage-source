# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Worker startup and idle polling must not write through a pending restore."""
from pathlib import Path
from unittest import mock

import pytest

from app import worker, backup_restore
from app.controller import AppController
from app.maintenance import export_backup, import_backup
from engine.settings import SettingsStore


@pytest.mark.parametrize("explicit", [True, False])
def test_worker_refuses_pending_recovery_before_queue_creation(tmp_path, monkeypatch, capsys, explicit):
    settings = tmp_path / "settings"
    journal = settings / backup_restore.RECOVERY_DIRECTORY
    journal.mkdir(parents=True)
    (journal / "journal.json").write_text("{}", encoding="utf-8")
    create_queue = mock.Mock(side_effect=AssertionError("must not open or migrate DB"))
    monkeypatch.setattr(worker.queue_store, "QueueStore", create_queue)
    argv = ["--db", str(settings / "library.db"), "--oneshot"]
    if explicit:
        argv += ["--settings-dir", str(settings)]
    assert worker.main(argv) == 2
    assert "无法启动" in capsys.readouterr().err
    create_queue.assert_not_called()
    assert (journal / "journal.json").is_file()


@pytest.mark.parametrize("concurrent", [True, False])
def test_idle_worker_stops_before_lease_recovery_or_settings_load(tmp_path, monkeypatch, concurrent):
    store = worker.queue_store.QueueStore(str(tmp_path / "library.db"))
    settings = SettingsStore(base_dir=tmp_path)
    try:
        instance = worker.Worker(store, settings_store=settings, concurrency=2 if concurrent else 1)
        (tmp_path / backup_restore.RECOVERY_DIRECTORY).mkdir()
        stale = mock.Mock(side_effect=AssertionError("must not change leases"))
        load = mock.Mock(side_effect=AssertionError("must not load mixed settings"))
        monkeypatch.setattr(store, "recover_stale_jobs", stale)
        monkeypatch.setattr(settings, "load", load)
        with pytest.raises(backup_restore.BackupRestoreError):
            if concurrent:
                instance._run_forever_concurrent()
            else:
                instance.step()
        stale.assert_not_called()
        load.assert_not_called()
    finally:
        store.close()


def test_worker_refuses_custom_db_until_normal_startup_recovers_settings_root(tmp_path, monkeypatch):
    source = AppController(SettingsStore(base_dir=tmp_path / "source"))
    target_store = SettingsStore(base_dir=tmp_path / "target")
    target_settings = target_store.load()
    target_settings["database_path"] = str(tmp_path / "separate-db" / "library.db")
    target_store.save(target_settings)
    target = AppController(target_store)
    class Interrupted(BaseException):
        pass
    try:
        for controller, label in ((source, "incoming"), (target, "current")):
            output = Path(controller.settings["output_dir"]) / "sample.md"
            output.write_text(label, encoding="utf-8")
            with controller.queue._conn:
                controller.queue._conn.execute("CREATE TABLE restore_marker(value TEXT)")
                controller.queue._conn.execute("INSERT INTO restore_marker VALUES (?)", (label,))
        source.save_settings({"theme": "paper"})
        target.save_settings({"theme": "dark"})
        archive = tmp_path / "source.zip"
        assert export_backup(source, source.settings_store, str(archive))["ok"]
        save = target_store.save
        with monkeypatch.context() as patch:
            def interrupt(settings):
                save(settings)
                raise Interrupted()
            patch.setattr(target_store, "save", interrupt)
            with pytest.raises(Interrupted):
                import_backup(target, target_store, str(archive))
        db = Path(target.settings["database_path"])
        output = Path(target.settings["output_dir"]) / "sample.md"
        target.close()
        target = None
        queue = worker.queue_store.QueueStore
        def create_restored_queue(path):
            assert not (target_store.base_dir / backup_restore.RECOVERY_DIRECTORY).exists()
            assert output.read_text(encoding="utf-8") == "current"
            assert target_store.load()["theme"] == "dark"
            result = queue(path)
            assert result._conn.execute("SELECT value FROM restore_marker").fetchone()[0] == "current"
            return result
        monkeypatch.setattr(worker.queue_store, "QueueStore", create_restored_queue)
        assert worker.main(["--db", str(db), "--settings-dir", str(target_store.base_dir), "--oneshot"]) == 2
        # The normal application's exclusive startup owns interrupted recovery;
        # a Worker must not roll back a possibly still-running import.
        assert backup_restore.recover_interrupted_import(target_store)
        assert worker.main(["--db", str(db), "--settings-dir", str(target_store.base_dir), "--oneshot"]) == 0
    finally:
        source.close()
        if target is not None:
            target.close()
