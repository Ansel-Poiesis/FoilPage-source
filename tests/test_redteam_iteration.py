# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Bounded local stress: isolated DB/ephemeral loopback, no platform or model calls."""
from concurrent.futures import ThreadPoolExecutor
import socket
import threading

import pytest

from app.controller import AppController
from app.local_api import LocalApiServer
from engine.queue_store import QueueStore
from engine.settings import SettingsStore
from test_queue_store import _job, _manifest
from test_local_api import StubCoordinator, call, ORIGIN
from test_redteam_bridge import make_bridge, _row


@pytest.mark.parametrize("error", ["older error text", ["malformed"], 123, None, {"message": None}])
def test_one_bad_error_does_not_hide_all_tasks(error):
    bridge, _ = make_bridge([_row("download", "bad", status="failed", error=error), _row("transcription", "good")])
    rows = bridge.get_tasks()
    assert len(rows) == 2
    assert isinstance(rows[0]["error"], str)


def test_occupied_port_does_not_start_downloader(tmp_path):
    controller = AppController(SettingsStore(base_dir=tmp_path / "data"))
    coordinator = StubCoordinator()
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        server = LocalApiServer(controller, coordinator, port=occupied.getsockname()[1])
        try:
            with pytest.raises(OSError):
                server.start()
            assert not coordinator.started
            assert not server.running
        finally:
            controller.close()


def test_80_jobs_8_independent_connections_claim_once_and_cancel(tmp_path):
    db = str(tmp_path / "stress.db")
    store = QueueStore(db)
    try:
        ids = [f"stress{i:06d}" for i in range(80)]
        for job_id in ids:
            store.enqueue(_job(job_id))
        cancelled = set(ids[::4])
        for job_id in cancelled:
            store.request_cancel(job_id)

        def drain(number):
            connection = QueueStore(db)
            claimed = []
            try:
                while task := connection.claim_next(f"stress-{number}"):
                    claimed.append(task["id"])
                    connection.update_progress(task["id"], .42, f"stress-{number}")
                    connection.save_manifest(_manifest(task["id"]), worker_id=f"stress-{number}")
            finally:
                connection.close()
            return claimed

        with ThreadPoolExecutor(max_workers=8) as pool:
            all_ids = [job_id for group in pool.map(drain, range(8)) for job_id in group]
        assert len(all_ids) == len(set(all_ids)) == 60
        assert set(all_ids) == set(ids) - cancelled
        assert all(store.get_job(job_id)["status"] == "cancelled" for job_id in cancelled)
        assert all(store.get_job(job_id)["progress"] == 1 for job_id in all_ids)
    finally:
        store.close()


@pytest.mark.parametrize("endpoint", ["/v1/pair", "/v1/setup"])
def test_concurrent_pair_is_one_shot_and_invalid_auth_does_not_queue(tmp_path, endpoint):
    controller = AppController(SettingsStore(base_dir=tmp_path / "data"))
    coordinator = StubCoordinator()
    setup_calls = []
    server = LocalApiServer(controller, coordinator, port=0,
                            setup_callback=lambda: setup_calls.append(True) or {})
    server.start()
    try:
        server.begin_pairing()
        barrier = threading.Barrier(16)

        def pair_once(_):
            barrier.wait(timeout=10)
            return call(server, "POST", endpoint, origin=ORIGIN, body={})[0]

        with ThreadPoolExecutor(max_workers=16) as pool:
            statuses = list(pool.map(pair_once, range(16)))
            rejected = list(pool.map(lambda _: call(server,"POST","/v1/downloads",token="invalid",origin=ORIGIN,body={"url":"https://www.bilibili.com/video/BV1test"})[0],range(32)))
        assert statuses.count(200) == 1
        assert len(setup_calls) == (1 if endpoint == "/v1/setup" else 0)
        assert all(status in [200,403] for status in statuses)
        assert set(rejected) == {401}
        assert not coordinator.jobs
        assert call(server,"GET","/health")[0] == 200
    finally:
        server.stop()
        controller.close()
