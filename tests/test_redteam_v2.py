# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""红队 v2：删除链路路径安全、settings 字段边界、reader 路径约束、本地 API 配对/导入边界。

本轮针对 0.7.8 → 0.7.14 新增功能（三种删除、清除完成、设置重构）与既有
loopback API 攻击面做对抗式验证。发现类用例按当前行为断言并标注，修复后
需同步更新。
"""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
from pathlib import Path

import pytest

from app.controller import AppController
from app.local_api import LocalApiServer, MAX_BODY_BYTES
from engine.queue_store import QueueStore
from engine.settings import SettingsStore, SettingsValidationError
from engine.validate import coerce_job
from account_auth_fixtures import grant_account


ORIGIN = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_ORIGIN = "chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _make_controller(temp: str) -> AppController:
    return AppController(SettingsStore(base_dir=Path(temp) / "data"))


def _seed_job(controller: AppController, *, job_id: str, source_path: str, output_dir: str) -> None:
    store: QueueStore = controller.queue
    store.enqueue(coerce_job({
        "id": job_id,
        "source_path": source_path,
        "output_dir": output_dir,
        "status": "queued",
        "whisper_model": "tiny",
        "device": "cpu",
        "language": "zh",
        "content_mode": "raw",
        "created_at": "2026-08-07T10:00:00",
        "updated_at": "2026-08-07T10:00:00",
        "source_title": "红队样本",
        "result_category": "未分类",
    }))


# ------------------------------------------------------------- 删除路径安全
def test_purge_job_output_dir_rejects_traversal_job_id() -> None:
    """job id 含 ../ 时必须拒绝，绝不越出 output 根。"""
    with tempfile.TemporaryDirectory(prefix="redteam_purge_") as temp:
        controller = _make_controller(temp)
        try:
            out_root = Path(controller.settings["output_dir"]).resolve()
            out_root.mkdir(parents=True, exist_ok=True)
            escape = Path(temp) / "escape"
            escape.mkdir(parents=True)
            (escape / "victim.txt").write_text("sensitive", encoding="utf-8")

            # 数据库被篡改时 job_id 可能带 ..；_purge_job_output_dir 必须拒绝
            for malicious in ("../escape", "..", ".", "../", "a/../escape", "%2e%2e/escape"):
                controller._purge_job_output_dir({
                    "id": malicious,
                    "output_dir": str(out_root),
                })
            assert (escape / "victim.txt").is_file(), "路径穿越删除了越界文件"
        finally:
            controller.close()


def test_purge_job_output_dir_removes_only_own_subdir() -> None:
    """正常 job id 只删除 output 根下自己那一份。"""
    with tempfile.TemporaryDirectory(prefix="redteam_purge_ok_") as temp:
        controller = _make_controller(temp)
        try:
            out_root = Path(controller.settings["output_dir"]).resolve()
            target = out_root / "abcdef123456"
            target.mkdir(parents=True)
            (target / "final.txt").write_text("x", encoding="utf-8")
            sibling = out_root / "ffffffffffff"
            sibling.mkdir(parents=True)
            (sibling / "keep.txt").write_text("keep", encoding="utf-8")

            controller._purge_job_output_dir({"id": "abcdef123456", "output_dir": str(out_root)})
            assert not target.exists()
            assert (sibling / "keep.txt").is_file()
        finally:
            controller.close()


def test_safe_remove_downloaded_source_only_within_download_dir() -> None:
    """源文件删除只允许发生在 download_dir 内，越界文件绝不触碰。"""
    with tempfile.TemporaryDirectory(prefix="redteam_remove_") as temp:
        controller = _make_controller(temp)
        try:
            download_root = Path(controller.settings["download_dir"]).resolve()
            download_root.mkdir(parents=True, exist_ok=True)
            inside = download_root / "inside.mp4"
            inside.write_bytes(b"x")
            outside = Path(temp) / "outside.mp4"
            outside.write_bytes(b"keep")

            controller._safe_remove_downloaded_source(str(outside))
            assert outside.is_file(), "download_dir 外文件被删除"
            controller._safe_remove_downloaded_source(str(inside))
            assert not inside.exists()
        finally:
            controller.close()


def test_delete_tasks_unknown_ids_are_noop() -> None:
    """R9 已修复：delete_tasks 对不存在 ID 跳过而非抛错，批量删除不中断。

    本用例同时确认穿越样式 ID 不会造成任何文件删除，真实任务记录保留。
    """
    with tempfile.TemporaryDirectory(prefix="redteam_delete_") as temp:
        controller = _make_controller(temp)
        try:
            victim = Path(temp) / "victim.mp4"
            victim.write_bytes(b"keep")
            _seed_job(controller, job_id="abcdef123456", source_path=str(victim),
                      output_dir=controller.settings["output_dir"])

            deleted = controller.delete_tasks(["../victim", ".", "..", "ghijklmnopqr"])
            assert deleted == []
            assert victim.is_file()
            assert controller.queue.get_job("abcdef123456") is not None
        finally:
            controller.close()


# ------------------------------------------------------------ settings 边界
def test_save_settings_rejects_poiesis_paths() -> None:
    """output_dir / download_dir 指向 Poiesis 必须被拒绝。"""
    with tempfile.TemporaryDirectory(prefix="redteam_settings_") as temp:
        controller = _make_controller(temp)
        try:
            fake_poiesis = Path(temp) / "00_Core" / "Poiesis"
            with pytest.raises(SettingsValidationError):
                controller.save_settings({"output_dir": str(fake_poiesis)})
            with pytest.raises(SettingsValidationError):
                controller.save_settings({"download_dir": str(fake_poiesis)})
        finally:
            controller.close()


def test_save_settings_filters_unknown_keys() -> None:
    """R11 已修复：save_settings 有字段白名单，未知键被过滤不落盘。"""
    with tempfile.TemporaryDirectory(prefix="redteam_settings_") as temp:
        controller = _make_controller(temp)
        try:
            controller.save_settings({"some_unknown_key": "<script>alert(1)</script>"})
            reloaded = SettingsStore(base_dir=Path(temp) / "data").load()
            assert "some_unknown_key" not in reloaded
        finally:
            controller.close()


# ------------------------------------------------------------- reader 路径
def test_reader_rejects_path_outside_output_dir() -> None:
    """R10 已修复：final/raw 读取前校验路径必须位于 output_dir 内。"""
    from app.bridge import Bridge

    with tempfile.TemporaryDirectory(prefix="redteam_reader_") as temp:
        secret = Path(temp) / "secret.txt"
        secret.write_text("top-secret", encoding="utf-8")

        class FakeController:
            media_action_lock = threading.RLock()
            settings = {"output_dir": str(Path(temp) / "output")}

            def task_rows(self, include_archived: bool = False):
                return [{
                    "task_id": "reader000001",
                    "status": "done",
                    "kind": "transcription",
                    "final_path": str(secret),
                    "raw_path": "",
                    "source_path": "",
                    "display_title": "红队样本",
                    "updated_at": "2026-08-07T10:00:00",
                }]

        controller = FakeController()
        bridge = Bridge(controller, object(), None,
                        window_provider=lambda: None, on_quit=lambda: None)
        got = bridge.get_reader("job:reader000001", "final")
        assert got["ok"] is False
        assert got["error"] == "文件不可用"


def test_document_editor_only_writes_completed_output_markdown() -> None:
    """阅读器保存只能原子覆盖已完成任务自己的 Markdown 文件。"""
    with tempfile.TemporaryDirectory(prefix="reader_edit_") as temp:
        controller = _make_controller(temp)
        try:
            output_root = Path(controller.settings["output_dir"])
            job_id = "readeredit01"
            job_dir = output_root / job_id
            job_dir.mkdir(parents=True)
            raw = job_dir / "sample_raw.md"
            final = job_dir / "sample.md"
            raw.write_text("原始", encoding="utf-8")
            final.write_text("# 旧稿", encoding="utf-8")
            _seed_job(controller, job_id=job_id, source_path="C:/sample.mp4", output_dir=str(output_root))
            controller.queue.claim_next("reader-edit")
            controller.queue.save_manifest({
                "job_id": job_id,
                "source_path": "C:/sample.mp4",
                "source_sha256": None,
                "backend": "local",
                "model": "tiny",
                "language": "zh",
                "duration_sec": 1.0,
                "raw_path": str(raw),
                "final_path": str(final),
                "content_mode": "raw",
                "prompt_version": "v1",
                "created_at": "2026-08-07T10:00:00",
            }, worker_id="reader-edit")

            result = controller.update_document_text(job_id, "final", "# 新稿\n")
            assert result["size"] == len("# 新稿\n".encode("utf-8"))
            assert final.read_text(encoding="utf-8") == "# 新稿\n"
            with pytest.raises(ValueError):
                controller.update_document_text(job_id, "source", "不可写")
        finally:
            controller.close()


# ------------------------------------------------------------ local API
def _call(server, method, path, *, token="", origin="", body=None, headers=None):
    host, port = server.address
    connection = http.client.HTTPConnection(host, port, timeout=3)
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    if origin:
        request_headers["Origin"] = origin
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=request_headers)
    response = connection.getresponse()
    raw = response.read()
    result = json.loads(raw.decode("utf-8")) if raw else None
    headers_out = dict(response.getheaders())
    connection.close()
    return response.status, result, headers_out


class StubCoordinator:
    def __init__(self) -> None:
        self.jobs = []
        self.started = False

    def start(self):
        self.started = True

    def stop(self, timeout=3.0):
        self.started = False

    def submit(self, url, *, title="", options=None, session_cookies=None, mode="local"):
        item = {
            "id": f"{len(self.jobs) + 1:012x}", "url": url, "title": title,
            "source": "browser", "platform_hint": "douyin",
            "requested_at": "2026-08-07T10:00:00",
            "updated_at": "2026-08-07T10:00:00",
            "options": options or {}, "status": "received", "progress": 0.0,
            "downloaded_path": "C:/private/video.mp4", "cancel_requested": False,
            "attempts": 0,
        }
        self.jobs.append(item)
        return item

    def submit_local_file(self, path, *, title="", transcribe=True, download_id=None):
        return {"media_id": "media-local", "job_id": "job-local", "path": path,
                "download_id": download_id}

    def list(self, *, statuses=None, limit=100):
        return self.jobs[-limit:]

    def get(self, download_id):
        return next((item for item in self.jobs if item["id"] == download_id), None)

    def cancel(self, download_id):
        return self.get(download_id)

    def retry(self, download_id):
        return self.get(download_id)


def test_local_api_rejects_bad_token_and_malicious_origin() -> None:
    with tempfile.TemporaryDirectory(prefix="redteam_api_") as temp:
        controller = _make_controller(temp)
        server = LocalApiServer(controller, StubCoordinator(), port=0)
        server.start()
        token = controller.settings["local_api_token"]
        try:
            status, _, _ = _call(server, "GET", "/v1/downloads", token="wrong-token")
            assert status == 401
            status, data, headers = _call(
                server, "GET", "/v1/downloads", token=token, origin="http://evil.example",
            )
            assert status == 403
            assert "Access-Control-Allow-Origin" not in headers
            status, _, _ = _call(server, "GET", "/v1/downloads", token=token, origin="http://127.0.0.1:9999")
            assert status == 403
        finally:
            server.stop()
            controller.close()


def test_local_api_no_origin_with_token_is_design_behavior() -> None:
    """无 Origin + 正确 token 的本地进程调用被放行（curl/本机工具），CORS 不外发。"""
    with tempfile.TemporaryDirectory(prefix="redteam_api_") as temp:
        controller = _make_controller(temp)
        server = LocalApiServer(controller, StubCoordinator(), port=0)
        server.start()
        token = controller.settings["local_api_token"]
        try:
            status, _, headers = _call(server, "GET", "/v1/downloads", token=token)
            assert status == 200
            assert "Access-Control-Allow-Origin" not in headers
        finally:
            server.stop()
            controller.close()


def test_local_api_setup_requires_pairing_window_and_is_one_shot() -> None:
    """R8 已修复：/v1/setup 与 pair 同窗口要求，未开窗 403，成功后窗口一次性消费。

    浏览器网页无法伪造 chrome-extension:// Origin（浏览器强制），但恶意扩展
    或本机进程可在首次配对前抢先调用；现在配对窗口未开启时直接 403，且
    成功消费后窗口关闭，无法在同一窗口内二次抢注。
    """
    with tempfile.TemporaryDirectory(prefix="redteam_api_") as temp:
        controller = _make_controller(temp)
        server = LocalApiServer(controller, StubCoordinator(), port=0)
        server.start()
        try:
            # 未开窗：403
            status, data, _ = _call(server, "POST", "/v1/setup", origin=ORIGIN)
            assert status == 403
            assert data["error"]["code"] == "pairing_closed"
            # 开窗后成功且一次性消费
            server.begin_pairing(1)
            status, data, _ = _call(server, "POST", "/v1/setup", origin=ORIGIN)
            assert status == 200
            assert data["token"] == controller.settings["local_api_token"]
            assert server.origin_policy.origin == ORIGIN
            # 窗口已被消费：不开新窗时重复调用被拒
            status, _, _ = _call(server, "POST", "/v1/setup", origin=ORIGIN)
            assert status == 403
            # 真实扩展随后被拒绝
            status, _, _ = _call(server, "GET", "/v1/downloads",
                                 token=controller.settings["local_api_token"],
                                 origin=OTHER_ORIGIN)
            assert status == 403
        finally:
            server.stop()
            controller.close()


def test_local_api_pair_closed_without_pairing_window() -> None:
    with tempfile.TemporaryDirectory(prefix="redteam_api_") as temp:
        controller = _make_controller(temp)
        server = LocalApiServer(controller, StubCoordinator(), port=0)
        server.start()
        try:
            status, _, _ = _call(server, "POST", "/v1/pair", origin=ORIGIN)
            assert status == 403
            server.begin_pairing(1)
            status, data, _ = _call(server, "POST", "/v1/pair", origin=ORIGIN)
            assert status == 200
            assert data["token"] == controller.settings["local_api_token"]
            # token 只发给合法扩展 origin
            status, _, _ = _call(server, "POST", "/v1/pair", origin="http://evil.example")
            assert status == 403
        finally:
            server.stop()
            controller.close()


def test_local_api_imports_path_restrictions() -> None:
    with tempfile.TemporaryDirectory(prefix="redteam_api_") as temp:
        controller = _make_controller(temp)
        controller.settings = grant_account(controller.settings_store, controller.settings_store.base_dir)
        server = LocalApiServer(controller, StubCoordinator(), port=0)
        server.start()
        token = controller.settings["local_api_token"]
        outside = Path(temp) / "outside.mp4"
        outside.write_bytes(b"x")
        try:
            # 白名单外路径
            status, _, _ = _call(server, "POST", "/v1/imports", token=token, origin=ORIGIN,
                                 body={"path": str(outside)})
            assert status == 400
            # R12 已修复：穿越/不存在路径 resolve(strict=True) 抛 OSError，
            # 统一转为 400 客户端错误，而不是 500。
            status, _, _ = _call(server, "POST", "/v1/imports", token=token, origin=ORIGIN,
                                 body={"path": "../outside.mp4"})
            assert status == 400
            # 空路径
            status, _, _ = _call(server, "POST", "/v1/imports", token=token, origin=ORIGIN,
                                 body={"path": "  "})
            assert status == 400
        finally:
            server.stop()
            controller.close()


def test_local_api_body_size_and_batch_limits() -> None:
    with tempfile.TemporaryDirectory(prefix="redteam_api_") as temp:
        controller = _make_controller(temp)
        controller.settings = grant_account(controller.settings_store, controller.settings_store.base_dir)
        server = LocalApiServer(controller, StubCoordinator(), port=0)
        server.start()
        token = controller.settings["local_api_token"]
        try:
            # 超大 body
            status, _, _ = _call(
                server, "POST", "/v1/downloads", token=token, origin=ORIGIN,
                headers={"Content-Length": str(MAX_BODY_BYTES + 1)},
                body={},
            )
            assert status == 400
            # 批量超过 50
            status, _, _ = _call(server, "POST", "/v1/downloads/batch", token=token, origin=ORIGIN,
                                 body={"items": [{"url": "https://douyin.com/video/1"}] * 51})
            assert status == 400
        finally:
            server.stop()
            controller.close()


def test_local_api_origin_preflight_policy() -> None:
    """未配对时合法扩展 origin 可过预检（配对流程所需）；恶意 origin 一律拒绝。"""
    with tempfile.TemporaryDirectory(prefix="redteam_api_") as temp:
        controller = _make_controller(temp)
        server = LocalApiServer(controller, StubCoordinator(), port=0)
        server.start()
        try:
            # 合法扩展 origin（未配对）→ 预检放行，这是配对握手的一部分
            status, _, headers = _call(server, "OPTIONS", "/v1/downloads", origin=ORIGIN)
            assert status == 204
            # 恶意 http origin → 拒绝
            status, _, _ = _call(server, "OPTIONS", "/v1/downloads", origin="http://evil.example")
            assert status == 403
            status, _, headers = _call(server, "OPTIONS", "/v1/pair", origin=ORIGIN)
            assert status == 204
            assert headers.get("Access-Control-Allow-Origin") == ORIGIN
        finally:
            server.stop()
            controller.close()
