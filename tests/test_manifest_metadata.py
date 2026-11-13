# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Manifest metadata persistence, migration and credential boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3
from threading import Barrier

import pytest

from engine import queue_store, validate
from test_queue_store import _job, _manifest


def _complete(store, job_id="metadata"):
    store.enqueue(_job(job_id))
    store.claim_next("metadata-worker")
    manifest = _manifest(job_id)
    manifest.update({
        "source_title": "精确标题", "original_source_title": "原标题",
        "title_status": "done", "title_skill_version": "concise-accurate-title-v1",
        "title_model": "title-model", "title_error": "",
        "agent_profile": "knowledge", "agent_model": "writer",
        "agent_status": "done", "agent_prompt_version": "agent-v3-readable-fidelity",
        "agent_editing_level": "balanced", "agent_vision_used": True,
        "agent_vision_frames": 2, "agent_fidelity_fallback": False,
        "agent_tags": ["记忆", "研究"], "segments_path": "C:/output/segments.json",
        "asr_avg_logprob": -0.42, "asr_no_speech_prob": 0.07,
        "asr_low_confidence": False,
        "agent_api_key": "credential-that-must-not-be-stored",
        "cookie": "session-that-must-not-be-stored",
        "url": "https://media.example/watch?token=private",
        "provider_response": {"api_key": "secret"},
    })
    store.save_manifest(manifest, worker_id="metadata-worker")
    return manifest


def test_metadata_roundtrip_after_reopen_and_replacement_excludes_credentials(tmp_path):
    db = str(tmp_path / "library.db")
    with_store = queue_store.QueueStore(db)
    original = _complete(with_store)
    with_store.close()
    store = queue_store.QueueStore(db)
    try:
        saved = store.get_manifest("metadata")
        expected = {key: value for key, value in original.items()
                    if key not in {"agent_api_key", "cookie", "url", "provider_response"}}
        assert saved == expected
        stored_json = store._conn.execute("SELECT metadata_json FROM manifests").fetchone()[0]
        assert "credential" not in stored_json and "private" not in stored_json
        assert "session" not in stored_json and "api_key" not in stored_json
        changed = {**saved, "source_title": "再次优化", "title_status": "skipped",
                   "agent_vision_used": False, "agent_tags": ["复盘"]}
        store.replace_manifest(changed)
        assert store.get_manifest("metadata") == changed
    finally:
        store.close()
    reopened = queue_store.QueueStore(db)
    try:
        assert reopened.get_manifest("metadata") == changed
    finally:
        reopened.close()


def test_old_database_metadata_migration_is_idempotent_and_concurrent(tmp_path):
    db = str(tmp_path / "old.db")
    manifest = _manifest("old")
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.executescript(queue_store._SCHEMA.replace(
            "    metadata_json  TEXT NOT NULL DEFAULT '{}',\n", ""))
        connection.execute("PRAGMA user_version=6")
        job = _job("old")
        connection.execute("""INSERT INTO jobs
            (id,source_path,output_dir,status,whisper_model,device,language,
             content_mode,created_at,updated_at) VALUES
            (:id,:source_path,:output_dir,'done',:whisper_model,:device,:language,
             :content_mode,:created_at,:updated_at)""", job)
        names = tuple(manifest)
        connection.execute(
            "INSERT INTO manifests (" + ",".join(names) + ") VALUES (" +
            ",".join(":" + name for name in names) + ")", manifest)
    start = Barrier(2)
    def open_old(_):
        start.wait(timeout=5)
        store = queue_store.QueueStore(db)
        try:
            return store.get_manifest("old")
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(open_old, range(2))) == [manifest, manifest]
    store = queue_store.QueueStore(db)
    try:
        columns = [row["name"] for row in store._conn.execute("PRAGMA table_info(manifests)")]
        assert columns.count("metadata_json") == 1
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == queue_store.SCHEMA_VERSION
        assert json.loads(store._conn.execute("SELECT metadata_json FROM manifests").fetchone()[0]) == {}
        replacement = {**manifest, "source_title": "旧库新标题", "title_status": "done"}
        store.replace_manifest(replacement)
        assert store.get_manifest("old") == replacement
    finally:
        store.close()


@pytest.mark.parametrize("value", ["broken", "[1]", '{"job_id":"wrong","title_status":{}}'])
def test_invalid_optional_metadata_preserves_core_result(tmp_path, value):
    store = queue_store.QueueStore(str(tmp_path / "library.db"))
    try:
        _complete(store)
        with store._conn:
            store._conn.execute("UPDATE manifests SET metadata_json=?", (value,))
        assert store.get_manifest("metadata") == _manifest_core(store)
    finally:
        store.close()


def _manifest_core(store):
    row = store._conn.execute("SELECT * FROM manifests").fetchone()
    return {key: row[key] for key in _manifest("metadata")}


def test_nested_metadata_rejected_before_completion(tmp_path):
    store = queue_store.QueueStore(str(tmp_path / "library.db"))
    try:
        store.enqueue(_job("invalid"))
        store.claim_next("worker")
        manifest = {**_manifest("invalid"), "agent_tags": [{"api_key": "secret"}]}
        with pytest.raises(validate.ContractValidationError):
            store.save_manifest(manifest, worker_id="worker")
        assert store.get_job("invalid")["status"] == "running"
        assert store.get_manifest("invalid") is None
    finally:
        store.close()


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        assert 0 < duration <= 0.01
        self.sleeps.append(duration)
        self.now += duration


def _inject_wal_error(monkeypatch, error, *, failures):
    original_connect = sqlite3.connect
    connections = []

    class Connection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.wal_attempts = 0
            self.timeouts = []
            self.closed = False
            connections.append(self)

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("PRAGMA busy_timeout="):
                self.timeouts.append(int(sql.split("=")[1]))
            if sql == "PRAGMA journal_mode=WAL":
                self.wal_attempts += 1
                if failures is None or self.wal_attempts <= failures:
                    raise error
            return super().execute(sql, *args, **kwargs)

        def close(self):
            self.closed = True
            super().close()

    def connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=Connection)

    monkeypatch.setattr(queue_store.sqlite3, "connect", connect)
    return connections


def _sqlite_error(code):
    error = sqlite3.OperationalError("database is locked")
    if code is not None:
        error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                                  sqlite3.SQLITE_BUSY_SNAPSHOT])
def test_initial_wal_lock_retries_once_and_keeps_timeout(monkeypatch, code):
    clock = _Clock()
    monkeypatch.setattr(queue_store.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(queue_store.time, "sleep", clock.sleep)
    connections = _inject_wal_error(monkeypatch, _sqlite_error(code), failures=1)
    store = queue_store.QueueStore(busy_timeout_ms=50)
    try:
        assert store.list_jobs() == []
        assert connections[0].wal_attempts == 2
        assert clock.sleeps == [0.01]
        assert connections[0].timeouts == [50, 40, 50]
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 50
    finally:
        store.close()
    assert connections[0].closed


@pytest.mark.parametrize("timeout_ms", [0, 25])
def test_initial_wal_permanent_lock_expires_within_one_budget_and_closes(monkeypatch, timeout_ms):
    clock = _Clock()
    monkeypatch.setattr(queue_store.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(queue_store.time, "sleep", clock.sleep)
    error = _sqlite_error(sqlite3.SQLITE_BUSY)
    connections = _inject_wal_error(monkeypatch, error, failures=None)
    with pytest.raises(sqlite3.OperationalError) as caught:
        queue_store.QueueStore(busy_timeout_ms=timeout_ms)
    assert caught.value is error
    assert connections[0].closed
    assert clock.now == pytest.approx(timeout_ms / 1000)
    assert 1 <= connections[0].wal_attempts <= 4
    assert connections[0].timeouts[-1] == timeout_ms
    assert all(0 <= value <= timeout_ms for value in connections[0].timeouts)
    assert connections[0].timeouts[:-1] == sorted(connections[0].timeouts[:-1], reverse=True)


@pytest.mark.parametrize("code", [sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CORRUPT, None])
def test_initial_wal_non_lock_error_propagates_without_retry_and_closes(monkeypatch, code):
    clock = _Clock()
    monkeypatch.setattr(queue_store.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(queue_store.time, "sleep", clock.sleep)
    error = _sqlite_error(code)  # Message alone must not classify an error as a lock.
    connections = _inject_wal_error(monkeypatch, error, failures=None)
    with pytest.raises(sqlite3.OperationalError) as caught:
        queue_store.QueueStore(busy_timeout_ms=50)
    assert caught.value is error
    assert connections[0].closed
    assert connections[0].wal_attempts == 1
    assert clock.sleeps == []
