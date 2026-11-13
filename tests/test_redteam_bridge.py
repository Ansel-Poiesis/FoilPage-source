# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""红队测试：桥接层异常输入与边界加固（类型崩溃 / 前缀截取 / 非数值进度 / 空指针保护）。

这些用例对应红队测试发现的真实脆弱点，修复后保持回归。
"""
from __future__ import annotations

from pathlib import Path
import threading


from app.bridge import Bridge
from account_auth_fixtures import grant_account


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
        self.calls = {}

    def task_rows(self, include_archived: bool = False):
        return self.rows

    def save_settings(self, changes):
        self.settings.update(changes or {})
        return self.settings

    def cancel_jobs(self, ids):
        self.calls["cancel_jobs"] = ids

    def cancel_download_tasks(self, ids):
        self.calls["cancel_downloads"] = ids

    def requeue_jobs(self, ids):
        self.calls["requeue"] = ids
        return ids

    def retranscribe_jobs(self, ids, mode="default", editing_level=""):
        self.calls["retranscribe"] = ids
        self.calls["retranscribe_mode"] = mode
        class R:
            created = [f"job:{i}" for i in ids]
            skipped = []
        return R()

    def delete_tasks(self, ids):
        self.calls["delete"] = ids
        return ids

    def delete_download_tasks(self, ids):
        self.calls["delete_downloads"] = ids
        return ids

    def set_result_categories(self, ids, category):
        self.calls["category"] = (ids, category)
        return len(ids)


class FakeWorker:
    running = False
    pid = None


class FakeStore:
    base_dir = Path.cwd() / "fake-data"


def _row(kind, task_id, status="done", **extra):
    row = {"kind": kind, "task_id": task_id, "status": status,
           "display_title": "标题", "source": "本地转写", "progress": 1.0,
           "updated_at": "2026-08-05T12:00:00", "result_category": "未分类",
           "final_path": "", "raw_path": "", "source_path": "C:/x/video.mp4",
           "downloaded_path": "", "device": "cuda"}
    row.update(extra)
    return row


def make_bridge(rows=None, *, account_dir=None):
    ctl = FakeController(rows=rows)
    store = FakeStore()
    if account_dir is not None:
        store.base_dir = account_dir
        grant_account(ctl.settings, store.base_dir)
    bridge = Bridge(ctl, FakeWorker(), store,
                    window_provider=lambda: None, on_quit=lambda: None)
    return bridge, ctl


# ---------------------------------------------------------------- 类型崩溃
def test_cancel_tasks_ignores_non_string():
    bridge, ctl = make_bridge()
    res = bridge.cancel_tasks(["job:j1", None, 123, "dl:d2", b"job:j3"])
    assert res["ok"] is True
    assert ctl.calls["cancel_jobs"] == ["j1"]
    assert ctl.calls["cancel_downloads"] == ["d2"]


def test_requeue_tasks_ignores_non_string(tmp_path):
    bridge, ctl = make_bridge(account_dir=tmp_path / "data")
    res = bridge.requeue_tasks([None, "job:j1", 7])
    assert res["requeued"] == ["job:j1"]
    assert ctl.calls["requeue"] == ["j1"]


def test_retranscribe_tasks_ignores_non_string(tmp_path):
    bridge, ctl = make_bridge(account_dir=tmp_path / "data")
    res = bridge.retranscribe_tasks([{"x": 1}, "job:j2"])
    assert res["ok"] is True
    assert ctl.calls["retranscribe"] == ["j2"]


def test_delete_tasks_ignores_non_string():
    bridge, ctl = make_bridge()
    res = bridge.delete_tasks(["job:j1", None, "dl:d2", []])
    assert res["ok"] is True
    assert ctl.calls["delete"] == ["j1"]
    assert ctl.calls["delete_downloads"] == ["d2"]


def test_set_category_ignores_non_string():
    bridge, ctl = make_bridge()
    res = bridge.set_category([None, "job:j1", 3.14], "<img src=x onerror=1>")
    assert res["ok"] is True
    assert ctl.calls["category"] == (["j1"], "<img src=x onerror=1>")


# ---------------------------------------------------------------- 前缀截取
def test_reader_prefix_job_and_dl():
    rows = [
        _row("transcription", "j1", final_path=str(Path.cwd() / "readme.md")),
        _row("download", "d1", status="done"),
    ]
    bridge, _ = make_bridge(rows=rows)
    # job: 前缀 4 字符截取正确
    got = bridge.get_reader("job:j1", "raw")
    assert got.get("ok") is False  # raw 文件不存在，但任务被正确找到（错误是文件不可用而非任务不存在）
    assert got.get("error") == "文件不可用"
    # dl: 前缀 3 字符截取正确（任务能找到；旧实现会截错成 "1" 而报任务不存在）
    got2 = bridge.get_reader("dl:d1", "source")
    assert got2.get("ok") is False
    assert got2.get("error") == "文件不可用"


def test_reader_none_task_id():
    bridge, _ = make_bridge(rows=[_row("transcription", "j1")])
    res = bridge.get_reader(None, "final")
    assert res.get("ok") is False
    assert res.get("error") == "任务不存在"


# ---------------------------------------------------------------- 非数值进度
def test_summary_tolerates_bad_progress():
    rows = [
        _row("transcription", "a", status="queued", progress="not-a-number"),
        _row("transcription", "b", status="done", progress=None),
        _row("transcription", "c", status="running", progress=0.5),
        _row("transcription", "d", status="running", progress=2.5),  # 越界值钳制
    ]
    bridge, _ = make_bridge(rows=rows)
    summary = bridge.get_summary()
    assert summary["total"] == 4
    # queued(1) + running(2) = 3 active；done 不活跃
    assert summary["active"] == 3
    assert summary["progress"] <= 1.0
    assert summary["progress"] >= 0.0


def test_tasks_tolerates_bad_progress():
    rows = [_row("transcription", "a", status="running", progress="boom")]
    bridge, _ = make_bridge(rows=rows)
    tasks = bridge.get_tasks()
    assert len(tasks) == 1
    assert tasks[0]["progress"] == 0.0


# ---------------------------------------------------------------- 空指针保护
def test_browser_status_without_origin_policy():
    class BareApi:
        running = False
        port = 18765
        # 无 origin_policy 属性
    bridge, _ = make_bridge()
    bridge._local_api_provider = lambda: BareApi()
    st = bridge.browser_status()
    assert st["ok"] is True
    assert st["configured"] is False
    assert st["origin"] == ""


def test_browser_status_api_none():
    bridge, _ = make_bridge()
    bridge._local_api_provider = lambda: None
    st = bridge.browser_status()
    assert st["ok"] is True
    assert st["running"] is False
