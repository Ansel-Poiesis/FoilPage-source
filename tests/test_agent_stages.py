# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Independent body/title execution, immutable ASR and persisted notification facts."""
import json
from pathlib import Path
from unittest import mock

import pytest

from app.worker import Worker
from engine.agent_client import AgentError, AgentResult
from engine.queue_store import QueueStore
from engine.settings import SettingsStore, SettingsValidationError
from account_auth_fixtures import grant_account
from test_agent_service import FakeResponse


def _enqueue(store, tmp_path):
    store.enqueue({"id": "stages", "source_path": str(tmp_path / "source.mp4"),
        "output_dir": str(tmp_path), "status": "queued", "source_title": "原视频标题",
        "whisper_model": "tiny", "device": "cpu", "language": "zh", "content_mode": "raw",
        "created_at": "2026-10-04T00:00:00", "updated_at": "2026-10-04T00:00:00"})


def _manifest(tmp_path):
    raw = tmp_path / "sample_raw.md"
    raw.write_text("# 原视频标题\n\n完整转写、例子和限定条件。", encoding="utf-8")
    return {"job_id": "stages", "source_path": str(tmp_path / "source.mp4"), "source_title": "原视频标题",
        "source_sha256": None, "backend": "local", "model": "tiny", "language": "zh", "duration_sec": 1.,
        "raw_path": str(raw), "final_path": str(raw), "content_mode": "raw", "prompt_version": "v1",
        "created_at": "2026-10-04T00:00:00"}


@pytest.mark.parametrize("body,title", [(False, False), (False, True), (True, False), (True, True)])
def test_four_independent_switches_snapshot_and_byok_never_debits(tmp_path, body, title):
    database = str(tmp_path / "q.db")
    store = QueueStore(database)
    _enqueue(store, tmp_path)
    manifest = _manifest(tmp_path)
    before = Path(manifest["raw_path"]).read_bytes()
    requests = []
    worker = Worker(store, worker_id="test", agent_settings=grant_account({"agent_enabled": body,
        "title_optimization_enabled": title, "agent_billing_mode": "byok", "agent_api_key": "fixture",
        "agent_model": "fixture", "agent_profile": "knowledge", "credit_balance": 99}, tmp_path))

    def asr(*args, **kwargs):
        # A setting changed while ASR runs must not rewrite the execution snapshot.
        worker.agent_settings.update(agent_enabled=not body, title_optimization_enabled=not title)
        return dict(manifest)

    def chat(request, **kwargs):
        payload = json.loads(request.data)
        requests.append(payload)
        result = '{"title":"准确的新标题"}' if "response_format" in payload else "# 隐式模型标题\n\n完整整理正文。"
        return FakeResponse({"choices": [{"message": {"content": result}}]})

    with mock.patch("run_engine.run", side_effect=asr), mock.patch("engine.agent_client.urlopen", side_effect=chat):
        assert worker.step()
    job = store.get_job("stages")
    saved = store.get_manifest("stages")
    assert job["status"] == "done"
    assert job["body_enabled_snapshot"] is body and job["title_enabled_snapshot"] is title
    assert job["body_status"] == ("done" if body else "disabled")
    assert job["title_status"] == ("done" if title else "disabled")
    assert job["source_title"] == ("准确的新标题" if title else "原视频标题")
    assert job["original_source_title"] == "原视频标题"
    assert job["optimized_source_title"] == ("准确的新标题" if title else "")
    assert job["body_usage_units"] == job["title_usage_units"] == 0
    assert job["body_request_count"] == int(body) and job["title_request_count"] == int(title)
    assert len(requests) == int(body) + int(title)
    assert worker.agent_settings["credit_balance"] == 99
    assert Path(manifest["raw_path"]).read_bytes() == before
    final = Path(saved["final_path"]).read_text(encoding="utf-8")
    assert final.startswith("# " + job["source_title"])
    assert "隐式模型标题" not in final
    store.close()
    reopened = QueueStore(database)
    try:
        assert reopened.get_job("stages")["title_enabled_snapshot"] is title
        assert reopened.get_manifest("stages")["body_status"] == job["body_status"]
    finally:
        reopened.close()


def test_body_failure_does_not_prevent_title_or_discard_asr(tmp_path):
    store = QueueStore(str(tmp_path / "q.db"))
    _enqueue(store, tmp_path)
    manifest = _manifest(tmp_path)
    with mock.patch("run_engine.run", return_value=manifest), mock.patch("app.agent_service.rewrite_transcript",
            side_effect=AgentError("provider-secret")), mock.patch("engine.title_optimizer.optimize_title", return_value="精确标题"):
        Worker(store, worker_id="test", agent_settings=grant_account(
            {"agent_enabled": True, "title_optimization_enabled": True}, tmp_path)).step()
    job = store.get_job("stages")
    assert job["status"] == "done" and job["body_status"] == "skipped" and job["title_status"] == "done"
    assert "provider-secret" not in str(store.get_manifest("stages"))
    assert Path(manifest["raw_path"]).is_file()
    store.close()


def test_cancel_before_asr_persists_enabled_snapshot_and_terminal_stages(tmp_path):
    store = QueueStore(str(tmp_path / "q.db"))
    _enqueue(store, tmp_path)
    job = store.claim_next("test")
    store.request_cancel("stages")
    with mock.patch("run_engine.run") as asr:
        Worker(store, worker_id="test", agent_settings={"agent_enabled": True, "title_optimization_enabled": True})._run_job(job)
    asr.assert_not_called()
    row = store.get_job("stages")
    assert row["status"] == row["body_status"] == row["title_status"] == "cancelled"
    assert row["body_enabled_snapshot"] and row["title_enabled_snapshot"]
    assert row["body_usage_units"] == row["title_usage_units"] == 0
    store.close()


def test_stage_metadata_is_whitelisted_and_rejects_untrusted_nested_values(tmp_path):
    store = QueueStore(str(tmp_path / "q.db"))
    _enqueue(store, tmp_path)
    store.claim_next("test")
    store.set_stage_metadata("stages", {"body_enabled_snapshot": True, "body_status": "pending",
        "account_login_token": "private", "provider_response": {"text": "private"}}, worker_id="test")
    value = store._conn.execute("SELECT stage_metadata_json FROM jobs").fetchone()[0]
    assert "private" not in value
    from engine.validate import ContractValidationError
    with pytest.raises(ContractValidationError):
        store.set_stage_metadata("stages", {"title_usage_units": True}, worker_id="test")
    store.close()


def test_billing_mode_defaults_migrates_and_validates(tmp_path):
    store = SettingsStore(base_dir=tmp_path)
    assert store.defaults()["agent_billing_mode"] == "byok"
    assert store.update(agent_billing_mode="hosted")["agent_billing_mode"] == "hosted"
    with pytest.raises(SettingsValidationError):
        store.update(agent_billing_mode="local-fake-credit")
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw.pop("agent_billing_mode")
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    assert store.load()["agent_billing_mode"] == "byok"


def test_body_quota_exceeded_marks_reason_and_keeps_task_done(tmp_path):
    """积分用完：正文整理跳过但标记 quota_exceeded，任务仍完成（ASR 不丢）。

    用户侧经 terminal_label 得到"积分已用完，请充值"弹窗与页面提示。
    """
    quota_error = AgentError("积分不足，请充值后继续")
    quota_error.code = "quota_exceeded"
    store = QueueStore(str(tmp_path / "q.db"))
    _enqueue(store, tmp_path)
    manifest = _manifest(tmp_path)
    with mock.patch("run_engine.run", return_value=manifest), mock.patch(
            "app.agent_service.rewrite_transcript", side_effect=quota_error):
        Worker(store, worker_id="test", agent_settings=grant_account(
            {"agent_enabled": True, "title_optimization_enabled": False}, tmp_path)).step()
    job = store.get_job("stages")
    assert job["status"] == "done" and job["body_status"] == "skipped"
    assert job.get("body_quota_exceeded") is True
    assert Path(manifest["raw_path"]).is_file()
    store.close()


def test_successful_stage_retries_clear_previous_quota_failure(tmp_path):
    from app.agent_service import apply_agent_translation, apply_title_optimization

    manifest = {**_manifest(tmp_path), "body_quota_exceeded": True, "title_quota_exceeded": True}
    original = Path(manifest["raw_path"]).read_bytes()
    settings = {"agent_billing_mode": "byok", "title_optimization_enabled": True}
    result = AgentResult(profile="knowledge", markdown="# 文章\n\n整理后的正文。", model="fixture")
    with mock.patch("app.agent_service.rewrite_transcript", return_value=result):
        body = apply_agent_translation(manifest, settings)
    assert body["body_status"] == "done" and body["body_quota_exceeded"] is False
    with mock.patch("engine.title_optimizer.optimize_title", return_value="准确标题"):
        titled = apply_title_optimization(body, settings)
    assert titled["title_status"] == "done" and titled["title_quota_exceeded"] is False
    assert Path(manifest["raw_path"]).read_bytes() == original
    saved = json.loads((tmp_path / "sample_manifest.json").read_text(encoding="utf-8"))
    assert saved["body_quota_exceeded"] is False and saved["title_quota_exceeded"] is False


@pytest.mark.parametrize("title_enabled", [False, True])
def test_batch_quota_failure_is_persisted_and_successful_retry_clears_it(tmp_path, title_enabled):
    from app.agent_service import batch_apply_agent_translation
    from app.task_notifications import TaskNotifications

    store = QueueStore(str(tmp_path / "q.db"))
    _enqueue(store, tmp_path)
    manifest = _manifest(tmp_path)
    original = Path(manifest["raw_path"]).read_bytes()
    with mock.patch("run_engine.run", return_value=manifest):
        Worker(store, worker_id="test", agent_settings=grant_account(
            {"agent_enabled": False, "title_optimization_enabled": False}, tmp_path)).step()
    settings = {"agent_billing_mode": "byok", "title_optimization_enabled": title_enabled}
    error = AgentError("积分不足，请充值后继续")
    error.code = "quota_exceeded"
    with mock.patch("app.agent_service.rewrite_transcript", side_effect=error), mock.patch(
            "engine.title_optimizer.optimize_title", return_value="准确标题"):
        failed = batch_apply_agent_translation(["stages"], store, settings)
    assert failed["failed"] == 1
    assert store.get_job("stages")["body_quota_exceeded"] is True
    assert store.get_manifest("stages")["body_quota_exceeded"] is True
    assert Path(manifest["raw_path"]).read_bytes() == original
    result = AgentResult(profile="knowledge", markdown="# 文章\n\n整理后的正文。", model="fixture")
    with mock.patch("app.agent_service.rewrite_transcript", return_value=result), mock.patch(
            "engine.title_optimizer.optimize_title", return_value="准确标题"):
        completed = batch_apply_agent_translation(["stages"], store, settings)
    assert completed["done"] == 1 and completed["failed"] == 0
    row = store.get_job("stages")
    assert row["body_quota_exceeded"] is False and row["title_quota_exceeded"] is False
    assert "积分" not in TaskNotifications.terminal_label(row)[0]
    assert Path(manifest["raw_path"]).read_bytes() == original
    store.close()


def test_hosted_begin_sends_selected_model_and_omits_default():
    """1.3.12 托管模型选择：显式选择随 begin 上送；空=不传（服务端默认）。"""
    from engine.agent_client import AgentStage

    captured = {}

    class FakeStage(AgentStage):
        def _post(self, action, payload):
            captured[action] = payload
            if action == "begin":
                return {"ok": True, "request_id": "r1", "receipt": "k1", "status": "pending",
                        "units": 1, "charge": "credits", "model": "deepseek-flash"}
            return {"ok": True}

    settings = {"agent_billing_mode": "hosted", "agent_hosted_model": "deepseek-flash",
                "subscription_server_url": "https://pay.example.com", "_agent_device_id": "device-x",
                "agent_model": "qwen-plus", "account_login_token": "token"}
    with FakeStage(settings, "body", "整理文本", "op-model-1") as stage:
        assert captured["begin"]["model"] == "deepseek-flash"
        assert stage.model == "deepseek-flash"
        assert stage.settings["agent_model"] == "deepseek-flash"

    captured.clear()
    with FakeStage({**settings, "agent_hosted_model": ""}, "body", "整理文本", "op-model-2"):
        assert "model" not in captured["begin"]


@pytest.mark.parametrize("status,code", [(401, "agent_unauthorized"), (403, "agent_forbidden"),
    (404, "agent_model_unavailable"), (429, "agent_rate_limited"), (503, "agent_unavailable")])
def test_byok_http_failures_have_safe_actionable_codes(status, code):
    from urllib.error import HTTPError
    from engine.agent_client import _chat
    error = HTTPError("https://fixture.invalid/secret", status, "provider-secret", {}, None)
    with mock.patch("engine.agent_client.urlopen", side_effect=error), pytest.raises(AgentError) as caught:
        _chat({"agent_base_url": "https://fixture.invalid", "agent_model": "fixture"},
              "fixture-key", [{"role": "user", "content": "原稿"}], cancel_check=None)
    assert caught.value.code == code
    assert "secret" not in str(caught.value)


def test_worker_persists_safe_auth_failure_and_keeps_transcript(tmp_path):
    store = QueueStore(str(tmp_path / "q.db"))
    _enqueue(store, tmp_path)
    manifest = _manifest(tmp_path)
    error = AgentError("provider-secret")
    error.code = "agent_unauthorized"
    with mock.patch("run_engine.run", return_value=manifest), mock.patch(
            "app.agent_service.rewrite_transcript", side_effect=error):
        Worker(store, worker_id="test", agent_settings=grant_account(
            {"agent_enabled": True, "title_optimization_enabled": False}, tmp_path)).step()
    saved = store.get_manifest("stages")
    assert saved["body_error_code"] == "agent_unauthorized"
    assert "API Key" in saved["body_error"]
    assert "provider-secret" not in str(saved)
    assert store.get_job("stages")["body_error_code"] == "agent_unauthorized"
    assert Path(manifest["final_path"]).is_file()
    store.close()
