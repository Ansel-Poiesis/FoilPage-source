# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""FoilPage 0.7 WebView2 Bridge 单测（mock controller / worker）。"""
from __future__ import annotations

import tempfile
import json
import threading
from unittest import mock
from pathlib import Path

import pytest

from app.bridge import APP_VERSION, Bridge
from engine.model_manager import ModelManagerError, ModelSpec, ModelState
from account_auth_fixtures import grant_account


def test_agent_cannot_write_user_terms_confirmation(tmp_path):
    from engine.settings import SettingsStore

    bridge, _, _ = make_bridge()
    bridge.settings_store = SettingsStore(base_dir=tmp_path)
    assert not {"terms_accepted_version", "terms_cross_border_accepted_version", "terms_accepted_service"} & bridge._agent_editable_settings()


@pytest.mark.parametrize("version", [3, True, "3", {}, -1, 1_000_001])
def test_subscription_contract_exposes_only_a_bounded_terms_version(version):
    bridge, _, _ = make_bridge(settings={"subscription_server_url": "https://pay.example.invalid"})
    bridge._subscription_service_base = "https://pay.example.invalid"
    bridge._subscription_service = {"terms": {"version": version}}
    assert bridge.get_subscription_contract()["terms"] == ({"version": 3} if type(version) is int and version == 3 else {})


class FakeController:
    def __init__(self, rows=None, settings=None):
        self.media_action_lock = threading.RLock()
        self.rows = rows or []
        self.settings = dict(settings or {
            "device": "cuda",
            "output_dir": str(Path.cwd()),
            "local_api_port": 18765,
            "local_api_token": "",
            "extension_origin": "",
        })
        self.saved = []
        self.downloads = mock.Mock()
        self.downloads.list.return_value = []
        self.queue = mock.Mock()
        self.queue.list_jobs.return_value = []
        self.media = mock.Mock()
        self.media.list_media.return_value = []
        self.purged_outputs = []
        self.purged_downloads = []

    def purge_orphan_output(self, path):
        self.purged_outputs.append(path)
        return True

    def purge_orphan_download(self, path):
        self.purged_downloads.append(path)
        return True

    def task_rows(self, include_archived: bool = False):
        self.task_rows_calls = getattr(self, "task_rows_calls", [])
        self.task_rows_calls.append(include_archived)
        return self.rows

    def clear_completed(self):
        return 0

    def save_settings(self, changes):
        self.settings.update(changes)
        self.saved.append(dict(changes))
        return self.settings

    def cancel_jobs(self, ids):
        self.cancel_jobs_called = ids

    def cancel_download_tasks(self, ids):
        self.cancel_downloads_called = ids

    def requeue_jobs(self, ids):
        return ids

    def retranscribe_jobs(self, ids):
        class R:
            created = [f"job:{i}" for i in ids]
            skipped = []
        return R()

    def delete_tasks(self, ids):
        return ids

    def delete_download_tasks(self, ids):
        return ids

    def set_result_categories(self, ids, category):
        return len(ids)

    def set_job_tags(self, job_id, tags):
        self.set_job_tags_calls = getattr(self, "set_job_tags_calls", [])
        self.set_job_tags_calls.append((job_id, list(tags)))
        return list(tags)

    def queue_download_for_transcription(self, download_id):
        class Q:
            created = [f"job-{download_id}"]
            skipped = []
        self.queued_download = download_id
        return Q()

    def import_files(self, paths):
        return [{"id": "m1", "path": paths[0]}]

    def enqueue_media(self, ids):
        class E:
            created = ["j1"]
            skipped = []
        return E()

    def import_folder(self, path):
        class S:
            imported = [{"id": "m2"}]
        return S()


class FakeWorker:
    def __init__(self, running=False, pid=None):
        self._running = running
        self._pid = pid if pid is not None else (1234 if running else None)

    @property
    def running(self):
        return self._running

    @property
    def pid(self):
        return self._pid if self._running else None

    def start(self):
        self._running = True
        self._pid = 1234

    def stop(self):
        self._running = False
        self._pid = None


class FakeStore:
    base_dir = Path.cwd() / "fake-data"

    def __init__(self, base_dir=None):
        if base_dir is not None:
            self.base_dir = Path(base_dir)


class FakeWindow:
    events = None

    def __init__(self):
        self.dialog_result = None
        self.dialog_calls = []
        self.eval_scripts = []

    def create_file_dialog(self, *args, **kwargs):
        self.dialog_calls.append((args, kwargs))
        return self.dialog_result

    def evaluate_js(self, script):
        self.eval_scripts.append(script)


class FakeLocalApi:
    def __init__(self, running=True, configured=False, port=18765, origin=""):
        self._running = running
        self.origin = origin
        self.port = port
        self.pairing_calls = 0
        self.submitted: list[dict] = []
        self.desktop_douyin_submitted: list[dict] = []

    @property
    def running(self):
        return self._running

    @property
    def origin_policy(self):
        return self

    @property
    def coordinator(self):
        return self

    def submit(self, url, *, title="", options=None, mode="local"):
        self.submitted.append({
            "url": url, "title": title,
            "options": options or {}, "mode": mode,
        })
        if "bad-url" in url:
            from engine.downloader import DownloadAdapterError
            raise DownloadAdapterError("unsupported_source", "当前链接来源尚未启用")
        return {"id": "a1b2c3d4e5f6", "url": url, "status": "received", "title": title}

    def submit_douyin_link_from_desktop(self, url, *, title="", transcribe=True):
        self.desktop_douyin_submitted.append({
            "url": url, "title": title, "transcribe": transcribe,
        })
        return {"id": "d0d0d0d0d0d0", "url": url, "status": "received", "title": title}

    def begin_pairing(self, seconds=120):
        self.pairing_calls += 1
        return 120


def make_bridge(rows=None, settings=None, worker_running=False, window=None, account_dir=None):
    controller = FakeController(rows=rows, settings=settings)
    worker = FakeWorker(running=worker_running)
    store = FakeStore(account_dir)
    if account_dir is not None:
        grant_account(controller.settings, store.base_dir)
    bridge = Bridge(controller, worker, store, window_provider=lambda: window, on_quit=lambda: None)
    return bridge, controller, worker


def _row(kind, task_id, status="done", **extra):
    row = {"kind": kind, "task_id": task_id, "status": status, "display_title": "标题",
           "source": "本地转写", "progress": 1.0, "updated_at": "2026-08-05T12:00:00",
           "result_category": "未分类", "final_path": "", "raw_path": "",
           "source_path": "C:/x/video.mp4", "downloaded_path": "", "device": "cuda"}
    row.update(extra)
    return row


# ---------------- GPU 加速包（先生 2026-10-08 拆包） ----------------
import zipfile


def _write_gpu_zip(path: Path, *, valid: bool = True, evil: bool = False) -> None:
    with zipfile.ZipFile(path, "w") as bundle:
        if valid:
            bundle.writestr("nvidia/cublas/bin/cublas64_12.dll", b"dll")
            bundle.writestr("nvidia/cudnn/bin/cudnn64_9.dll", b"dll")
            bundle.writestr("nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll", b"dll")
        else:
            bundle.writestr("readme.txt", "not a gpu pack")
        if evil:
            bundle.writestr("../escape.dll", b"evil")


def test_gpu_pack_state_detects_installed_libs(tmp_path):
    bridge, _, _ = make_bridge()
    internal = tmp_path / "worker" / "_internal"
    (internal / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    (internal / "nvidia" / "cublas" / "bin" / "cublas64_12.dll").write_bytes(b"dll")
    with mock.patch.object(bridge, "_worker_internal_dir", return_value=internal):
        state = bridge._gpu_pack_state()
    assert state["installed"] is False
    assert state["components"] == ["cublas"]


def test_install_gpu_pack_validates_and_installs(tmp_path):
    bridge, _, _ = make_bridge()
    internal = tmp_path / "worker" / "_internal"
    internal.mkdir(parents=True)
    pack = tmp_path / "gpu-pack.zip"
    _write_gpu_zip(pack)
    with mock.patch.object(bridge, "_worker_internal_dir", return_value=internal):
        bad = bridge.install_gpu_pack(str(tmp_path / "missing.zip"))
        assert bad["ok"] is False
        wrong = tmp_path / "wrong.zip"
        _write_gpu_zip(wrong, valid=False)
        assert bridge.install_gpu_pack(str(wrong))["ok"] is False
        evil = tmp_path / "evil.zip"
        _write_gpu_zip(evil, evil=True)
        assert "路径不合法" in bridge.install_gpu_pack(str(evil))["error"]
        result = bridge.install_gpu_pack(str(pack))
    assert result["ok"] is True
    assert result["needs_restart"] is True
    assert (internal / "nvidia" / "cublas" / "bin" / "cublas64_12.dll").is_file()
    assert not (internal / ".gpu-pack-staging").exists()


def test_bootstrap_shape():
    bridge, _, _ = make_bridge(rows=[_row("transcription", "a1")])
    boot = bridge.get_bootstrap()
    assert boot["version"] == APP_VERSION
    assert boot["settings"]["data_dir"] == str(Path.cwd() / "fake-data")
    assert len(boot["tasks"]) == 1
    assert boot["summary"]["total"] == 1


def test_task_id_prefixes():
    rows = [
        _row("transcription", "j1", status="done"),
        _row("download", "d1", status="received"),
    ]
    bridge, _, _ = make_bridge(rows=rows)
    tasks = bridge.get_tasks()
    ids = {t["task_id"] for t in tasks}
    assert ids == {"job:j1", "dl:d1"}


def test_task_error_details_keep_actionable_diagnostics_and_redact_secrets():
    bridge, _, _ = make_bridge(rows=[_row(
        "download", "d1", status="failed", error={
            "code": "browser_download_interrupted",
            "message": "Chrome returned HTTP 403 for https://media.example/video?token=secret",
            "retryable": False,
            "diagnostic": {
                "channel": "chrome.downloads",
                "phase": "browser_download_interrupted",
                "browser_error": "SERVER_FORBIDDEN",
                "browser_detail": "https://media.example/video?token=secret",
                "cookie_value": "session-secret",
                "bytes_received": 0,
            },
        },
    )])

    task = bridge.get_tasks()[0]

    assert "SERVER_FORBIDDEN" in task["error_details"]["diagnostic"]["browser_error"]
    assert task["error_details"]["diagnostic"]["bytes_received"] == 0
    serialized = json.dumps(task, ensure_ascii=False)
    assert "media.example" not in serialized
    assert "token=secret" not in serialized
    assert "session-secret" not in serialized


def test_summary_counts():
    rows = [
        _row("transcription", "a", status="done"),
        _row("transcription", "b", status="queued"),
        _row("transcription", "c", status="failed"),
    ]
    bridge, _, _ = make_bridge(rows=rows)
    summary = bridge.get_summary()
    assert summary["total"] == 3
    assert summary["active"] == 1
    assert summary["terminal"] == 2
    assert summary["completed"] == 1
    assert summary["progress"] == pytest.approx(1 / 3, abs=0.0001)


def test_worker_state_uses_property():
    bridge, _, _ = make_bridge(worker_running=True)
    state = bridge.get_worker()
    assert state["running"] is True
    assert state["pid"] == 1234
    bridge2, _, _ = make_bridge(worker_running=False)
    state2 = bridge2.get_worker()
    assert state2["running"] is False
    assert state2["pid"] is None


def test_worker_toggle(tmp_path):
    bridge, _, worker = make_bridge(worker_running=False, account_dir=tmp_path / "data")
    state = bridge.worker_toggle()
    assert state["running"] is True
    assert worker._running is True
    state = bridge.worker_toggle()
    assert state["running"] is False


def test_cancel_prefix_split():
    bridge, controller, _ = make_bridge()
    bridge.cancel_tasks(["job:j1", "dl:d2"])
    assert controller.cancel_jobs_called == ["j1"]
    assert controller.cancel_downloads_called == ["d2"]


def test_delete_prefix_split():
    bridge, controller, _ = make_bridge()
    result = bridge.delete_tasks(["job:j1", "dl:d2"])
    assert result["ok"] is True
    assert "j1" in result["removed"]
    assert "d2" in result["removed"]


def test_requeue_jobs_only(tmp_path):
    bridge, controller, _ = make_bridge(account_dir=tmp_path / "data")
    result = bridge.requeue_tasks(["job:j1", "dl:d2"])
    assert result["requeued"] == ["job:j1"]


def test_set_category_jobs_only():
    bridge, controller, _ = make_bridge()
    result = bridge.set_category(["job:j1", "dl:d2"], "知识科普")
    assert result["updated"] == 1


def test_get_documents_includes_tags_and_searches_tags(tmp_path):
    raw = tmp_path / "raw.md"
    raw.write_text("fixture", encoding="utf-8")
    rows = [
        _row("transcription", "a1", tags=["访谈"], raw_path=str(raw)),
        _row("transcription", "a2", tags=["复盘"], raw_path=str(raw)),
        _row("transcription", "a3", raw_path=str(raw)),
    ]
    bridge, _, _ = make_bridge(rows=rows)
    docs = bridge.get_documents("", "全部")
    by_id = {d["task_id"]: d for d in docs}
    assert by_id["job:a1"]["tags"] == ["访谈"]
    assert by_id["job:a2"]["tags"] == ["复盘"]
    assert by_id["job:a3"]["tags"] == []
    found = bridge.get_documents("访谈", "全部")
    assert [d["task_id"] for d in found] == ["job:a1"]


def test_set_job_tags_strips_prefix():
    bridge, controller, _ = make_bridge()
    result = bridge.set_job_tags("job:j1", ["a", "b"])
    assert result == {"ok": True, "tags": ["a", "b"]}
    assert controller.set_job_tags_calls == [("j1", ["a", "b"])]


def test_get_tag_index_counts_transcription_tags_only(tmp_path):
    raw = tmp_path / "raw.md"
    raw.write_text("fixture", encoding="utf-8")
    rows = [
        _row("transcription", "a1", tags=["访谈", "复盘"], raw_path=str(raw)),
        _row("transcription", "a2", tags=["访谈"], raw_path=str(raw)),
        _row("transcription", "removed", tags=["无文本"]),
        _row("download", "d1", tags=["不应计入"]),
    ]
    bridge, _, _ = make_bridge(rows=rows)
    assert bridge.get_tag_index() == [
        {"tag": "访谈", "count": 2},
        {"tag": "复盘", "count": 1},
    ]


def test_save_settings_forwarded():
    bridge, controller, _ = make_bridge()
    result = bridge.save_settings({"theme": "dark"})
    assert result["theme"] == "dark"
    assert controller.saved[-1] == {"theme": "dark"}


def test_save_settings_paper_theme_forwarded():
    bridge, controller, _ = make_bridge()
    result = bridge.save_settings({"theme": "paper"})
    assert result["theme"] == "paper"
    assert controller.saved[-1] == {"theme": "paper"}


def test_pick_setting_dir_whitelist():
    import webview

    window = FakeWindow()
    window.dialog_result = ["C:/chosen"]
    bridge, controller, _ = make_bridge(window=window)
    result = bridge.pick_setting_dir("output_dir")
    assert result["ok"] is True
    assert controller.settings["output_dir"] == "C:/chosen"
    # pywebview 的对话框常量在 webview 模块上，Window 实例没有该属性。
    assert window.dialog_calls[0][0][0] == webview.FileDialog.FOLDER
    bad = bridge.pick_setting_dir("database_path")
    assert bad["ok"] is False


def test_pick_setting_dir_cancelled():
    window = FakeWindow()
    window.dialog_result = None
    bridge, _, _ = make_bridge(window=window)
    result = bridge.pick_setting_dir("output_dir")
    assert result.get("cancelled") is True


@pytest.mark.parametrize("activity", [
    "model_download", "transcription_running", "download_transcribing", "download_processing",
])
def test_cache_directory_picker_rechecks_activity_that_starts_while_open(activity):
    window = FakeWindow()
    bridge, controller, _ = make_bridge(window=window, settings={"model_dir": "C:/original"})

    def choose_after_work_starts(*_args, **_kwargs):
        if activity == "model_download":
            bridge._model_download["name"] = "small"
        else:
            kind, status = activity.split("_", 1)
            controller.rows = [{"kind": kind, "status": status}]
        return ["C:/chosen"]

    window.create_file_dialog = choose_after_work_starts
    result = bridge.pick_setting_dir("model_dir")
    assert result["ok"] is False
    assert "模型正在使用或下载" in result["error"]
    assert controller.settings["model_dir"] == "C:/original"
    assert controller.saved == []
    if activity != "model_download":
        assert controller.task_rows_calls == [True], "archived active work must also be protected"


def test_idle_cache_directory_picker_allows_video_only_downloads_and_preserves_other_settings():
    window = FakeWindow()
    window.dialog_result = ["C:/chosen"]
    bridge, controller, _ = make_bridge(window=window, rows=[{"kind": "download", "status": "downloading"}],
                                      settings={"model_dir": "C:/original", "download_dir": "C:/videos", "theme": "paper"})
    assert bridge.pick_setting_dir("model_dir")["ok"] is True
    assert controller.saved == [{"model_dir": "C:/chosen"}]
    assert controller.settings["download_dir"] == "C:/videos"
    assert controller.settings["theme"] == "paper"


def test_cache_directory_picker_does_not_save_when_activity_cannot_be_checked():
    window = FakeWindow()
    window.dialog_result = ["C:/chosen"]
    bridge, controller, _ = make_bridge(window=window, settings={"model_dir": "C:/original"})
    controller.task_rows = mock.Mock(side_effect=RuntimeError("fixture task lookup unavailable"))
    result = bridge.pick_setting_dir("model_dir")
    assert result == {"ok": False, "error": "无法确认模型使用状态，请稍后重试"}
    assert controller.settings["model_dir"] == "C:/original"
    assert controller.saved == []


def test_video_directory_picker_remains_available_during_model_download():
    window = FakeWindow()
    window.dialog_result = ["C:/videos"]
    bridge, controller, _ = make_bridge(window=window)
    bridge._model_download["name"] = "small"
    assert bridge.pick_setting_dir("download_dir")["ok"] is True
    assert controller.saved == [{"download_dir": "C:/videos"}]


def test_open_path_missing_file():
    bridge, _, _ = make_bridge()
    result = bridge.open_path("Z:/no/such/file.mp4")
    assert result["ok"] is False


def test_open_path_wrong_kind():
    bridge, _, _ = make_bridge()
    result = bridge.open_directory("Z:/no/such/dir")
    assert result["ok"] is False


def test_archived_rows_hidden_from_workbench_kept_in_library() -> None:
    rows = [
        _row("transcription", "done-1", status="done", display_title="已完成文档"),
    ]
    bridge, controller, _ = make_bridge(rows=rows)
    # 工作台默认不包含归档
    bridge.get_tasks()
    assert controller.task_rows_calls[-1] is False
    # 文本库与阅读器包含归档
    bridge.get_documents("", "全部")
    assert controller.task_rows_calls[-1] is True
    bridge.get_reader("job:done-1", "final")
    assert controller.task_rows_calls[-1] is True
    # 清除完成接口透传
    result = bridge.clear_completed()
    assert result == {"ok": True, "removed": 0}


def test_get_documents_filters(tmp_path):
    raw, final = tmp_path / "raw.md", tmp_path / "final.md"
    raw.write_text("raw fixture", encoding="utf-8")
    final.write_text("final fixture", encoding="utf-8")
    rows = [
        _row("transcription", "a", status="done", raw_path=str(raw)),
        _row("transcription", "b", status="done", display_title="已整理文档",
             final_path=str(final)),
        _row("transcription", "missing", status="done", final_path="Z:/x/missing.md"),
        _row("download", "c", status="done"),
    ]
    bridge, _, _ = make_bridge(rows=rows)
    docs = bridge.get_documents("", "全部")
    assert all(d["task_id"].startswith("job:") for d in docs)
    assert len(docs) == 2
    assert [doc["task_id"] for doc in bridge.get_documents("", "已整理")] == ["job:b"]
    assert [doc["task_id"] for doc in bridge.get_documents("", "仅转写")] == ["job:a"]
    # query match on title
    matched = bridge.get_documents("已整理文档", "全部")
    assert len(matched) == 1
    assert matched[0]["title"] == "已整理文档"


def test_push_state_skips_without_window():
    bridge, _, _ = make_bridge(window=None)
    bridge.push_state()  # should not raise


def test_push_state_evaluates_js():
    window = FakeWindow()
    bridge, _, _ = make_bridge(window=window)
    bridge.push_state()
    assert window.eval_scripts
    assert "__onState(" in window.eval_scripts[0]


def test_import_files_enqueues(tmp_path):
    bridge, controller, _ = make_bridge(account_dir=tmp_path / "data")
    result = bridge.import_files(["C:/a.mp4"])
    assert result["ok"] is True
    assert result["imported"] == 1


def test_set_launch_at_startup(monkeypatch):
    import app.bridge as bridge_module

    calls = []
    monkeypatch.setattr(bridge_module, "enable_startup", lambda: calls.append("on"), raising=False)
    monkeypatch.setattr(bridge_module, "disable_startup", lambda: calls.append("off"), raising=False)
    # inject via module-level import inside method: patch app.startup
    import app.startup as startup_module
    monkeypatch.setattr(startup_module, "enable_startup", lambda: calls.append("on"))
    monkeypatch.setattr(startup_module, "disable_startup", lambda: calls.append("off"))

    bridge, controller, _ = make_bridge()
    bridge.set_launch_at_startup(True)
    assert calls == ["on"]
    assert controller.settings["launch_at_startup"] is True
    bridge.set_launch_at_startup(False)
    assert calls == ["on", "off"]
    assert controller.settings["launch_at_startup"] is False


class TestBrowserPairing:
    def make(self, account_dir=None, **kw):
        controller = FakeController()
        worker = FakeWorker()
        api = FakeLocalApi(**kw)
        store = FakeStore(account_dir)
        if account_dir is not None:
            grant_account(controller.settings, store.base_dir)
        bridge = Bridge(
            controller, worker, store,
            window_provider=lambda: None, on_quit=lambda: None,
            local_api_provider=lambda: api,
        )
        return bridge, api

    def test_browser_status_configured(self):
        bridge, api = self.make(configured=True, origin="chrome-extension://goednbbhmhdkfbekdfmlieggdpeiikfb")
        st = bridge.browser_status()
        assert st["ok"] is True
        assert st["running"] is True
        assert st["configured"] is True
        assert st["origin"] == "chrome-extension://goednbbhmhdkfbekdfmlieggdpeiikfb"
        assert st["port"] == 18765
        assert st["extension_dir"].endswith("browser-extension")

    def test_browser_status_unconfigured(self):
        bridge, api = self.make(configured=False)
        st = bridge.browser_status()
        assert st["ok"] is True
        assert st["configured"] is False
        assert st["origin"] == ""

    def test_browser_status_stopped(self):
        bridge, api = self.make(running=False)
        st = bridge.browser_status()
        assert st["ok"] is True
        assert st["running"] is False
        assert st["configured"] is False

    def test_begin_pairing_starts_window(self):
        bridge, api = self.make()
        result = bridge.begin_browser_pairing()
        assert result["ok"] is True
        assert api.pairing_calls == 1
        assert result["seconds"] == 120
        assert result["port"] == 18765

    def test_begin_pairing_no_api(self):
        controller = FakeController()
        worker = FakeWorker()
        bridge = Bridge(controller, worker, FakeStore(), window_provider=lambda: None, on_quit=lambda: None)
        result = bridge.begin_browser_pairing()
        assert result["ok"] is False

    def test_begin_pairing_api_stopped(self):
        bridge, api = self.make(running=False)
        result = bridge.begin_browser_pairing()
        assert result["ok"] is False
        assert api.pairing_calls == 0

    def test_download_from_url_submits_local_ytdlp_rail(self, tmp_path):
        bridge, api = self.make(account_dir=tmp_path / "data")
        result = bridge.download_from_url(
            "https://www.bilibili.com/video/BV1abc", "测试", False,
        )
        assert result["ok"] is True
        assert api.submitted and api.submitted[0]["url"] == "https://www.bilibili.com/video/BV1abc"
        assert api.submitted[0]["title"] == "测试"
        assert api.submitted[0]["mode"] == "local"
        assert api.submitted[0]["options"]["transcribe_after_download"] is False

    def test_download_from_url_requests_one_shot_extension_auth_for_douyin_share_links(self, tmp_path):
        bridge, api = self.make(account_dir=tmp_path / "data")
        result = bridge.download_from_url(
            "9.74 10/23 @某人: 《标题》观看后 #知识推荐 https://v.douyin.com/short-id/复制此链接，打开抖音搜索，直接观看视频！",
            "抖音标题", False,
        )
        assert result["ok"] is True
        assert api.desktop_douyin_submitted == [{
            "url": "https://v.douyin.com/short-id/",
            "title": "抖音标题",
            "transcribe": False,
        }]
        assert not api.submitted

    def test_download_from_url_keeps_direct_douyin_media_on_local_rail(self, tmp_path):
        bridge, api = self.make(account_dir=tmp_path / "data")
        result = bridge.download_from_url(
            "https://v3-web.douyinvod.com/video/tos/example.mp4", "", False,
        )
        assert result["ok"] is True
        assert not api.desktop_douyin_submitted
        assert api.submitted[0]["url"].endswith("example.mp4")

    def test_download_from_url_rejects_bad_source(self, tmp_path):
        bridge, api = self.make(account_dir=tmp_path / "data")
        result = bridge.download_from_url("https://bad-url.example/v")
        assert result["ok"] is False
        assert result["code"] == "unsupported_source"
        assert api.submitted

    def test_download_from_url_no_local_api(self, tmp_path):
        controller = FakeController()
        worker = FakeWorker()
        store = FakeStore(tmp_path / "data")
        grant_account(controller.settings, store.base_dir)
        bridge = Bridge(controller, worker, store, window_provider=lambda: None, on_quit=lambda: None)
        result = bridge.download_from_url("https://www.bilibili.com/video/BV1abc")
        assert result["ok"] is False
        assert "本地 API" in result["error"]


def test_download_done_shows_downloaded_text():
    bridge, _, _ = make_bridge(rows=[_row("download", "d1", status="done", source_path="")])
    task = bridge.get_tasks()[0]
    assert task["progress_text"] == "已下载"


def test_local_task_icon_is_unified_play_icon():
    bridge, _, _ = make_bridge(rows=[_row("transcription", "j1", status="done", source_path="C:/x/video.MP4")])
    task = bridge.get_tasks()[0]
    assert task["icon"] == "▶"


@pytest.mark.parametrize("download_id,stored_id", [
    ("dl:d1", "d1"),
    ("012345abcdef", "012345abcdef"),
])
def test_transcribe_download_bridge_accepts_ui_and_agent_ids(tmp_path, download_id, stored_id):
    bridge, controller, _ = make_bridge(rows=[_row("download", stored_id, status="done", source_path="")],
                                        account_dir=tmp_path / "data")
    res = bridge.transcribe_download(download_id)
    assert res["ok"] is True
    assert res["created"] == [f"job:job-{stored_id}"]
    assert controller.queued_download == stored_id


def test_transcribe_download_already_queued_reports_merge_feedback(tmp_path):
    bridge, _, _ = make_bridge(
        rows=[_row("download", "d1", status="done", source_path="")],
        account_dir=tmp_path / "data",
    )

    class Skipped:
        created = []
        skipped = ["d1"]

    stub = Skipped()
    bridge.controller.queue_download_for_transcription = lambda _id: stub
    res = bridge.transcribe_download("dl:d1")
    assert res["ok"] is False
    assert res["already"] is True
    assert res["skipped"] == 1
    assert "已在转写队列" in res["error"]


def test_agent_download_transcribe_operation_passes_raw_query_id(tmp_path):
    from app.agent_operations import AgentOperations

    bridge, controller, _ = make_bridge(
        rows=[_row("download", "012345abcdef", status="done", source_path="")],
        account_dir=tmp_path / "data",
    )
    operations = AgentOperations(bridge.agent_operations_facade())

    result = operations.call("downloads.transcribe", {"download_id": "012345abcdef"})

    assert result["created"] == ["job:job-012345abcdef"]
    assert controller.queued_download == "012345abcdef"


def test_transcribe_download_rejects_bad_prefix():
    bridge, _, _ = make_bridge()
    res = bridge.transcribe_download("job:j1")
    assert res["ok"] is False


# ------------------------------------------------------------- 模型管理
def _fake_states():
    specs = [
        ModelSpec("tiny", "Systran/faster-whisper-tiny", "约 75 MB", "快速验证"),
        ModelSpec("large-v3", "Systran/faster-whisper-large-v3", "约 3 GB", "最高质量"),
    ]
    return [
        ModelState(specs[0], installed=True, verified=True,
                   path="C:/cache/hub/tiny", bytes_on_disk=10),
        ModelState(specs[1], installed=False, verified=False,
                   path=None, bytes_on_disk=0),
    ]


def test_get_models_lists_cache_state_in_temp_dir():
    with tempfile.TemporaryDirectory(prefix="bridge_models_") as temp:
        bridge, controller, _ = make_bridge(settings={"model_dir": temp})
        result = bridge.get_models()
        assert result["ok"] is True
        assert result["cache_root"] == str(Path(temp).resolve())
        assert {m["name"] for m in result["models"]} == {
            "tiny", "base", "small", "medium", "large-v3",
        }
        assert all(m["installed"] is False for m in result["models"])


def test_download_model_starts_background_download():
    with tempfile.TemporaryDirectory(prefix="bridge_models_") as temp:
        bridge, _, _ = make_bridge(settings={"model_dir": temp})
        fake = mock.Mock()
        fake.list_models.return_value = _fake_states()
        with (
            mock.patch("engine.model_manager.ModelManager", return_value=fake),
            mock.patch.object(bridge, "_download_model_worker") as worker,
        ):
            result = bridge.download_model("tiny")
        assert result == {"ok": True, "started": True, "name": "tiny"}
        worker.assert_called_once_with("tiny")
        with bridge._lock:
            assert bridge._model_download["name"] == "tiny"


def test_download_model_rejects_concurrent_download():
    bridge, _, _ = make_bridge()
    fake = mock.Mock()
    fake.list_models.return_value = _fake_states()
    with (
        mock.patch("engine.model_manager.ModelManager", return_value=fake),
        mock.patch.object(bridge, "_download_model_worker"),
    ):
        bridge.download_model("tiny")
        second = bridge.download_model("large-v3")
    assert second["ok"] is False
    assert "已有模型下载中" in second["error"]


def test_download_worker_clears_state_on_failure():
    with tempfile.TemporaryDirectory(prefix="bridge_models_") as temp:
        bridge, _, _ = make_bridge(settings={"model_dir": temp})
        fake = mock.Mock()
        fake.list_models.return_value = _fake_states()
        fake.download.side_effect = ModelManagerError("网络不可用")
        with bridge._lock:
            bridge._model_download = {
                "name": "tiny", "message": "准备模型缓存",
                "progress": 0.0, "error": "",
            }
        with mock.patch("engine.model_manager.ModelManager", return_value=fake):
            bridge._download_model_worker("tiny")
        with bridge._lock:
            assert bridge._model_download["name"] == ""
            # 失败原因必须跨过 finally 保留，否则前端轮询永远读不到。
            assert bridge._model_download["error"] == "网络不可用"
            assert bridge._model_download["progress"] is None
        fake.download.assert_called_once()
        # error 只保留到下一次下载启动；启动守卫不被残留 error 卡住。
        with mock.patch.object(bridge, "_download_model_worker"):
            restarted = bridge.download_model("tiny")
        assert restarted["ok"] is True
        with bridge._lock:
            assert bridge._model_download["error"] == ""


def test_delete_model_returns_removed_flag():
    bridge, _, _ = make_bridge()
    fake = mock.Mock()
    fake.list_models.return_value = _fake_states()
    fake.delete.return_value = True
    with mock.patch("engine.model_manager.ModelManager", return_value=fake):
        result = bridge.delete_model("tiny")
    assert result["ok"] is True
    assert result["removed"] is True
    fake.delete.assert_called_once_with("tiny")


def test_model_api_rejects_invalid_names():
    bridge, _, _ = make_bridge()
    for bad in (None, "", "  ", 42):
        assert bridge.download_model(bad)["ok"] is False
        assert bridge.delete_model(bad)["ok"] is False


def test_delete_all_models_removes_all_installed():
    bridge, _, _ = make_bridge()
    fake = mock.Mock()
    fake.list_models.return_value = _fake_states()
    fake.delete_all.return_value = 2
    with mock.patch("engine.model_manager.ModelManager", return_value=fake):
        result = bridge.delete_all_models()
    assert result["ok"] is True
    assert result["removed"] == 2
    fake.delete_all.assert_called_once_with()


def test_cache_status_and_cleanup_delegate_to_controller():
    bridge, controller, _ = make_bridge()
    controller.cache_status = mock.Mock(
        return_value={"ok": True, "cleanable_bytes": 12, "model_bytes": 34}
    )
    controller.cleanup_cache = mock.Mock(
        return_value={"ok": True, "freed_bytes": 12, "removed": ["C:/tmp/v2a_x"]}
    )
    assert bridge.cache_status() == {
        "ok": True, "cleanable_bytes": 12, "model_bytes": 34,
    }
    assert bridge.cleanup_cache() == {
        "ok": True, "freed_bytes": 12, "removed": ["C:/tmp/v2a_x"],
    }
    controller.cache_status.assert_called_once_with()
    controller.cleanup_cache.assert_called_once_with()


@pytest.mark.parametrize("channel", ["formal", "test"])
def test_check_update_parses_latest_release(channel):
    bridge, _, _ = make_bridge()
    release = {
        "tag_name": f"v{APP_VERSION}",
        "html_url": f"https://github.com/Ansel-Poiesis/FoilPage-source/releases/tag/v{APP_VERSION}",
        "prerelease": channel == "test",
    }
    payload = json.dumps([release] if channel == "test" else release).encode("utf-8")
    fake = mock.MagicMock()
    fake.read.return_value = payload
    fake.__enter__.return_value = fake
    with mock.patch("app.bridge.RELEASE_CHANNEL", channel), mock.patch("urllib.request.urlopen", return_value=fake):
        result = bridge.check_update()
    assert result["ok"] is True
    assert result["latest"] == APP_VERSION
    assert result["is_latest"] is True
    assert result["current"] == APP_VERSION


def test_check_update_network_failure_degrades_gracefully():
    bridge, _, _ = make_bridge()
    with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
        result = bridge.check_update()
    assert result["ok"] is False
    assert "error" in result


def test_data_reconcile_reports_health():
    bridge, controller, _ = make_bridge()
    controller.downloads.list.return_value = [{
        "id": "dl-1", "title": "悬空任务", "status": "awaiting_browser",
        "updated_at": "2020-01-01 00:00:00", "url": "https://example.com/v",
        "source": "browser", "progress": 0.0, "error": None, "options": {},
    }]
    result = bridge.data_reconcile()
    assert result["ok"] is True
    assert result["stale_fixed"]
    assert result["stale_fixed"][0]["id"] == "dl-1"


def test_cleanup_orphans_delegates_to_controller():
    bridge, controller, _ = make_bridge()
    result = bridge.cleanup_orphans(["C:/out/orphan1"], ["C:/dl/orphan.mp4"])
    assert result["ok"] is True
    assert controller.purged_outputs == ["C:/out/orphan1"]
    assert controller.purged_downloads == ["C:/dl/orphan.mp4"]


def test_pick_save_and_open_file_dialogs():
    window = FakeWindow()
    window.dialog_result = ["C:/tmp/backup.zip"]
    bridge, _, _ = make_bridge(window=window)
    saved = bridge.pick_save_file("FoilPage 备份.zip")
    assert saved["ok"] is True and saved["path"] == "C:/tmp/backup.zip"
    opened = bridge.pick_open_file()
    assert opened["ok"] is True and opened["path"] == "C:/tmp/backup.zip"


def test_export_backup_rejects_missing_destination_parent():
    bridge, _, _ = make_bridge()
    result = bridge.export_backup("Z:/no-such-dir/backup.zip")
    assert result["ok"] is False


class TestSystemNotify:
    def test_notify_calls_notifier(self):
        calls = []
        controller = FakeController()
        worker = FakeWorker()
        bridge = Bridge(
            controller, worker, FakeStore(),
            window_provider=lambda: None, on_quit=lambda: None,
            notifier=lambda title, message: calls.append((title, message)) or True,
        )
        result = bridge.system_notify("FoilPage：转写完成", "测试任务 已完成")
        assert result["ok"] is True
        assert calls == [("FoilPage：转写完成", "测试任务 已完成")]

    def test_notify_without_notifier(self):
        controller = FakeController()
        worker = FakeWorker()
        bridge = Bridge(
            controller, worker, FakeStore(),
            window_provider=lambda: None, on_quit=lambda: None,
        )
        result = bridge.system_notify("标题", "内容")
        assert result["ok"] is False

    def test_tray_notify_without_icon_is_safe(self):
        from pathlib import Path
        from app.tray import TrayController

        tray = TrayController(
            icon_path=Path("Z:/nonexistent-shengjian.ico"),
            show=lambda: None, quit_app=lambda: None, version="test",
        )
        assert tray.notify("t", "m") is False  # 无可用图标 → False，不抛异常


def test_retry_downloads_routes_douyin_to_page_guidance_and_others_to_local_retry(tmp_path):
    """下载行的重试必须走下载轨：抖音失败行返回页面指引（地址已失），
    其余平台走本地重试；转写前缀与 done 行不掺入。"""
    controller = FakeController()
    bili = {"id": "b1", "url": "https://www.bilibili.com/video/BV1x",
            "platform_hint": "bilibili", "status": "failed", "title": "B站",
            "error": {"code": "network_error", "retryable": True}}
    dy = {"id": "d1", "url": "https://www.douyin.com/video/123",
          "platform_hint": "douyin", "status": "failed", "title": "抖音",
          "error": {"code": "media_forbidden", "retryable": False}}
    controller.downloads.get.side_effect = lambda i: {"b1": bili, "d1": dy}.get(i)
    controller.retry_download_tasks = lambda ids: list(ids)
    store = FakeStore(tmp_path / "data")
    grant_account(controller.settings, store.base_dir)
    bridge = Bridge(controller, worker=mock.Mock(), settings_store=store,
                    window_provider=lambda: None, on_quit=lambda: None)
    res = bridge.retry_downloads(["dl:d1", "dl:b1", "job:skip"])
    assert res["ok"] is True
    assert res["retried"] == ["dl:b1"]
    assert [p["task_id"] for p in res["needs_page"]] == ["dl:d1"]
    assert res["needs_page"][0]["url"].startswith("https://www.douyin.com/")
    # job: 前缀在 _prefixed_ids 处已被滤除，不进入下载重试统计
    assert res["skipped"] == 0


def test_open_video_page_rejects_non_http_urls():
    controller = FakeController()
    controller.downloads.get.return_value = {
        "id": "x", "url": "file:///C:/windows/evil", "platform_hint": "douyin",
        "status": "failed", "title": "t",
    }
    bridge = Bridge(controller, worker=mock.Mock(), settings_store=mock.Mock(),
                    window_provider=lambda: None, on_quit=lambda: None)
    assert bridge.open_video_page("dl:x")["ok"] is False
