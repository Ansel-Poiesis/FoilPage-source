# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Media library import, deduplication, search and relocation tests."""
from __future__ import annotations

import sys
import sqlite3
import tempfile
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.media_store import (  # noqa: E402
    MediaStore,
    MediaStoreError,
    MediaTypeError,
    RelocationMismatchError,
)
from engine.queue_store import QueueStore  # noqa: E402


def test_import_is_idempotent_and_content_deduplicated() -> None:
    with tempfile.TemporaryDirectory(prefix="media_store_") as temp:
        root = Path(temp)
        db = root / "library.db"
        first = root / "first.wav"
        duplicate = root / "duplicate.wav"
        first.write_bytes(b"RIFF-same-audio")
        duplicate.write_bytes(first.read_bytes())
        store = MediaStore(str(db))
        try:
            added = store.import_file(str(first))
            repeated = store.import_file(str(first))
            same_content = store.import_file(str(duplicate))
            assert added.created is True
            assert repeated.created is False
            assert repeated.item["id"] == added.item["id"]
            assert same_content.created is False
            assert same_content.duplicate_of == added.item["id"]
            assert len(store.list_media()) == 1
        finally:
            store.close()


def test_directory_import_filters_and_searches() -> None:
    with tempfile.TemporaryDirectory(prefix="media_scan_") as temp:
        root = Path(temp)
        nested = root / "nested"
        nested.mkdir()
        (root / "Alpha.mp4").write_bytes(b"video-a")
        (nested / "Beta.MP3").write_bytes(b"audio-b")
        (nested / "notes.txt").write_text("ignore", encoding="utf-8")
        store = MediaStore(str(root / "library.db"))
        try:
            summary = store.import_directory(str(root), recursive=True)
            assert summary.created == 2
            assert summary.skipped == 1
            assert len(store.list_media(query="alpha")) == 1
            assert len(store.list_media(media_type="audio")) == 1
            assert len(store.list_media(media_type="video")) == 1
        finally:
            store.close()


def test_reimport_changed_file_refreshes_hash() -> None:
    with tempfile.TemporaryDirectory(prefix="media_changed_") as temp:
        root = Path(temp)
        source = root / "voice.wav"
        source.write_bytes(b"RIFF-before")
        store = MediaStore(str(root / "library.db"))
        try:
            before = store.import_file(str(source)).item
            source.write_bytes(b"RIFF-after-and-longer")
            after = store.import_file(str(source)).item
            assert after["id"] == before["id"]
            assert after["content_sha256"] != before["content_sha256"]
            assert after["size_bytes"] == len(b"RIFF-after-and-longer")
        finally:
            store.close()


def test_missing_refresh_and_relocation() -> None:
    with tempfile.TemporaryDirectory(prefix="media_missing_") as temp:
        root = Path(temp)
        original = root / "voice.wav"
        moved = root / "moved" / "voice.wav"
        original.write_bytes(b"RIFF-relocate")
        store = MediaStore(str(root / "library.db"))
        try:
            imported = store.import_file(str(original)).item
            moved.parent.mkdir()
            original.replace(moved)
            assert store.refresh_missing() == 1
            assert store.get_media(imported["id"])["missing"] is True
            relocated = store.relocate(imported["id"], str(moved))
            assert relocated["missing"] is False
            assert Path(relocated["source_path"]) == moved.resolve()
        finally:
            store.close()


def test_relocation_rejects_different_content() -> None:
    with tempfile.TemporaryDirectory(prefix="media_relocate_bad_") as temp:
        root = Path(temp)
        original = root / "voice.wav"
        other = root / "other.wav"
        original.write_bytes(b"RIFF-original")
        other.write_bytes(b"RIFF-different")
        store = MediaStore(str(root / "library.db"))
        try:
            media_id = store.import_file(str(original)).item["id"]
            original.unlink()
            try:
                store.relocate(media_id, str(other))
            except RelocationMismatchError:
                pass
            else:
                raise AssertionError("不同内容不应被接受为重新定位目标")
        finally:
            store.close()


def test_non_media_file_is_rejected_and_delete_works() -> None:
    with tempfile.TemporaryDirectory(prefix="media_delete_") as temp:
        root = Path(temp)
        text = root / "notes.txt"
        text.write_text("not media", encoding="utf-8")
        store = MediaStore(str(root / "library.db"))
        try:
            try:
                store.import_file(str(text))
            except MediaTypeError:
                pass
            else:
                raise AssertionError("非媒体文件应被拒绝")

            audio = root / "keep.wav"
            audio.write_bytes(b"RIFF-delete")
            media_id = store.import_file(str(audio)).item["id"]
            assert store.delete_media(media_id) is True
            assert store.delete_media(media_id) is False
        finally:
            store.close()


def test_clear_media_removes_records_without_deleting_source_files() -> None:
    with tempfile.TemporaryDirectory(prefix="media_clear_") as temp:
        root = Path(temp)
        first = root / "first.wav"
        second = root / "second.mp4"
        first.write_bytes(b"RIFF-first")
        second.write_bytes(b"video-second")
        store = MediaStore(str(root / "library.db"))
        try:
            store.import_file(str(first))
            store.import_file(str(second))
            assert store.clear_media() == 2
            assert store.list_media() == []
            assert first.is_file()
            assert second.is_file()
            assert store.clear_media() == 0
        finally:
            store.close()


def test_media_and_queue_tables_share_one_database() -> None:
    with tempfile.TemporaryDirectory(prefix="media_queue_db_") as temp:
        root = Path(temp)
        db = root / "library.db"
        source = root / "same.wav"
        source.write_bytes(b"RIFF-shared-db")
        media_store = MediaStore(str(db))
        queue_store = QueueStore(str(db))
        try:
            media_id = media_store.import_file(str(source)).item["id"]
            queue_store.enqueue({
                "id": "shared-job",
                "source_path": str(source.resolve()),
                "output_dir": str(root / "output"),
                "status": "queued",
                "whisper_model": "tiny",
                "device": "cpu",
                "language": "en",
                "content_mode": "raw",
                "created_at": "2026-07-23T10:00:00",
                "updated_at": "2026-07-23T10:00:00",
            })
            assert media_store.get_media(media_id)["filename"] == "same.wav"
            assert queue_store.get_job("shared-job")["status"] == "queued"
            from engine.queue_store import SCHEMA_VERSION
            assert queue_store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert media_store._conn.execute(
                "SELECT version FROM app_schema WHERE component='media'"
            ).fetchone()[0] == 2
        finally:
            queue_store.close()
            media_store.close()


def test_future_media_schema_is_refused_without_journal_or_schema_writes(tmp_path, monkeypatch):
    db = tmp_path / "future-media.db"
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("CREATE TABLE app_schema (component TEXT PRIMARY KEY, version INTEGER NOT NULL)")
        connection.execute("INSERT INTO app_schema VALUES ('media', 99)")
        connection.execute("CREATE TABLE future_data (value TEXT NOT NULL)")
        connection.execute("INSERT INTO future_data VALUES ('preserved')")
    fingerprint = sha256(db.read_bytes()).hexdigest()
    statements = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    with pytest.raises(MediaStoreError, match="媒体库版本 99.*支持版本 2"):
        MediaStore(str(db))
    assert sha256(db.read_bytes()).hexdigest() == fingerprint
    assert not any(sql.strip().upper().startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "PRAGMA JOURNAL_MODE="))
                   for sql in statements)
    assert not db.with_name(db.name + "-wal").exists()
    with closing(connect(db)) as connection:
        assert connection.execute("SELECT version FROM app_schema WHERE component='media'").fetchone()[0] == 99
        assert connection.execute("SELECT value FROM future_data").fetchone()[0] == "preserved"
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='media'").fetchone() is None


def test_media_migration_rolls_back_added_column_and_content_after_version_write_failure(tmp_path):
    from engine.media_store import _SCHEMA

    db = tmp_path / "legacy-media.db"
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.executescript(_SCHEMA)
        connection.execute("INSERT INTO app_schema VALUES ('media', 1)")
        connection.execute(
            """INSERT INTO media VALUES
               ('old', 'old.wav', 'old.wav', 'old.wav', '.wav', 'audio', 4, 1,
                'original-content', 1.0, 0, '2026-01-01', '2026-01-01')"""
        )
        connection.execute(
            """CREATE TRIGGER reject_version BEFORE UPDATE ON app_schema
               WHEN NEW.component='media' BEGIN SELECT RAISE(ABORT, 'version write failed'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="version write failed"):
        MediaStore(str(db))
    with closing(sqlite3.connect(db)) as connection, connection:
        assert connection.execute("SELECT version FROM app_schema WHERE component='media'").fetchone()[0] == 1
        assert "original_content_sha256" not in {row[1] for row in connection.execute("PRAGMA table_info(media)")}
        assert connection.execute("SELECT content_sha256 FROM media WHERE id='old'").fetchone()[0] == "original-content"
        connection.execute("DROP TRIGGER reject_version")
    with closing(MediaStore(str(db))) as store:
        assert store._conn.execute("SELECT original_content_sha256 FROM media WHERE id='old'").fetchone()[0] == "original-content"
        assert store._conn.execute("SELECT version FROM app_schema WHERE component='media'").fetchone()[0] == 2


def test_media_migration_rechecks_version_inside_transaction(tmp_path):
    with closing(MediaStore(str(tmp_path / "media.db"))) as store:
        with store._conn:
            store._conn.execute("UPDATE app_schema SET version=99 WHERE component='media'")
        schema = store._conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        with pytest.raises(MediaStoreError, match="媒体库版本 99"):
            store._migrate()
        assert store._conn.execute("SELECT version FROM app_schema WHERE component='media'").fetchone()[0] == 99
        assert store._conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == schema
        assert not store._conn.in_transaction


if __name__ == "__main__":
    test_import_is_idempotent_and_content_deduplicated()
    test_directory_import_filters_and_searches()
    test_reimport_changed_file_refreshes_hash()
    test_missing_refresh_and_relocation()
    test_relocation_rejects_different_content()
    test_non_media_file_is_rejected_and_delete_works()
    test_clear_media_removes_records_without_deleting_source_files()
    test_media_and_queue_tables_share_one_database()
    print("OK MediaStore tests passed")
