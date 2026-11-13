# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Queue startup reads a complete schema without competing with real writers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
import sqlite3
from threading import Barrier

import pytest

from engine import queue_store
from engine.media_store import MediaStore
from test_queue_store import _job


def trace_connections(monkeypatch):
    original_connect = sqlite3.connect
    statements = []
    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(queue_store.sqlite3, 'connect', connect)
    return statements


def mutating_schema(statements):
    return [sql for sql in statements if sql.strip().upper().startswith((
        'CREATE ', 'ALTER ', 'DROP ', 'BEGIN IMMEDIATE', 'PRAGMA USER_VERSION=',
    ))]


def test_complete_schema_open_while_writer_held_does_no_ddl_or_migration(tmp_path, monkeypatch):
    database = str(tmp_path / 'active.db')
    with closing(queue_store.QueueStore(database)) as initial:
        initial.enqueue(_job('startup-active'))
    with closing(sqlite3.connect(database)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("UPDATE jobs SET source_title='uncommitted' WHERE id='startup-active'")
        statements = trace_connections(monkeypatch)
        with closing(queue_store.QueueStore(database, busy_timeout_ms=0)) as reader:
            assert reader._conn.execute('PRAGMA busy_timeout').fetchone()[0] == 0
            assert reader.get_job('startup-active')['source_title'] != 'uncommitted'
            assert mutating_schema(statements) == []
            # The startup fast path does not bypass normal business write locks.
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                reader.claim_next('busy-worker')
            assert not reader._conn.in_transaction
        writer.rollback()
    with closing(queue_store.QueueStore(database)) as store:
        assert store.get_job('startup-active')['status'] == 'queued'
        assert store.get_job('startup-active')['attempts'] == 0


def test_media_component_version_does_not_prove_queue_schema(tmp_path, monkeypatch):
    database = str(tmp_path / 'shared.db')
    with closing(MediaStore(database)) as media:
        assert media._conn.execute("SELECT version FROM app_schema WHERE component='media'").fetchone()[0] == 2
        assert media._conn.execute('PRAGMA user_version').fetchone()[0] == 0
    statements = trace_connections(monkeypatch)
    with closing(queue_store.QueueStore(database)) as store:
        assert store._schema_is_current()
        assert store._conn.execute("SELECT version FROM app_schema WHERE component='media'").fetchone()[0] == 2
    assert any(sql.strip().upper().startswith('BEGIN IMMEDIATE') for sql in statements)
    statements.clear()
    with closing(sqlite3.connect(database)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        statements.clear()
        with closing(queue_store.QueueStore(database, busy_timeout_ms=0)) as reopened:
            assert reopened._schema_is_current()
            assert mutating_schema(statements) == []
        writer.rollback()


@pytest.mark.parametrize('version', [0, 6, 8])
def test_old_or_partially_migrated_database_two_connections_complete_once(tmp_path, version):
    database = str(tmp_path / 'old.db')
    legacy = queue_store._SCHEMA.replace("    metadata_json  TEXT NOT NULL DEFAULT '{}',\n", '')
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(legacy)
        connection.execute('ALTER TABLE jobs ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')
        connection.execute(f'PRAGMA user_version={version}')
        connection.execute('''INSERT INTO jobs(id,source_path,output_dir,status,whisper_model,device,
                           language,content_mode,created_at,updated_at) VALUES
                           ('old-preserved','source.mp4','output','queued','tiny','cpu','en','raw','old','old')''')
    barrier = Barrier(2)
    def open_old(_):
        barrier.wait(timeout=5)
        with closing(queue_store.QueueStore(database)) as store:
            assert store._schema_is_current()
            return store.get_job('old-preserved')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(open_old, range(2)))
    assert first == second
    assert first['source_path'] == 'source.mp4' and first['created_at'] == 'old' and first['tags'] == []
    with closing(queue_store.QueueStore(database)) as store:
        columns = [row['name'] for row in store._conn.execute('PRAGMA table_info(jobs)')]
        assert all(columns.count(name) == 1 for name in queue_store._MIGRATION_COLUMNS)
        assert store._conn.execute('PRAGMA user_version').fetchone()[0] == queue_store.SCHEMA_VERSION


def test_missing_status_index_repairs_even_when_user_version_is_current(tmp_path, monkeypatch):
    database = str(tmp_path / 'missing-index.db')
    with closing(queue_store.QueueStore(database)) as initial:
        initial.enqueue(_job('preserved-index'))
        initial._conn.execute('DROP INDEX idx_jobs_status_created')
    statements = trace_connections(monkeypatch)
    with closing(queue_store.QueueStore(database)) as repaired:
        assert repaired._schema_is_current()
        assert repaired.get_job('preserved-index')['status'] == 'queued'
    assert any(sql.strip().upper().startswith('CREATE INDEX') for sql in statements)
    assert any(sql.strip().upper().startswith('BEGIN IMMEDIATE') for sql in statements)


@pytest.mark.parametrize('definition', [
    'CREATE INDEX idx_jobs_status_created ON jobs(created_at,status)',
    'CREATE INDEX idx_jobs_status_created ON jobs(status,created_at DESC)',
    'CREATE INDEX idx_jobs_status_created ON jobs(status COLLATE NOCASE,created_at)',
    "CREATE INDEX idx_jobs_status_created ON jobs(status,created_at) WHERE status='queued'",
    'CREATE UNIQUE INDEX idx_jobs_status_created ON jobs(status,created_at)',
    'CREATE INDEX idx_jobs_status_created ON jobs(lower(status),created_at)',
])
def test_wrong_named_index_is_not_mistaken_for_complete_schema(tmp_path, monkeypatch, definition):
    database = str(tmp_path / 'wrong-index.db')
    with closing(queue_store.QueueStore(database)) as initial:
        initial._conn.execute('DROP INDEX idx_jobs_status_created')
        initial._conn.execute(definition)
        assert not initial._schema_is_current()
    statements = trace_connections(monkeypatch)
    with closing(queue_store.QueueStore(database)) as opened:
        # The fast path does not bless incompatible schema. Preserve the old
        # migration behavior rather than silently dropping somebody's index.
        assert not opened._schema_is_current()
    assert any(sql.strip().upper().startswith('BEGIN IMMEDIATE') for sql in statements)


@pytest.mark.parametrize('change', [
    ('source_path      TEXT NOT NULL', 'source_path      INTEGER NOT NULL'),
    ('source_path      TEXT NOT NULL', 'source_path      TEXT'),
    ("progress         REAL NOT NULL DEFAULT 0", "progress         REAL NOT NULL DEFAULT 99"),
    ('id               TEXT PRIMARY KEY', 'id               TEXT'),
    ('id               TEXT PRIMARY KEY', 'id               TEXT PRIMARY KEY COLLATE NOCASE'),
    ('job_id         TEXT PRIMARY KEY', 'job_id         TEXT PRIMARY KEY COLLATE NOCASE'),
    ('FOREIGN KEY (job_id) REFERENCES jobs(id)', 'FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE'),
])
def test_incompatible_required_column_primary_or_foreign_key_cannot_take_fast_path(tmp_path, monkeypatch, change):
    database = str(tmp_path / 'incompatible.db')
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(queue_store._SCHEMA.replace(*change))
        # Add the same migration fields but do not repair an incompatible base.
        columns = {row[1] for row in connection.execute('PRAGMA table_info(jobs)')}
        for name, definition in queue_store._MIGRATION_COLUMNS.items():
            if name not in columns:
                connection.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
        connection.execute(f'PRAGMA user_version={queue_store.SCHEMA_VERSION}')
    statements = trace_connections(monkeypatch)
    with closing(queue_store.QueueStore(database)) as opened:
        assert not opened._schema_is_current()
    assert any(sql.strip().upper().startswith('BEGIN IMMEDIATE') for sql in statements)


def test_complete_columns_with_stale_version_still_obey_original_busy_budget(tmp_path):
    database = str(tmp_path / 'stale-version.db')
    with closing(queue_store.QueueStore(database)) as initial:
        initial._conn.execute('PRAGMA user_version=6')
    with closing(sqlite3.connect(database)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            queue_store.QueueStore(database, busy_timeout_ms=0)
        writer.rollback()
    with closing(queue_store.QueueStore(database)) as migrated:
        assert migrated._schema_is_current()


def test_current_version_with_missing_table_does_not_skip_creation(tmp_path, monkeypatch):
    database = str(tmp_path / 'missing-table.db')
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(f'PRAGMA user_version={queue_store.SCHEMA_VERSION}')
    statements = trace_connections(monkeypatch)
    with closing(queue_store.QueueStore(database)) as opened:
        assert opened._schema_is_current()
    assert any(sql.strip().upper().startswith('CREATE TABLE') for sql in statements)
    assert any(sql.strip().upper().startswith('BEGIN IMMEDIATE') for sql in statements)


@pytest.mark.parametrize('complete_queue', [True, False])
def test_future_database_is_refused_before_wal_or_schema_changes(tmp_path, monkeypatch, complete_queue):
    database = tmp_path / 'future.db'
    if complete_queue:
        with closing(queue_store.QueueStore(str(database))) as store:
            store.enqueue(_job('future-preserved'))
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.execute('CREATE TABLE future_data (value TEXT NOT NULL)')
        connection.execute("INSERT INTO future_data VALUES ('future-preserved')")
        connection.execute(f'PRAGMA user_version={queue_store.SCHEMA_VERSION + 1}')
    original_hash = sha256(database.read_bytes()).hexdigest()
    statements = trace_connections(monkeypatch)
    with pytest.raises(queue_store.QueueStoreError, match=f'数据库版本 {queue_store.SCHEMA_VERSION + 1}.*支持版本 {queue_store.SCHEMA_VERSION}'):
        queue_store.QueueStore(str(database))
    assert sha256(database.read_bytes()).hexdigest() == original_hash
    assert mutating_schema(statements) == []
    assert not any(sql.strip().upper().startswith('PRAGMA JOURNAL_MODE=') for sql in statements)
    assert not (tmp_path / 'future.db-wal').exists()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == queue_store.SCHEMA_VERSION + 1
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert connection.execute('SELECT value FROM future_data').fetchone()[0] == 'future-preserved'
        if complete_queue:
            assert connection.execute("SELECT status FROM jobs WHERE id='future-preserved'").fetchone()[0] == 'queued'
        else:
            assert connection.execute("SELECT name FROM sqlite_master WHERE name IN ('jobs','manifests')").fetchall() == []
    assert sha256(database.read_bytes()).hexdigest() == original_hash


def test_migration_rechecks_future_version_before_stamping_or_altering(tmp_path):
    database = tmp_path / 'newer-migration.db'
    with closing(queue_store.QueueStore(str(database))) as store:
        store._conn.execute(f'PRAGMA user_version={queue_store.SCHEMA_VERSION + 1}')
        store._conn.commit()
        before = store._conn.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall()
        with pytest.raises(queue_store.QueueStoreError, match=f'数据库版本 {queue_store.SCHEMA_VERSION + 1}.*支持版本 {queue_store.SCHEMA_VERSION}'):
            store._migrate()
        assert store._conn.execute('PRAGMA user_version').fetchone()[0] == queue_store.SCHEMA_VERSION + 1
        assert store._conn.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall() == before
        assert not store._conn.in_transaction
