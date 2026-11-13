# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Controller/Agent behavior on synthetic local documents; no paid work runs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import threading

import pytest

from app.agent_control import AgentControl, AgentControlError
from app.controller import AppController, DocumentRevisionConflict
from engine.agent_protocol import document_revision, derive_agent_token
from engine.settings import SettingsStore
from account_auth_fixtures import grant_account


class NoDownloads:
    def __init__(self, controller):
        self.store = controller.downloads

    def get(self, identifier):
        return self.store.get(identifier)

    def cancel(self, identifier):
        return self.store.request_cancel(identifier)

    def retry(self, identifier):
        return self.store.retry(identifier)


@pytest.fixture
def local(tmp_path):
    controller = AppController(SettingsStore(base_dir=tmp_path / "profile"))
    control = AgentControl(controller, NoDownloads(controller))
    try:
        yield controller, control
    finally:
        controller.close()


def completed(controller, identifier="000000000001", text="测试正文\n"):
    output = Path(controller.settings["output_dir"]) / identifier
    output.mkdir(parents=True)
    source = Path(controller.settings_store.base_dir) / (identifier + ".wav")
    source.write_bytes(b"synthetic-not-a-real-audio-file")
    final = output / "article.md"
    raw = output / "raw.md"
    final.write_text(text, encoding="utf-8", newline="")
    raw.write_text(text, encoding="utf-8", newline="")
    controller.queue.enqueue({"id": identifier, "source_path": str(source), "output_dir": str(output.parent),
                              "status": "queued", "source_title": "文稿标题", "whisper_model": "tiny",
                              "device": "cpu", "language": "zh", "content_mode": "raw",
                              "created_at": "2026-10-08T12:00:00", "updated_at": "2026-10-08T12:00:00"})
    controller.queue.claim_next("synthetic-worker")
    controller.queue.save_manifest({"job_id": identifier, "source_path": str(source),
                                    "source_sha256": "synthetic", "backend": "local", "model": "tiny",
                                    "language": "zh", "duration_sec": 1.0, "raw_path": str(raw),
                                    "final_path": str(final), "content_mode": "raw", "prompt_version": "v1",
                                    "created_at": "2026-10-08T12:00:00"}, worker_id="synthetic-worker")
    return "job:" + identifier, final, raw


def test_agent_token_has_fixed_scope_and_no_raw_secret():
    assert derive_agent_token("synthetic") == "7534cdff8ce19ee92b90ddbc43f6deaebe9fce0a6bb6b032075ec50438e002d6"
    with pytest.raises(ValueError):
        derive_agent_token("")


def test_documents_are_registered_data_with_scoped_revisions(local):
    controller, control = local
    task_id, final, raw = completed(controller)
    final_doc = control.call("documents.read", {"task_id": task_id})
    raw_doc = control.call("documents.read", {"task_id": task_id, "tab": "raw"})
    assert final_doc["revision"] == document_revision(task_id, "final", final.read_bytes())
    assert raw_doc["revision"] != final_doc["revision"]
    assert "path" not in final_doc
    assert final_doc["size"] == len(final.read_bytes())
    assert control.call("documents.list", {})["items"][0]["task_id"] == task_id
    assert control.call("documents.list", {"query": "不会匹配"}) == {"items": [], "total": 0}


def test_agent_and_desktop_share_the_same_cas_lock(local):
    controller, control = local
    task_id, final, _ = completed(controller)
    before = control.call("documents.read", {"task_id": task_id})
    controller.update_document_text(task_id[4:], "final", "桌面刚刚保存\n", expected_revision=before["revision"])
    with pytest.raises(AgentControlError) as failed:
        control.call("documents.update", {"task_id": task_id, "tab": "final", "text": "过期稿",
                                           "expected_revision": before["revision"]})
    assert (failed.value.status, failed.value.code) == (409, "revision_conflict")
    assert final.read_text(encoding="utf-8") == "桌面刚刚保存\n"


def test_two_agent_saves_of_one_revision_have_exactly_one_winner(local):
    controller, control = local
    task_id, final, _ = completed(controller)
    revision = control.call("documents.read", {"task_id": task_id})["revision"]
    def save(text):
        try:
            value = control.call("documents.update", {"task_id": task_id, "tab": "final", "text": text,
                                                       "expected_revision": revision})
            return "saved", value["revision"]
        except AgentControlError as exc:
            return "conflict", exc.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(save, ["版本甲\n", "版本乙\n"]))
    assert sorted(status for status, _ in values) == ["conflict", "saved"]
    current = controller.read_document_text(task_id[4:], "final")
    assert ("saved", current["revision"]) in values
    assert final.read_text(encoding="utf-8") in {"版本甲\n", "版本乙\n"}


def test_metadata_patch_is_atomic_and_not_a_body_version(local):
    controller, control = local
    task_id, final, _ = completed(controller)
    body_revision = control.call("documents.read", {"task_id": task_id})["revision"]
    before = control.call("documents.metadata", {"task_id": task_id})
    after = control.call("documents.update_metadata", {
        "task_id": task_id, "expected_revision": before["revision"], "title": "  新标题  ",
        "tags": ["记录", "记录", "分析"], "category": "笔记",
    })
    assert (after["title"], after["tags"], after["category"]) == ("新标题", ["记录", "分析"], "笔记")
    assert after["revision"] != before["revision"]
    assert control.call("documents.read", {"task_id": task_id})["revision"] == body_revision
    assert final.read_text(encoding="utf-8") == "测试正文\n"
    with pytest.raises(AgentControlError) as failed:
        control.call("documents.update_metadata", {"task_id": task_id, "expected_revision": after["revision"],
                                                    "title": "应全部回滚", "tags": ["过长" * 20]})
    assert failed.value.code == "invalid_request"
    assert control.call("documents.metadata", {"task_id": task_id}) == after


def test_metadata_revision_is_checked_inside_database_transaction(local):
    controller, control = local
    task_id, _, _ = completed(controller)
    before = control.call("documents.metadata", {"task_id": task_id})
    controller.set_job_tags(task_id[4:], ["桌面最新标签"])
    with pytest.raises(DocumentRevisionConflict):
        controller.update_document_metadata(task_id[4:], expected_revision=before["revision"], title="过期标题")
    current = control.call("documents.metadata", {"task_id": task_id})
    assert current["title"] == before["title"]
    assert current["tags"] == ["桌面最新标签"]


@pytest.mark.parametrize("method,params", [
    ("info", {"path": "C:/private"}), ("tasks.list", {"limit": True}),
    ("tasks.list", {"status": "impossible"}), ("tasks.get", {"task_id": "../private"}),
    ("documents.read", {"task_id": "dl:000000000001"}),
    ("documents.read", {"task_id": "job:000000000001", "tab": "source"}),
    ("documents.update_metadata", {"task_id": "job:000000000001", "expected_revision": "a" * 64}),
    ("documents.update", {"task_id": "job:000000000001", "tab": "final", "text": "\ud800", "expected_revision": "a" * 64}),
])
def test_invalid_requests_do_not_reach_content_services(local, method, params):
    _, control = local
    with pytest.raises(AgentControlError) as failed:
        control.call(method, params)
    assert failed.value.status == 400
    assert failed.value.code == "invalid_request"


def test_registered_paths_outside_output_folder_cannot_be_read_or_written(local):
    controller, control = local
    task_id, final, _ = completed(controller)
    private = Path(controller.settings_store.base_dir) / "private.md"
    private.write_text("合成私密正文", encoding="utf-8")
    controller.queue._conn.execute("UPDATE jobs SET final_path=? WHERE id=?", (str(private), task_id[4:]))
    controller.queue._conn.commit()
    with pytest.raises(AgentControlError) as failed:
        control.call("documents.read", {"task_id": task_id})
    assert (failed.value.status, failed.value.code) == (404, "document_unavailable")
    assert private.read_text(encoding="utf-8") == "合成私密正文"
    assert final.read_text(encoding="utf-8") == "测试正文\n"


def test_lists_and_info_do_not_expose_profile_or_file_details(local):
    controller, control = local
    task_id, _, _ = completed(controller)
    values = [control.call("info", {}), control.call("tasks.list", {}), control.call("tasks.get", {"task_id": task_id}),
              control.call("documents.list", {})]
    text = repr(values)
    for prohibited in (controller.settings["local_api_token"], "source_path", "final_path", "raw_path", "account_login_token", "options"):
        assert prohibited not in text


def test_untitled_download_does_not_disclose_url_as_its_title(local):
    controller, control = local
    item = controller.downloads.create("https://example.invalid/watch?token=private-synthetic-url", "", "youtube", {})
    task_id = "dl:" + item["id"]
    values = [control.call("tasks.list", {}), control.call("tasks.get", {"task_id": task_id}),
              control.call("tasks.cancel", {"task_id": task_id})]
    assert "private-synthetic-url" not in repr(values)
    assert values[1]["title"] == "未命名下载"
    assert values[2]["status"] == "cancelled"


@pytest.mark.parametrize("status,has_error", [("done", False), ("failed", True)])
def test_download_agent_get_and_list_agree_on_stale_retry_errors(local, status, has_error):
    controller, control = local
    item = controller.downloads.create("https://example.invalid/watch", "重试下载", "youtube", {})
    controller.downloads.transition(item["id"], "resolving")
    if status == "done":
        controller.downloads.transition(item["id"], "downloading")
        controller.downloads.transition(item["id"], "imported")
    controller.downloads.transition(item["id"], status, error={"code": "network_error", "message": "旧网络错误"})
    task_id = "dl:" + item["id"]
    detail = control.call("tasks.get", {"task_id": task_id})
    listing = control.call("tasks.list", {})
    row = next(row for row in listing["items"] if row["task_id"] == task_id)
    assert ("error_code" in detail) is has_error
    assert ("error_code" in row) is has_error
    assert controller.downloads.get(item["id"])["error"]["code"] == "network_error"


@pytest.mark.parametrize("delete_method", ["delete_files", "delete_tasks"])
def test_desktop_removal_is_respected_by_old_agent_document_drafts(local, delete_method):
    controller, control = local
    task_id, final, _ = completed(controller)
    before = control.call("documents.read", {"task_id": task_id})
    assert getattr(controller, delete_method)([task_id[4:]]) == [task_id[4:]]
    with pytest.raises(AgentControlError) as failed:
        control.call("documents.update", {"task_id": task_id, "tab": "final", "text": "旧稿不能复活文件",
                                           "expected_revision": before["revision"]})
    assert failed.value.status == 404
    assert not final.exists()
    assert not final.parent.exists()


def test_worker_publishes_tags_with_done_and_preserves_user_edits_during_settlement(local, monkeypatch):
    from app.worker import Worker
    controller, control = local
    job_id = "000000000009"
    task_id = "job:" + job_id
    output = Path(controller.settings["output_dir"]) / job_id
    output.mkdir()
    source = controller.settings_store.base_dir / "synthetic.wav"
    source.write_bytes(b"synthetic-not-a-real-audio-file")
    raw, final = output / "raw.md", output / "final.md"
    raw.write_text("合成转写", encoding="utf-8")
    final.write_text("合成整理稿", encoding="utf-8")
    controller.settings_store.update(agent_enabled=True, title_optimization_enabled=False,
                                     delete_source_after_translation=False)
    controller.settings = grant_account(controller.settings_store, controller.settings_store.base_dir)
    controller.queue.enqueue({"id": job_id, "source_path": str(source), "output_dir": str(output.parent),
                              "status": "queued", "source_title": "标题", "whisper_model": "tiny", "device": "cpu",
                              "language": "zh", "content_mode": "raw", "created_at": "2026-10-08T12:00:00",
                              "updated_at": "2026-10-08T12:00:00"})
    manifest = {"job_id": job_id, "source_path": str(source), "source_sha256": "synthetic", "backend": "local",
                "model": "tiny", "language": "zh", "duration_sec": 1.0, "raw_path": str(raw), "final_path": str(final),
                "content_mode": "raw", "prompt_version": "v1", "created_at": "2026-10-08T12:00:00"}
    # Prevent importing or running the real ASR stack at all.
    fake_engine = ModuleType("run_engine")
    fake_engine.run = lambda *_a, **_k: dict(manifest)
    monkeypatch.setitem(sys.modules, "run_engine", fake_engine)
    def fake_translation(current, settings, **_kwargs):
        settings["_agent_stage_settlements"].append(object())
        return {**current, "agent_status": "done", "agent_tags": ["模型标签"]}
    monkeypatch.setattr("app.agent_service.apply_agent_translation", fake_translation)
    monkeypatch.setattr("app.agent_service._write_manifest_file", lambda _manifest: None)
    settled = []
    def fake_settle(stages, *, success):
        assert success is True
        # The done result is already externally visible, with initial tags.
        assert controller.queue.get_job(job_id)["status"] == "done"
        metadata = control.call("documents.metadata", {"task_id": task_id})
        assert metadata["tags"] == ["模型标签"]
        control.call("documents.update_metadata", {"task_id": task_id, "expected_revision": metadata["revision"],
                                                    "tags": ["用户编辑标签"], "title": "用户编辑标题", "category": "用户分类"})
        body = control.call("documents.read", {"task_id": task_id})
        control.call("documents.update", {"task_id": task_id, "tab": "final", "expected_revision": body["revision"],
                                           "text": "用户编辑正文"})
        stages.clear()
        settled.append(True)
        return {"body_usage_units": 0, "body_status": "done"}
    monkeypatch.setattr("engine.agent_client.settle_agent_stages", fake_settle)
    worker = Worker(controller.queue, worker_id="synthetic-worker", settings_store=controller.settings_store,
                    heartbeat_interval=0.05, agent_settings=controller.settings)
    assert worker.step() is True
    assert settled == [True]
    assert controller.queue.get_job(job_id)["status"] == "done"
    assert controller.queue.get_job(job_id)["tags"] == ["用户编辑标签"]
    assert controller.queue.get_job(job_id)["source_title"] == "用户编辑标题"
    assert controller.queue.get_job(job_id)["result_category"] == "用户分类"
    assert final.read_text(encoding="utf-8") == "用户编辑正文"
    assert controller.queue.get_manifest(job_id)["body_status"] == "done"


@pytest.mark.parametrize("delete_method", ["delete_files", "delete_tasks"])
def test_agent_write_and_bridge_delete_share_lock_without_inversion(local, monkeypatch, delete_method):
    from app.bridge import Bridge
    controller, control = local
    task_id, final, _ = completed(controller)
    before = control.call("documents.read", {"task_id": task_id})
    bridge = Bridge(controller, SimpleNamespace(running=False, pid=None), controller.settings_store,
                    lambda: None, lambda: None)
    entered_write, entered_delete, release_write = threading.Event(), threading.Event(), threading.Event()
    original_write = controller._atomic_write_text
    original_delete = getattr(controller, delete_method)
    def paused_write(path, text):
        entered_write.set()
        assert release_write.wait(3)
        original_write(path, text)
    def announced_delete(ids, **kwargs):
        # Bridge already owns its UI lock here and next waits for the controller.
        entered_delete.set()
        return original_delete(ids, **kwargs)
    monkeypatch.setattr(controller, "_atomic_write_text", paused_write)
    monkeypatch.setattr(controller, delete_method, announced_delete)
    with ThreadPoolExecutor(max_workers=3) as executor:
        writer = executor.submit(control.call, "documents.update", {
            "task_id": task_id, "tab": "final", "text": "Agent 正在保存",
            "expected_revision": before["revision"],
        })
        try:
            assert entered_write.wait(3)
            deleter = executor.submit(getattr(bridge, delete_method), [task_id])
            assert entered_delete.wait(3)
            reader = executor.submit(bridge.get_reader, task_id)
            release_write.set()
            assert writer.result(timeout=3)["revision"]
            assert deleter.result(timeout=3) == {"ok": True, "removed": [task_id[4:]]}
            assert reader.result(timeout=3)["ok"] is False
        finally:
            release_write.set()
    assert not final.exists() and not final.parent.exists()
