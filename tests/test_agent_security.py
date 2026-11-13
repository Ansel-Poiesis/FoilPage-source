# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Independent loopback Agent boundary tests; every asset is test-owned.

No desktop, worker engine, provider, email or payment service is started.  The
authentication oracle below deliberately does not import the product helper.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.controller import AppController
from app.local_api import LocalApiServer
from engine.settings import SettingsStore
from account_auth_fixtures import grant_account


JOB_ID = "123456789abc"
TASK_ID = "job:" + JOB_ID
LOCAL_TOKEN = "agent-security-public-fixture-token"
PRIVATE_SENTINELS = ("private-model-key-fixture", "private-login-session-fixture", "private-error-detail-fixture")


class NoDownloads:
    def __init__(self, controller):
        self.store = controller.downloads
        self.retries = 0

    def start(self):
        pass

    def stop(self, timeout=3):
        pass

    def get(self, task_id):
        return self.store.get(task_id)

    def cancel(self, task_id):
        return self.store.request_cancel(task_id)

    def retry(self, task_id):
        self.retries += 1
        return self.store.retry(task_id)


def agent_bearer(token=LOCAL_TOKEN):
    return hmac.new(token.encode("utf-8"), b"shengjian-agent-control-v1", hashlib.sha256).hexdigest()


def add_job(controller, *, job_id=JOB_ID, status="done", attempts=1, max_attempts=3,
            text="原始测试正文", title="测试标题", outside=None):
    root = Path(controller.settings["output_dir"])
    root.mkdir(parents=True, exist_ok=True)
    source = controller.settings_store.base_dir / (job_id + ".mp4")
    source.write_bytes(b"fixture, not real media")
    final = Path(outside) if outside is not None else root / (job_id + ".md")
    final.write_text(text, encoding="utf-8")
    raw = root / (job_id + "_raw.txt")
    raw.write_text("原始转写测试", encoding="utf-8")
    now = datetime.now().isoformat(timespec="seconds")
    controller.queue.enqueue({"id": job_id, "source_path": str(source), "output_dir": str(root),
        "status": "queued", "whisper_model": "tiny", "device": "cpu", "language": "zh",
        "content_mode": "raw", "source_title": title, "created_at": now, "updated_at": now,
        "raw_path": str(raw), "final_path": str(final), "attempts": attempts if status == "queued" else attempts - 1, "max_attempts": max_attempts,
        "error": {"code": "fixture_error", "message": PRIVATE_SENTINELS[2],
                  "details": {"cookie": "cookie-private-fixture"}}})
    if status != "queued":
        claimed = controller.queue.claim_next("fixture-no-engine")
        assert claimed["id"] == job_id
        if status == "done":
            controller.queue.save_manifest({"job_id": job_id, "source_path": str(source),
                "source_sha256": "synthetic-fixture", "backend": "local", "model": "tiny", "language": "zh",
                "duration_sec": 1.0, "raw_path": str(raw), "final_path": str(final), "content_mode": "raw",
                "prompt_version": "v1", "created_at": now}, worker_id="fixture-no-engine")
        elif status != "running":
            controller.queue.update_status(job_id, status, error={"code": "fixture_error", "message": PRIVATE_SENTINELS[2],
                "details": {"cookie": "cookie-private-fixture"}}, worker_id="fixture-no-engine")
    return final


@pytest.fixture
def agent_app(tmp_path, monkeypatch):
    # Even an accidental provider call must fail before leaving this machine.
    original_connect = socket.socket.connect
    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError("external network is prohibited in Agent acceptance tests")
        return original_connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", local_only)
    store = SettingsStore(base_dir=tmp_path / "data")
    store.update(local_api_token=LOCAL_TOKEN, agent_api_key=PRIVATE_SENTINELS[0],
                 account_login_token=PRIVATE_SENTINELS[1], account_user_id="fixture-not-authorized")
    controller = AppController(store)
    coordinator = NoDownloads(controller)
    server = LocalApiServer(controller, coordinator, port=0)
    server.start()
    # Only test-owned settings are changed; no ordinary SettingsStore.load is
    # ever pointed at the user's default profile.
    controller.save_settings({"local_api_port": server.address[1]})
    app = SimpleNamespace(controller=controller, server=server, store=store, coordinator=coordinator, tmp_path=tmp_path)
    try:
        yield app
    finally:
        server.stop()
        controller.media.close()
        controller.queue.close()
        controller.downloads.close()


def request(app, *, method="info", params=None, bearer=None, headers=None, raw=None):
    conn = http.client.HTTPConnection(*app.server.address, timeout=5)
    data = json.dumps({"method": method, "params": params or {}}, ensure_ascii=False).encode("utf-8") if raw is None else raw
    supplied = {"Content-Type": "application/json", "Authorization": "Bearer " + (agent_bearer() if bearer is None else bearer)}
    supplied.update(headers or {})
    try:
        conn.request("POST", "/v1/agent/call", body=data, headers=supplied)
        response = conn.getresponse()
        body = response.read()
        return response.status, json.loads(body), dict(response.getheaders())
    finally:
        conn.close()


def result(app, method, params=None):
    status, payload, _ = request(app, method=method, params=params)
    assert status == 200, payload
    assert payload["ok"] is True
    return payload["result"]


def test_derived_agent_auth_is_distinct_from_extension_and_has_no_browser_cors(agent_app):
    assert len(agent_bearer()) == 64
    assert agent_bearer() != LOCAL_TOKEN
    assert request(agent_app, bearer=LOCAL_TOKEN)[0] == 401
    assert request(agent_app, bearer="wrong")[0] == 401
    status, payload, headers = request(agent_app)
    assert status == 200 and payload["ok"] is True
    assert "Access-Control-Allow-Origin" not in headers
    text = json.dumps(payload)
    assert LOCAL_TOKEN not in text and agent_bearer() not in text


@pytest.mark.parametrize("host", ["localhost", "localhost:18765", "127.0.0.1", "evil.invalid", "127.0.0.1.evil.invalid:18765", "user@127.0.0.1:18765", "[::1]:18765"])
def test_agent_rejects_untrusted_or_nonexact_host(agent_app, host):
    status, payload, _ = request(agent_app, headers={"Host": host})
    assert status == 403 and payload["error"]["code"] == "host_denied"


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.invalid"},
    {"Origin": "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}, {"Origin": "null"},
    {"Origin": ""}, {"Sec-Fetch-Site": "none"}, {"Sec-Fetch-Mode": "cors"},
    {"Sec-Fetch-Dest": "empty"}, {"Sec-Fetch-User": "?1"}, {"sEc-FeTcH-Custom": ""}])
def test_agent_rejects_any_browser_origin_or_fetch_marker(agent_app, headers):
    status, payload, _ = request(agent_app, headers=headers)
    assert status == 403 and payload["error"]["code"] in {"origin_denied", "browser_denied"}


@pytest.mark.parametrize("raw", [b'{"method":"info","method":"tasks.retry","params":{}}',
    b'{"method":"info","params":{"limit":1,"limit":2}}', b'{"method":"info","params":{},"path":"C:/private"}',
    b'{"method":"info","params":[],"unknown":true}', b'[]', b'{"method":"info","params":{"__proto__":{}}}',
    b'{"method":"info","params":{},"method":null}', b'{"method":"info","params":{}} trailing'])
def test_agent_strict_json_rejects_duplicates_unknown_fields_and_ambiguous_shapes(agent_app, raw):
    status, _, _ = request(agent_app, raw=raw)
    assert status == 400


@pytest.mark.parametrize("headers,status", [({"Content-Length": str(3 * 1024 * 1024 + 1)}, 413),
    ({"Content-Length": "-1"}, 400), ({"Content-Length": "1.0"}, 400),
    ({"Transfer-Encoding": "chunked"}, 400), ({"Content-Type": "text/plain"}, 400)])
def test_agent_rejects_bad_framing_before_reading_or_dispatching_body(agent_app, headers, status):
    response, payload, _ = request(agent_app, headers=headers, raw=b"")
    assert response == status
    assert payload["error"]["code"] == ("request_too_large" if status == 413 else "invalid_request")


def test_agent_errors_do_not_echo_request_content_or_internal_exception_details(agent_app, monkeypatch):
    def fail_internal(*_):
        raise RuntimeError(PRIVATE_SENTINELS[0] + str(agent_app.tmp_path))
    monkeypatch.setattr(agent_app.server.agent_control, "call", fail_internal)
    status, payload, _ = request(agent_app)
    assert status == 500 and payload["error"]["code"] == "internal_error"
    rendered = json.dumps(payload)
    assert PRIVATE_SENTINELS[0] not in rendered and str(agent_app.tmp_path) not in rendered


@pytest.mark.parametrize("method", ["save_settings", "payments.create", "account.logout", "files.read", "shell.execute", "documents.delete"])
def test_agent_has_no_payment_secret_file_or_command_dispatch(agent_app, method):
    status, payload, _ = request(agent_app, method=method)
    assert status == 404 and payload["error"]["code"] == "method_not_found"


def test_agent_list_and_task_views_are_allowlisted_and_never_export_paths_or_private_details(agent_app):
    add_job(agent_app.controller)
    agent_app.controller.downloads.create("https://fixture.invalid/private-signed-url?token=secret-fixture",
        "下载测试标题", "douyin", {"transcribe": False})
    for method, params in [("tasks.list", {}), ("tasks.get", {"task_id": TASK_ID}), ("documents.list", {})]:
        payload = result(agent_app, method, params)
        text = json.dumps(payload)
        for secret in (LOCAL_TOKEN, agent_bearer(), *PRIVATE_SENTINELS, "cookie-private-fixture", str(agent_app.tmp_path), "private-signed-url", "secret-fixture"):
            assert secret not in text
        for forbidden in ("source_path", "final_path", "raw_path", "options", "settings", "login_token", "license_token", "error", "downloaded_path"):
            assert '"' + forbidden + '"' not in text
    assert result(agent_app, "tasks.get", {"task_id": TASK_ID})["task_id"] == TASK_ID


@pytest.mark.parametrize("params", [{"limit": True}, {"limit": 0}, {"limit": 201}, {"limit": 1.5},
    {"status": "unknown"}, {"query": []}, {"path": "C:/private"}])
def test_task_filters_reject_invalid_types_ranges_and_unknown_inputs(agent_app, params):
    assert request(agent_app, method="tasks.list", params=params)[0] == 400


@pytest.mark.parametrize("task_id", [JOB_ID, "job:../private", "job:" + "a" * 13, "job:" + "A" * 12, "file:///private", "job:123456789abc/../../private"])
def test_documents_accept_registered_task_ids_only(agent_app, task_id):
    add_job(agent_app.controller)
    assert request(agent_app, method="documents.read", params={"task_id": task_id})[0] == 400


def test_registered_document_outside_output_root_is_not_read_or_written(agent_app):
    outside = agent_app.tmp_path / "private.md"
    add_job(agent_app.controller, outside=outside)
    status, payload, _ = request(agent_app, method="documents.read", params={"task_id": TASK_ID})
    assert status == 404 and payload["error"]["code"] == "document_unavailable"
    assert request(agent_app, method="documents.update", params={"task_id": TASK_ID, "tab": "final", "text": "replace", "expected_revision": "a" * 64})[0] != 200
    assert outside.read_text(encoding="utf-8") == "原始测试正文"


def test_only_completed_text_assets_can_be_edited(agent_app):
    target = add_job(agent_app.controller, status="queued")
    status, _, _ = request(agent_app, method="documents.update", params={"task_id": TASK_ID, "tab": "final", "text": "replace", "expected_revision": "a" * 64})
    assert status == 409 and target.read_text(encoding="utf-8") == "原始测试正文"


def test_body_cas_conflict_and_desktop_edit_do_not_overwrite_existing_content(agent_app):
    target = add_job(agent_app.controller)
    before = result(agent_app, "documents.read", {"task_id": TASK_ID})
    assert len(before["revision"]) == 64 and before["text"] == "原始测试正文"
    agent_app.controller.update_document_text(JOB_ID, "final", "桌面先保存")
    status, payload, _ = request(agent_app, method="documents.update", params={"task_id": TASK_ID, "tab": "final", "text": "旧读稿", "expected_revision": before["revision"]})
    assert status == 409 and payload["error"]["code"] == "revision_conflict"
    assert target.read_text(encoding="utf-8") == "桌面先保存"


def test_two_body_writers_using_one_revision_have_exactly_one_winner(agent_app):
    target = add_job(agent_app.controller)
    revision = result(agent_app, "documents.read", {"task_id": TASK_ID})["revision"]
    def write(text):
        return request(agent_app, method="documents.update", params={"task_id": TASK_ID, "tab": "final", "text": text, "expected_revision": revision})[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(write, ["并发 A", "并发 B"]))
    assert sorted(statuses) == [200, 409]
    assert target.read_text(encoding="utf-8") in {"并发 A", "并发 B"}
    assert list(target.parent.glob("*.tmp")) == []


def test_revision_is_bound_to_task_and_tab_and_text_limit_is_byte_based(agent_app):
    target = add_job(agent_app.controller)
    other = "abcdef123456"
    add_job(agent_app.controller, job_id=other, text="原始测试正文")
    revision = result(agent_app, "documents.read", {"task_id": TASK_ID})["revision"]
    for task_id, tab in [("job:" + other, "final"), (TASK_ID, "raw")]:
        status, _, _ = request(agent_app, method="documents.update", params={"task_id": task_id, "tab": tab, "text": "错位", "expected_revision": revision})
        assert status == 409
    status, payload, _ = request(agent_app, method="documents.update", params={"task_id": TASK_ID, "tab": "final", "text": "中" * 666_667, "expected_revision": revision})
    assert status == 413 and payload["error"]["code"] == "text_too_large"
    assert target.read_text(encoding="utf-8") == "原始测试正文"


def test_metadata_cas_changes_title_tags_category_atomically_and_rejects_stale_editor(agent_app):
    add_job(agent_app.controller)
    metadata = result(agent_app, "documents.metadata", {"task_id": TASK_ID})
    changed = result(agent_app, "documents.update_metadata", {"task_id": TASK_ID, "expected_revision": metadata["revision"],
        "title": "新标题", "tags": ["甲", "乙"], "category": "测试分类"})
    assert (changed["title"], changed["tags"], changed["category"]) == ("新标题", ["甲", "乙"], "测试分类")
    status, payload, _ = request(agent_app, method="documents.update_metadata", params={"task_id": TASK_ID,
        "expected_revision": metadata["revision"], "title": "旧编辑覆盖", "tags": ["丢失"], "category": "旧分类"})
    assert status == 409 and payload["error"]["code"] == "revision_conflict"
    assert result(agent_app, "documents.metadata", {"task_id": TASK_ID}) == changed


@pytest.mark.parametrize("patch", [{"title": "更新不应部分生效", "tags": [1]},
    {"title": "更新不应部分生效", "tags": ["太长" * 20]},
    {"title": "更新不应部分生效", "category": None},
    {"title": "更新不应部分生效", "path": "C:/private"}])
def test_invalid_metadata_patch_cannot_partially_change_existing_fields(agent_app, patch):
    add_job(agent_app.controller)
    before = result(agent_app, "documents.metadata", {"task_id": TASK_ID})
    status, _, _ = request(agent_app, method="documents.update_metadata", params={
        "task_id": TASK_ID, "expected_revision": before["revision"], **patch})
    assert status == 400
    assert result(agent_app, "documents.metadata", {"task_id": TASK_ID}) == before


def test_two_metadata_writers_cannot_produce_a_mixed_patch(agent_app):
    add_job(agent_app.controller)
    revision = result(agent_app, "documents.metadata", {"task_id": TASK_ID})["revision"]
    def write(name):
        return request(agent_app, method="documents.update_metadata", params={"task_id": TASK_ID,
            "expected_revision": revision, "title": name, "tags": [name], "category": name})[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(write, ["A", "B"]))
    assert sorted(statuses) == [200, 409]
    metadata = result(agent_app, "documents.metadata", {"task_id": TASK_ID})
    assert metadata["title"] in {"A", "B"}
    assert metadata["tags"] == [metadata["title"]] and metadata["category"] == metadata["title"]


@pytest.mark.parametrize("grant", ["none", "expired", "wrong_device"])
def test_retry_preserves_account_gate_queue_attempts_and_local_balances(agent_app, grant):
    add_job(agent_app.controller, status="failed", attempts=2)
    if grant != "none":
        agent_app.controller.settings = grant_account(agent_app.store, agent_app.store.base_dir,
            **({"expires_at": time.time() - 1} if grant == "expired" else {"device": "wrong-test-device"}))
    before = agent_app.controller.queue.get_job(JOB_ID)
    balance = agent_app.controller.settings.get("credit_balance")
    status, payload, _ = request(agent_app, method="tasks.retry", params={"task_id": TASK_ID})
    assert status == 403 and payload["error"]["code"] == "login_required"
    assert agent_app.controller.queue.get_job(JOB_ID) == before
    assert agent_app.controller.settings.get("credit_balance") == balance


def test_retry_with_a_verified_free_account_only_requeues_and_does_not_execute_or_reset_attempts(agent_app):
    add_job(agent_app.controller, status="failed", attempts=2)
    agent_app.controller.settings = grant_account(agent_app.store, agent_app.store.base_dir)
    result(agent_app, "tasks.retry", {"task_id": TASK_ID})
    job = agent_app.controller.queue.get_job(JOB_ID)
    assert job["status"] == "queued" and job["attempts"] == 2 and job["max_attempts"] == 3
    assert agent_app.controller.settings.get("credit_balance", 0) == 0
    assert len(agent_app.controller.queue.list_jobs()) == 1


def test_retry_cannot_raise_attempt_limit_or_replace_an_exhausted_job(agent_app):
    add_job(agent_app.controller, status="failed", attempts=3)
    agent_app.controller.settings = grant_account(agent_app.store, agent_app.store.base_dir)
    before = agent_app.controller.queue.get_job(JOB_ID)
    assert request(agent_app, method="tasks.retry", params={"task_id": TASK_ID})[0] == 409
    assert agent_app.controller.queue.get_job(JOB_ID) == before
    assert len(agent_app.controller.queue.list_jobs()) == 1


def test_cancellation_remains_available_without_a_login_and_never_deletes_text(agent_app):
    target = add_job(agent_app.controller, status="queued")
    result(agent_app, "tasks.cancel", {"task_id": TASK_ID})
    assert agent_app.controller.queue.get_job(JOB_ID)["status"] == "cancelled"
    assert target.read_text(encoding="utf-8") == "原始测试正文"


def test_download_task_cancel_is_available_but_retry_cannot_bypass_account_gate(agent_app):
    downloaded = agent_app.controller.downloads.create("https://fixture.invalid/signed-private",
        "下载测试", "douyin", {"transcribe": False})
    download_id = downloaded["id"]
    task_id = "dl:" + download_id
    assert result(agent_app, "tasks.get", {"task_id": task_id})["kind"] == "download"
    assert result(agent_app, "tasks.cancel", {"task_id": task_id})["status"] == "cancelled"
    second = agent_app.controller.downloads.create("https://fixture.invalid/never-requested", "重试下载", "douyin", {"transcribe": False})
    agent_app.controller.downloads.transition(second["id"], "resolving")
    agent_app.controller.downloads.transition(second["id"], "failed", error={"code": "fixture", "retryable": True})
    before = agent_app.controller.downloads.get(second["id"])
    status, payload, _ = request(agent_app, method="tasks.retry", params={"task_id": "dl:" + second["id"]})
    assert status == 403 and payload["error"]["code"] == "login_required"
    assert agent_app.controller.downloads.get(second["id"]) == before
    assert agent_app.coordinator.retries == 0
    agent_app.controller.settings = grant_account(agent_app.store, agent_app.store.base_dir)
    assert result(agent_app, "tasks.retry", {"task_id": "dl:" + second["id"]})["status"] == "received"
    assert agent_app.coordinator.retries == 1
    assert agent_app.controller.downloads.get(second["id"])["attempts"] == before["attempts"]
