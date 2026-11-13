# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Actual independent SQLite connections exercise queue writer ordering."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from engine.queue_store import CancellationRequestedError, QueueStateError, QueueStore
from test_queue_store import _job, _manifest, _ts


@pytest.mark.parametrize("operation", ["progress", "manifest", "cancel", "recover"])
def test_writer_waits_before_reading_state_changed_by_another_connection(tmp_path, operation):
    database = str(tmp_path / "writer-order.db")
    writer = QueueStore(database)
    worker = QueueStore(database)
    identifier = "writer-order"
    try:
        writer.enqueue(_job(identifier))
        if operation != "cancel":
            writer.claim_next("owner")
        if operation == "recover":
            with writer._conn:
                writer._conn.execute("UPDATE jobs SET heartbeat_at='2000-01-01T00:00:00' WHERE id=?", (identifier,))

        # Publish the earlier writer only after the second connection tries its
        # operation. A deferred SELECT can see the old state in WAL mode, while
        # BEGIN IMMEDIATE must wait before validating status/owner/cancellation.
        writer._conn.execute("BEGIN IMMEDIATE")
        if operation == "progress":
            writer._conn.execute("""UPDATE jobs SET status='queued', worker_id=NULL,
                claimed_at=NULL, heartbeat_at=NULL, progress=0 WHERE id=?""", (identifier,))
        elif operation == "manifest":
            writer._conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (identifier,))
        elif operation == "cancel":
            writer._conn.execute("""UPDATE jobs SET status='running', worker_id='new-owner',
                attempts=1, claimed_at=?, heartbeat_at=? WHERE id=?""", (_ts(), _ts(), identifier))
        else:
            writer._conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (_ts(), identifier))

        attempted = threading.Event()
        selected = threading.Event()

        def trace(sql):
            normalized = sql.lstrip().upper()
            if normalized.startswith("SELECT"):
                selected.set()
            if normalized.startswith(("BEGIN", "SELECT")):
                attempted.set()

        worker._conn.set_trace_callback(trace)
        operations = {
            "progress": lambda: worker.update_progress(identifier, .42, "owner"),
            "manifest": lambda: worker.save_manifest(_manifest(identifier), worker_id="owner"),
            "cancel": lambda: worker.request_cancel(identifier),
            "recover": lambda: worker.recover_stale_jobs(60),
        }
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(operations[operation])
            try:
                assert attempted.wait(5), "queue operation did not reach SQLite"
                assert not selected.is_set(), "queue validated a stale row before acquiring the writer"
            finally:
                writer._conn.commit()
            if operation == "progress":
                with pytest.raises(QueueStateError, match="running"):
                    pending.result(timeout=5)
            elif operation == "manifest":
                with pytest.raises(CancellationRequestedError):
                    pending.result(timeout=5)
            elif operation == "cancel":
                result = pending.result(timeout=5)
                assert result["status"] == "running" and result["cancel_requested"]
                assert result["worker_id"] == "new-owner" and result["attempts"] == 1
            else:
                assert pending.result(timeout=5) == []

        job = writer.get_job(identifier)
        assert writer.get_manifest(identifier) is None
        assert not worker._conn.in_transaction
        if operation == "progress":
            assert job["status"] == "queued" and job["progress"] == 0
        elif operation == "manifest":
            assert job["status"] == "running" and job["cancel_requested"]
        elif operation == "recover":
            assert job["status"] == "running" and job["worker_id"] == "owner"
    finally:
        if writer._conn.in_transaction:
            writer._conn.rollback()
        worker.close()
        writer.close()


@pytest.mark.parametrize("failure", ["completion", "stage_metadata"])
def test_manifest_and_job_changes_roll_back_together_after_sql_failure(tmp_path, failure):
    database = str(tmp_path / "manifest-rollback.db")
    store = QueueStore(database)
    observer = QueueStore(database)
    try:
        identifier = "manifest-rollback"
        store.enqueue(_job(identifier))
        before = store.claim_next("owner")
        operation = "status" if failure == "completion" else "stage_metadata_json"
        store._conn.execute(f"""CREATE TEMP TRIGGER reject_job_completion
            BEFORE UPDATE OF {operation} ON jobs WHEN NEW.status='done'
            BEGIN SELECT RAISE(ABORT, 'fixture completion failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="fixture completion failure"):
            store.save_manifest(_manifest(identifier), worker_id="owner")
        assert observer.get_manifest(identifier) is None
        assert observer.get_job(identifier) == before
        assert not store._conn.in_transaction
        store._conn.execute("DROP TRIGGER reject_job_completion")
        store.save_manifest(_manifest(identifier), worker_id="owner")
        assert observer.get_job(identifier)["status"] == "done"
        assert observer.get_manifest(identifier) is not None
    finally:
        observer.close()
        store.close()


def test_writer_lock_error_uses_the_existing_timeout_without_replaying_or_reading(tmp_path):
    database = str(tmp_path / "busy-timeout.db")
    writer = QueueStore(database)
    worker = QueueStore(database, busy_timeout_ms=0)
    try:
        identifier = "busy-timeout"
        writer.enqueue(_job(identifier))
        before = writer.claim_next("owner")
        statements = []
        worker._conn.set_trace_callback(statements.append)
        writer._conn.execute("BEGIN IMMEDIATE")
        writer._conn.execute("UPDATE jobs SET progress=.12 WHERE id=?", (identifier,))
        with pytest.raises(sqlite3.OperationalError) as failure:
            worker.update_progress(identifier, .42, "owner")
        assert failure.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert [statement.strip().upper() for statement in statements] == ["BEGIN IMMEDIATE"]
        assert not worker._conn.in_transaction
        assert worker._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        writer._conn.rollback()
        assert worker.get_job(identifier) == before
        assert worker.update_progress(identifier, .42, "owner")["progress"] == .42
    finally:
        if writer._conn.in_transaction:
            writer._conn.rollback()
        worker.close()
        writer.close()
