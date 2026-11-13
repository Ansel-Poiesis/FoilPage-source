# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Verified local accounts gate new work without losing existing queue work."""
from __future__ import annotations

import http.client
import json
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from account_auth_fixtures import grant_account
from app.bridge import Bridge
from app.controller import AppController
from app.download_coordinator import DownloadCoordinator
from app.local_api import LocalApiServer
from app.worker import Worker
from engine.account_access import AccountLoginRequired, require_account_access
from engine.queue_store import QueueStore
from engine.settings import SettingsStore
from test_bridge import FakeController, FakeLocalApi, FakeWindow, FakeWorker
from test_local_api import ORIGIN, StubCoordinator, call
from test_worker_runtime import _enqueue, _manifest
import run_engine


def denied_settings(base_dir: Path, reason: str) -> dict:
    settings = grant_account({}, base_dir)
    if reason == "missing":
        settings["account_login_token"] = ""
    elif reason == "expired":
        grant_account(settings, base_dir, expires_at=time.time() - 1)
    elif reason == "wrong_user":
        settings["account_user_id"] = "different-user"
    elif reason == "wrong_device":
        grant_account(settings, base_dir, device="different-device")
    elif reason == "forged":
        settings["license_token"] = "edited-local-license"
    elif reason == "unconfigured":
        settings["subscription_license_public_key"] = ""
    else:
        raise AssertionError(reason)
    settings.update(account_access={"allowed": True}, subscription_active=True)
    return settings


DENIALS = ["missing", "expired", "wrong_user", "wrong_device", "forged", "unconfigured"]


@pytest.mark.parametrize("reason", DENIALS)
def test_actual_device_verification_rejects_local_bypass(tmp_path, reason):
    settings = denied_settings(tmp_path, reason)
    with pytest.raises(AccountLoginRequired) as error:
        require_account_access(settings, tmp_path)
    assert error.value.code == "login_required"
    assert str(error.value) == "请先登录 FoilPage，登录后继续"


def _bridge(base_dir, settings):
    controller = FakeController(settings=settings)
    window = FakeWindow()
    window.create_file_dialog = mock.Mock(return_value=[str(base_dir / "sample.mp4")])
    worker = FakeWorker()
    worker.start = mock.Mock(wraps=worker.start)
    api = FakeLocalApi()
    bridge = Bridge(controller, worker, SimpleNamespace(base_dir=base_dir),
                    window_provider=lambda: window, on_quit=lambda: None,
                    local_api_provider=lambda: api)
    return bridge, controller, worker, window, api


@pytest.mark.parametrize("method,args", [
    ("import_files", (["C:/fixture/video.mp4"],)),
    ("pick_and_import", ()), ("pick_folder_and_import", ()),
    ("download_from_url", ("https://www.youtube.com/watch?v=fixture",)),
    ("transcribe_download", ("dl:012345abcdef",)),
    ("requeue_tasks", (["job:fixture-job"],)),
    ("retranscribe_tasks", (["job:fixture-job"],)),
    ("retry_downloads", (["dl:012345abcdef"],)),
    ("batch_agent_translate", (["job:fixture-job"],)),
    ("worker_toggle", ()),
])
def test_bridge_new_work_requires_login_before_any_side_effect(tmp_path, method, args):
    bridge, controller, worker, window, api = _bridge(tmp_path, denied_settings(tmp_path, "missing"))
    guarded = ["import_files", "enqueue_media", "requeue_jobs", "retranscribe_jobs",
               "queue_download_for_transcription"]
    for name in guarded:
        setattr(controller, name, mock.Mock(wraps=getattr(controller, name)))
    result = getattr(bridge, method)(*args)
    assert result == {"ok": False, "code": "login_required", "error": "请先登录 FoilPage，登录后继续"}
    for name in guarded:
        getattr(controller, name).assert_not_called()
    controller.downloads.get.assert_not_called()
    controller.queue.get_job.assert_not_called()
    window.create_file_dialog.assert_not_called()
    worker.start.assert_not_called()
    assert not api.submitted and not api.desktop_douyin_submitted
    assert bridge._batch_agent["running"] is False


@pytest.mark.parametrize("reason", DENIALS)
def test_bridge_rejects_invalid_signed_identity_and_valid_free_login_restores_entry(tmp_path, reason):
    bridge, controller, worker, _window, _api = _bridge(tmp_path, denied_settings(tmp_path, reason))
    assert bridge.import_files(["C:/fixture/video.mp4"])["code"] == "login_required"
    grant_account(controller.settings, tmp_path)
    result = bridge.import_files(["C:/fixture/video.mp4"])
    assert result["ok"] and result["imported"] == 1 and result["enqueued"] == ["j1"]
    assert bridge.worker_toggle()["running"] is True
    worker.start.assert_called_once()


@pytest.fixture
def local_server(tmp_path):
    settings_store = SettingsStore(base_dir=tmp_path / "data")
    controller = AppController(settings_store)
    coordinator = StubCoordinator()
    coordinator.submit_local_file = mock.Mock(wraps=coordinator.submit_local_file)
    server = LocalApiServer(controller, coordinator, port=0)
    server.start()
    try:
        yield server, controller, coordinator
    finally:
        server.stop()
        controller.queue.close()
        controller.media.close()
        controller.downloads.close()


@pytest.mark.parametrize("reason", DENIALS)
def test_loopback_new_download_import_batch_retry_are_closed_without_verified_account(local_server, reason):
    server, controller, coordinator = local_server
    controller.settings.update(denied_settings(controller.settings_store.base_dir, reason))
    token = controller.settings["local_api_token"]
    original = coordinator.submit("https://www.youtube.com/watch?v=accepted", options={})
    original["status"] = "failed"
    snapshot = dict(original)
    paths = [("/v1/downloads", {"url": "https://www.youtube.com/watch?v=new"}),
             ("/v1/imports", {"path": str(controller.settings_store.base_dir / "sample.mp4")}),
             ("/v1/downloads/batch", {"items": [{"url": "https://www.youtube.com/watch?v=new"}]}),
             (f"/v1/downloads/{original['id']}/retry", {})]
    for path, body in paths:
        status, data, _ = call(server, "POST", path, token=token, body=body)
        assert status == 403 and data["error"]["code"] == "login_required"
    assert len(coordinator.jobs) == 1 and original == snapshot
    coordinator.submit_local_file.assert_not_called()
    assert controller.queue.list_jobs() == []
    grant_account(controller.settings, controller.settings_store.base_dir)
    status, data, _ = call(server, "POST", "/v1/downloads", token=token,
                          body={"url": "https://www.youtube.com/watch?v=new"})
    assert status == 202 and data["job"]["id"] == coordinator.jobs[-1]["id"]
    assert len(coordinator.jobs) == 2


@pytest.mark.parametrize("original_transcribe", [False, True])
def test_logout_allows_only_existing_browser_import_with_original_transcription_snapshot(local_server, original_transcribe):
    server, controller, coordinator = local_server
    base_dir = controller.settings_store.base_dir
    media = Path(controller.settings["download_dir"]) / "accepted.mp4"
    media.write_bytes(b"fixture-media")
    job = coordinator.submit("https://www.douyin.com/video/123", mode="browser",
                             options={"mode": "browser", "transcribe_after_download": original_transcribe})
    controller.settings.update(denied_settings(base_dir, "missing"))
    token = controller.settings["local_api_token"]
    body = {"path": str(media), "browser_download": True, "download_id": job["id"],
            "transcribe": not original_transcribe}
    status, data, _ = call(server, "POST", "/v1/imports", token=token, body=body)
    assert status == 202 and data["job"]["download_id"] == job["id"]
    assert coordinator.submit_local_file.call_args.kwargs["transcribe"] is original_transcribe
    assert coordinator.submit_local_file.call_args.kwargs["validate_media"] is True
    coordinator.submit_local_file.reset_mock()
    for changes in ({"download_id": "abcdefabcdef"}, {"browser_download": False}):
        status, data, _ = call(server, "POST", "/v1/imports", token=token, body={**body, **changes})
        assert status == 403 and data["error"]["code"] == "login_required"
    coordinator.submit_local_file.assert_not_called()
    status, _, _ = call(server, "POST", f"/v1/downloads/{job['id']}/cancel", token=token, body={})
    assert status == 200 and job["status"] == "cancelled"
    assert call(server, "GET", "/v1/downloads", token=token)[0] == 200
    assert call(server, "GET", "/health")[0] == 200
    server.begin_pairing()
    status, data, _ = call(server, "POST", "/v1/pair", origin=ORIGIN)
    assert status == 200 and data["token"] == token


def test_unread_accepted_response_replays_receipt_after_logout_without_new_credentials(tmp_path):
    settings = SettingsStore(base_dir=tmp_path / "data")
    grant_account(settings, settings.base_dir)
    controller = AppController(settings)
    coordinator = DownloadCoordinator(controller)
    server = LocalApiServer(controller, coordinator, port=0)
    accepted = threading.Event()
    original_submit = coordinator.submit

    def submit_and_signal(*args, **kwargs):
        result = original_submit(*args, **kwargs)
        accepted.set()
        return result

    connection = None
    try:
        # Exercise the actual HTTP handler, coordinator and durable receipt.
        # Only the download consumer is suspended; no external transfer runs.
        with mock.patch.object(coordinator, "start"), mock.patch.object(coordinator, "submit", side_effect=submit_and_signal) as submit:
            server.start()
            token = controller.settings["local_api_token"]
            body = {"url": "https://www.douyin.com/video/1234567890123456789",
                    "client_request_id": "a" * 32,
                    "options": {"transcribe_after_download": False}}
            connection = http.client.HTTPConnection(*server.address, timeout=3)
            connection.request("POST", "/v1/downloads", body=json.dumps(body).encode("utf-8"),
                               headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            assert accepted.wait(2)
            # Leave this response unread, as if the client's reply was lost.
            receipt = controller.downloads.find_by_client_request_id(body["client_request_id"])
            assert receipt is not None and receipt["status"] == "received"
            before = controller.downloads.get(receipt["id"])
            submit.assert_called_once()
            submit.reset_mock()
            controller.settings["account_login_token"] = ""
            replay = {**body, "title": "must not replace title", "browser_session": {"invalid": "must not consume"},
                      "media_candidates": "must not consume", "media_url": "must not consume"}
            with mock.patch("app.download_coordinator.sanitize_douyin_session_cookies", side_effect=AssertionError("replay read cookies")):
                status, data, _ = call(server, "POST", "/v1/downloads", token=token, body=replay)
            assert status == 202 and data["job"]["id"] == receipt["id"]
            submit.assert_not_called()
            assert coordinator._transient_sessions == {} and coordinator._transient_media_hints == {}
            for changes, expected_status, expected_code in [
                ({"client_request_id": "b" * 32}, 403, "login_required"),
                ({"url": "https://www.douyin.com/video/9876543210987654321"}, 400, "invalid_client_request_id"),
                ({"options": {"transcribe_after_download": True}}, 400, "invalid_client_request_id"),
            ]:
                status, data, _ = call(server, "POST", "/v1/downloads", token=token, body={**replay, **changes})
                assert status == expected_status and data["error"]["code"] == expected_code
            submit.assert_not_called()
            assert controller.downloads.get(receipt["id"]) == before
            assert len(controller.downloads.list()) == 1 and controller.queue.list_jobs() == []
    finally:
        if connection:
            connection.close()
        server.stop()
        controller.queue.close()
        controller.media.close()
        controller.downloads.close()


@pytest.mark.parametrize("request_id", [str(uuid.uuid4()), "Ab_0-" * 2 + "Z9", "Ab_0" * 16],
                         ids=["standard-v4-uuid", "12-characters", "64-characters"])
def test_http_receipts_keep_existing_request_id_contract_after_logout(tmp_path, request_id):
    settings = SettingsStore(base_dir=tmp_path / "data")
    grant_account(settings, settings.base_dir)
    controller = AppController(settings)
    coordinator = DownloadCoordinator(controller)
    server = LocalApiServer(controller, coordinator, port=0)
    try:
        with mock.patch.object(coordinator, "start"), mock.patch.object(coordinator, "submit", wraps=coordinator.submit) as submit:
            server.start()
            token = controller.settings["local_api_token"]
            body = {"url": "https://www.youtube.com/watch?v=fixtureVideo",
                    "client_request_id": request_id,
                    "options": {"transcribe_after_download": False}}
            status, data, _ = call(server, "POST", "/v1/downloads", token=token, body=body)
            assert status == 202
            receipt = controller.downloads.find_by_client_request_id(request_id)
            assert receipt is not None and receipt["id"] == data["job"]["id"]
            before = controller.downloads.get(receipt["id"])
            submit.assert_called_once()
            submit.reset_mock()
            controller.settings["account_login_token"] = ""
            status, data, _ = call(server, "POST", "/v1/downloads", token=token, body=body)
            assert status == 202 and data["job"]["id"] == receipt["id"]
            status, data, _ = call(server, "POST", "/v1/downloads", token=token,
                                  body={**body, "client_request_id": "unknown-" + request_id[:40]})
            assert status == 403 and data["error"]["code"] == "login_required"
            submit.assert_not_called()
            assert controller.downloads.get(receipt["id"]) == before
            assert len(controller.downloads.list()) == 1
    finally:
        server.stop()
        controller.close()


@pytest.mark.parametrize("request_id", ["a" * 11, "b" * 65, "bad receipt!", "非法凭据abcdefgh", 0, False, ["not-a-string"]])
def test_http_invalid_receipt_ids_never_lookup_or_create_work(local_server, request_id):
    server, controller, coordinator = local_server
    token = controller.settings["local_api_token"]
    with mock.patch.object(controller.downloads, "find_by_client_request_id", wraps=controller.downloads.find_by_client_request_id) as lookup:
        for logged_in in [True, False]:
            if logged_in:
                grant_account(controller.settings, controller.settings_store.base_dir)
            else:
                controller.settings["account_login_token"] = ""
            status, data, _ = call(server, "POST", "/v1/downloads", token=token,
                                  body={"url": "https://www.youtube.com/watch?v=fixtureVideo", "client_request_id": request_id})
            assert status == 400 and data["error"]["code"] == "invalid_client_request_id"
        lookup.assert_not_called()
    assert not coordinator.jobs and controller.downloads.list() == [] and controller.queue.list_jobs() == []


@pytest.mark.parametrize("reason", DENIALS)
def test_worker_preserves_queued_progress_and_attempts_until_real_login(tmp_path, monkeypatch, reason):
    settings = SettingsStore(base_dir=tmp_path)
    settings.update(**denied_settings(tmp_path, reason))
    store = QueueStore(str(tmp_path / "queue.db"))
    try:
        job_id = _enqueue(store, "C:/fixture/accepted.wav", str(tmp_path / "output"))
        store._conn.execute("UPDATE jobs SET progress=0.42,attempts=1 WHERE id=?", (job_id,))
        store._conn.commit()
        before = store.get_job(job_id)
        instance = Worker(store, settings_store=settings, worker_id="verified-worker")
        with mock.patch.object(store, "claim_next", wraps=store.claim_next) as claim:
            assert instance.step() is False and instance.account_waiting is True
            claim.assert_not_called()
        assert store.get_job(job_id) == before and store.get_manifest(job_id) is None
        grant_account(settings, tmp_path)
        monkeypatch.setattr(run_engine, "run", lambda *_a, **_kw: _manifest(job_id, before["source_path"], before["output_dir"]))
        assert instance.step() is True and instance.account_waiting is False
        after = store.get_job(job_id)
        assert after["status"] == "done" and after["attempts"] == before["attempts"] + 1
        assert after["progress"] == 1 and store.get_manifest(job_id)["job_id"] == job_id
    finally:
        store.close()


def test_worker_without_settings_store_still_needs_real_db_root_bound_account(tmp_path, monkeypatch):
    store = QueueStore(str(tmp_path / "queue.db"))
    try:
        job_id = _enqueue(store, "C:/fixture/offline.wav", str(tmp_path / "output"))
        instance = Worker(store, worker_id="offline-worker")
        assert instance.step() is False and store.get_job(job_id)["status"] == "queued"
        instance.agent_settings = grant_account({}, tmp_path)
        job = store.get_job(job_id)
        monkeypatch.setattr(run_engine, "run", lambda *_a, **_kw: _manifest(job_id, job["source_path"], job["output_dir"]))
        assert instance.step() is True and store.get_job(job_id)["status"] == "done"
    finally:
        store.close()


@pytest.mark.parametrize("invalid_document", ["{broken-json", "[]"])
def test_unreadable_account_settings_pause_worker_without_rewriting_queue_or_settings(tmp_path, invalid_document):
    settings = SettingsStore(base_dir=tmp_path)
    grant_account(settings, tmp_path)
    store = QueueStore(str(tmp_path / "queue.db"))
    try:
        job_id = _enqueue(store, "C:/fixture/accepted.wav", str(tmp_path / "output"))
        before = store.get_job(job_id)
        settings.path.write_text(invalid_document, encoding="utf-8")
        instance = Worker(store, settings_store=settings)
        with mock.patch.object(store, "claim_next", wraps=store.claim_next) as claim:
            assert instance.step() is False and instance.account_waiting is True
            claim.assert_not_called()
        assert store.get_job(job_id) == before
        assert settings.path.read_text(encoding="utf-8") == invalid_document
    finally:
        store.close()


def test_logout_keeps_running_work_and_pauses_next_queued_claim(tmp_path, monkeypatch):
    settings = SettingsStore(base_dir=tmp_path)
    grant_account(settings, tmp_path)
    store = QueueStore(str(tmp_path / "queue.db"))
    started, release = threading.Event(), threading.Event()
    thread = None
    try:
        first = _enqueue(store, "C:/fixture/running.wav", str(tmp_path / "output"))
        second = _enqueue(store, "C:/fixture/waiting.wav", str(tmp_path / "output"))
        instance = Worker(store, settings_store=settings, heartbeat_interval=0.05)

        def held_run(source, output, *_args, job_id=None, **_kwargs):
            # job_id is the seventh positional argument in the real engine API.
            running_id = _args[4]
            started.set()
            assert release.wait(3)
            return _manifest(running_id, source, output)

        monkeypatch.setattr(run_engine, "run", held_run)
        thread = threading.Thread(target=instance.step)
        thread.start()
        assert started.wait(2)
        running_id = store.list_jobs(status="running")[0]["id"]
        waiting_id = second if running_id == first else first
        before = store.get_job(waiting_id)
        settings.update(account_login_token="", account_user_id="", license_token="")
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert store.get_job(running_id)["status"] == "done"
        assert store.get_manifest(running_id) is not None
        assert instance.step() is False
        assert store.get_job(waiting_id) == before and instance.account_waiting is True
    finally:
        release.set()
        if thread:
            thread.join(5)
        store.close()


def test_concurrent_worker_checks_identity_before_claiming_any_job(tmp_path):
    settings = SettingsStore(base_dir=tmp_path)
    store = QueueStore(str(tmp_path / "queue.db"))
    checked = threading.Event()
    instance = Worker(store, settings_store=settings, concurrency=3, poll=0.01)
    original_load = settings.load

    def observed_load():
        result = original_load()
        checked.set()
        return result

    ids = [_enqueue(store, f"C:/fixture/{i}.wav", str(tmp_path / "output")) for i in range(3)]
    before = [store.get_job(job_id) for job_id in ids]
    thread = threading.Thread(target=instance._run_forever_concurrent)
    try:
        with mock.patch.object(settings, "load", side_effect=observed_load), mock.patch.object(store, "claim_next", wraps=store.claim_next) as claim:
            thread.start()
            assert checked.wait(2)
            instance._request_stop()
            thread.join(5)
            assert not thread.is_alive()
            claim.assert_not_called()
        assert instance.account_waiting is True
        assert [store.get_job(job_id) for job_id in ids] == before
    finally:
        instance._request_stop()
        thread.join(5)
        store.close()
