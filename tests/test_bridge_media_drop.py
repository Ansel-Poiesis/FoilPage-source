# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Native drop contracts against real temporary media and queue stores."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from app.bridge import Bridge
from app.controller import AppController
from engine.media_store import ImportResult
from engine.settings import SettingsStore
from account_auth_fixtures import grant_account


class DropWindow:
    def __init__(self):
        self.allow_import = True
        self.dialog_result = None
        self.scripts = []
        self.results = []
        self.dom = SimpleNamespace(body=SimpleNamespace(on=mock.Mock()))

    def create_file_dialog(self, *_args, **_kwargs):
        return self.dialog_result

    def evaluate_js(self, script):
        self.scripts.append(script)
        if "beginNativeDrop()" in script:
            return self.allow_import
        prefix = "window.Tools?.finishNativeDrop("
        if script.startswith(prefix):
            self.results.append(json.loads(script[len(prefix):-1]))
        return None


@pytest.fixture
def instance(tmp_path):
    store = SettingsStore(base_dir=tmp_path / "app-data")
    store.update(local_api_enabled=False, device="cpu", agent_enabled=False,
                 title_optimization_enabled=False)
    grant_account(store, store.base_dir)
    controller = AppController(store)
    window = DropWindow()
    bridge = Bridge(controller, None, store, lambda: window, lambda: None)
    try:
        yield bridge, controller, window
    finally:
        bridge.close_media_preview()
        controller.close()


def media(tmp_path, name="source.wav", payload=b"RIFF-local-audio"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_importresult_registers_real_media_and_creates_a_queued_job(instance, tmp_path):
    bridge, controller, _window = instance
    path = media(tmp_path)
    original = controller.import_files
    observed = []

    def record(paths):
        entries = original(paths)
        observed.extend(entries)
        return entries

    with mock.patch.object(controller, "import_files", side_effect=record):
        result = bridge.import_files([str(path)])
    assert len(observed) == 1 and isinstance(observed[0], ImportResult)
    assert result["ok"] is True and result["imported"] == 1
    assert result["failed"] == 0 and len(result["enqueued"]) == 1
    jobs = controller.queue.list_jobs()
    assert len(jobs) == 1 and jobs[0]["status"] == "queued"
    assert jobs[0]["source_path"] == str(path.resolve()) and jobs[0]["device"] == "cpu"


def test_mixed_import_keeps_valid_file_and_returns_sanitized_partial_failures(instance, tmp_path):
    bridge, controller, _window = instance
    good = media(tmp_path)
    unsupported = media(tmp_path, "unsupported.txt")
    result = bridge.import_files([str(good), str(tmp_path / "missing.wav"), str(unsupported), None])
    assert result["ok"] is True and result["imported"] == 1
    assert len(result["enqueued"]) == 1 and result["failed"] == 3
    assert len(result["errors"]) == 3 and str(tmp_path) not in "\n".join(result["errors"])
    assert len(controller.queue.list_jobs()) == 1


def test_same_path_and_content_create_one_job_and_active_reimport_creates_none(instance, tmp_path):
    bridge, controller, _window = instance
    first = media(tmp_path, "first.wav")
    duplicate = media(tmp_path, "copy.wav", first.read_bytes())
    result = bridge.import_files([str(first), str(first), str(duplicate)])
    assert result["imported"] == 1 and len(result["enqueued"]) == 1
    assert result["skipped"] == 2 and result["failed"] == 0
    repeated = bridge.import_files([str(first), str(duplicate)])
    assert repeated["ok"] is True and repeated["enqueued"] == [] and repeated["skipped"] == 2
    assert len(controller.queue.list_jobs()) == 1 and len(controller.media.list_media()) == 1


def test_folder_dialog_imports_real_supported_files_recursively(instance, tmp_path):
    bridge, controller, window = instance
    folder = tmp_path / "selected folder"
    first = media(folder, "first.wav", b"audio-one")
    second = media(folder / "nested", "second.mp3", b"audio-two")
    media(folder, "ignored.txt", b"text")
    window.dialog_result = [str(folder)]
    result = bridge.pick_folder_and_import()
    assert result["ok"] is True and result["imported"] == 2
    assert len(result["enqueued"]) == 2 and result["failed"] == 0
    assert {job["source_path"] for job in controller.queue.list_jobs()} == {str(first), str(second)}


def test_native_drop_registers_pywebview_dom_handler_with_default_prevention(instance):
    bridge, _controller, window = instance
    from webview.dom import DOMEventHandler

    with mock.patch("webview.dom.DOMEventHandler", wraps=DOMEventHandler) as constructor:
        bridge._bind_media_drop(window)
    constructor.assert_called_once_with(bridge._on_media_drop, prevent_default=True)
    args = window.dom.body.on.call_args.args
    assert args[0] == "drop" and isinstance(args[1], DOMEventHandler)
    assert bridge._media_drop_element is window.dom.body


def test_native_drop_only_uses_full_paths_and_delivers_json_safe_result(instance, tmp_path):
    bridge, controller, window = instance
    good = media(tmp_path, "媒体 '样本'.wav")
    ignored = media(tmp_path, "other.wav", b"different-audio")
    bridge._on_media_drop({"dataTransfer": {"files": [
        {"pywebviewFullPath": str(good), "name": good.name},
        {"path": str(ignored)}, {"name": str(ignored)}, {"pywebviewFullPath": 123}, None,
    ]}})
    assert len(controller.queue.list_jobs()) == 1
    assert len(window.results) == 1 and window.results[0]["ok"] is True
    assert len(window.results[0]["enqueued"]) == 1
    assert window.scripts[0] == "window.Tools?.beginNativeDrop() === true"


def test_native_drop_outside_import_page_is_discarded_before_any_registration(instance, tmp_path):
    bridge, controller, window = instance
    window.allow_import = False
    bridge._on_media_drop({"dataTransfer": {"files": [{"pywebviewFullPath": str(media(tmp_path))}]}})
    assert controller.queue.list_jobs() == [] and controller.media.list_media() == []
    assert len(window.scripts) == 1 and window.results == []


def test_accepted_drop_finishes_queue_transaction_after_user_leaves_import_page(instance, tmp_path):
    bridge, controller, window = instance
    path = media(tmp_path)
    original = controller.import_files

    def leave_page(paths):
        entries = original(paths)
        window.allow_import = False
        return entries

    with mock.patch.object(controller, "import_files", side_effect=leave_page):
        bridge._on_media_drop({"dataTransfer": {"files": [{"pywebviewFullPath": str(path)}]}})
    assert window.allow_import is False and len(controller.queue.list_jobs()) == 1
    assert len(window.results) == 1 and window.results[0]["ok"] is True


def test_native_drop_without_full_paths_reports_recoverable_picker_action(instance):
    bridge, controller, window = instance
    bridge._on_media_drop({"dataTransfer": {"files": [{"name": "not-an-absolute-path.wav"}]}})
    assert controller.queue.list_jobs() == [] and window.results[0]["ok"] is False
    assert "选择文件" in window.results[0]["error"]
