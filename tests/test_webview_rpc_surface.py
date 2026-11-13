# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""The installed pywebview exposes only the frontend's explicit RPC contract."""
from __future__ import annotations

import inspect
import json
import re
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from webview.util import inject_pywebview, js_bridge_call
from webview.window import Window

from account_auth_fixtures import grant_account
from app.bridge import Bridge
from app.controller import AppController
from app.webview_api import WEBVIEW_API_METHODS, expose_bridge
from engine.settings import SettingsStore


@pytest.fixture
def rpc_surface(tmp_path):
    settings = SettingsStore(base_dir=tmp_path / "data")
    grant_account(settings, settings.base_dir)
    controller = AppController(settings)
    worker = SimpleNamespace(running=False, start=mock.Mock(), stop=mock.Mock())
    window = Window("isolated-rpc", "FoilPage", url=None, js_api=None)
    window.js_api_endpoint = ""
    window.scripts = []
    window.responses = []
    window.replied = threading.Event()
    window.run_js = window.scripts.append

    def evaluate_js(script):
        window.responses.append(script)
        window.replied.set()

    window.evaluate_js = evaluate_js
    bridge = Bridge(controller, worker, settings, window_provider=lambda: window, on_quit=lambda: None)
    expose_bridge(window, bridge)
    try:
        yield window, bridge, controller, settings, worker
    finally:
        controller.close()


def dispatch(window, name, args):
    window.replied.clear()
    js_bridge_call(window, name, args, "fixture-call")
    # This contract includes real Windows library/file and exception I/O; it is
    # not a cold-start performance benchmark. Keep a finite RPC completion bound.
    assert window.replied.wait(15), name
    result = re.search(r"\(\{(isError: true, )?value: '(.*)'\}\)$", window.responses[-1])
    assert result is not None, window.responses[-1]
    value = json.loads(result.group(2).replace("\\'", "'").replace("\\\\", "\\"))
    return bool(result.group(1)), value


def test_allowlist_matches_all_existing_frontend_calls_and_excludes_native_helpers():
    api = (Path(__file__).resolve().parents[1] / "app/webview_ui/js/api.js").read_text(encoding="utf-8")
    methods = set(re.findall(r'call\("([a-z_]+)"', api))
    assert methods == WEBVIEW_API_METHODS
    assert "push_state" not in methods and "sync_subscription_in_background" not in methods
    assert "get_notification_tasks" not in methods and "_work_entry_error" not in methods


def test_actual_pywebview_injection_has_only_top_level_names_and_unshifted_arguments(rpc_surface):
    window, bridge, *_ = rpc_surface
    inject_pywebview("edgechromium", window)
    assert window.events.loaded.wait(3)
    finish = next(script for script in window.scripts if "_createApi(JSON.parse(" in script)
    encoded = re.search(r"_createApi\(JSON.parse\('(.*?)'\)\)", finish)
    assert encoded is not None
    methods = json.loads(encoded.group(1))
    assert {item["func"] for item in methods} == WEBVIEW_API_METHODS
    assert not any("." in item["func"] or item["func"].startswith("_") for item in methods)
    for item in methods:
        name = item["func"]
        exposed = window._functions[name]
        assert inspect.isfunction(exposed) and not inspect.ismethod(exposed)
        signature = inspect.signature(getattr(bridge, name))
        assert inspect.signature(exposed) == signature
        assert item["params"] == list(signature.parameters)
        assert "self" not in item["params"]
    assert next(item["params"] for item in methods if item["func"] == "download_from_url") == ["url", "title", "transcribe"]
    assert next(item["params"] for item in methods if item["func"] == "email_login_verify") == ["email", "code", "intent"]
    assert window._js_api is None


@pytest.mark.parametrize("name", [
    "controller.enqueue_media", "controller.import_files", "controller.save_settings",
    "controller.queue.enqueue", "controller.settings_store.save",
    "settings_store.save", "settings_store.update", "worker.start",
    "_work_entry_error", "_clear_account", "push_state", "sync_subscription_in_background",
    "import_files.__wrapped__", "not_a_bridge_method",
])
def test_raw_nested_private_and_unknown_dispatch_cannot_reach_bridge_objects(rpc_surface, name, caplog):
    window, bridge, controller, settings, worker = rpc_surface
    before = settings.load()
    with mock.patch.object(controller, "enqueue_media", wraps=controller.enqueue_media) as enqueue, \
         mock.patch.object(settings, "save", wraps=settings.save) as save:
        js_bridge_call(window, name, [["untrusted"]], "unknown-call")
        enqueue.assert_not_called()
        save.assert_not_called()
        worker.start.assert_not_called()
    assert window.responses == []
    assert f"Function {name}() does not exist" in caplog.text
    assert settings.load() == before and controller.queue.list_jobs() == []
    assert window._js_api is None and name not in window._functions


def test_actual_rpc_import_and_settings_preserve_real_parameters_and_results(rpc_surface, tmp_path):
    window, bridge, controller, settings, _ = rpc_surface
    source = tmp_path / "原始音频.wav"
    # A real PCM fixture keeps media decoding separate from this RPC contract.
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 160)
    import av
    with av.open(str(source)) as decoded:
        assert decoded.duration == 10000
    is_error, result = dispatch(window, "import_files", [[str(source)]])
    assert not is_error and result["ok"] is True
    assert result["imported"] == 1 and len(result["enqueued"]) == 1
    job = controller.queue.get_job(result["enqueued"][0])
    assert job["source_path"] == str(source.resolve())
    is_error, result = dispatch(window, "save_settings", [{"theme": "paper", "agent_model": "模型'甲"}])
    assert not is_error and result["theme"] == "paper" and result["agent_model"] == "模型'甲"
    assert settings.load()["agent_model"] == "模型'甲"
    assert result["account_access"]["allowed"] is True


def test_actual_rpc_keeps_business_gate_and_validation_exception(rpc_surface, tmp_path):
    window, bridge, controller, settings, _ = rpc_surface
    controller.save_settings({"account_login_token": ""})
    is_error, result = dispatch(window, "import_files", [[str(tmp_path / "never-imported.wav")]])
    assert not is_error and result == {"ok": False, "code": "login_required", "error": "请先登录 FoilPage，登录后继续"}
    assert controller.queue.list_jobs() == [] and controller.media.list_media() == []
    before = settings.load()
    is_error, error = dispatch(window, "save_settings", [{"font_scale": "invalid-fixture-scale"}])
    assert is_error and error["name"] == "SettingsValidationError"
    assert error["message"] and "stack" in error
    assert settings.load() == before


def test_actual_rpc_preserves_omitted_defaults_and_explicit_positional_arguments(rpc_surface):
    window, bridge, controller, *_ = rpc_surface
    submit = mock.Mock(return_value={"id": "fixture-download", "status": "received"})
    bridge._local_api_provider = lambda: SimpleNamespace(running=True, coordinator=SimpleNamespace(submit=submit))
    url = "https://www.bilibili.com/video/BV1fixture"
    is_error, result = dispatch(window, "download_from_url", [url])
    assert not is_error and result == {"ok": True, "job": {"id": "fixture-download", "status": "received"}}
    submit.assert_called_once_with(url, title="", options={"transcribe_after_download": True}, mode="local")
    submit.reset_mock()
    is_error, result = dispatch(window, "download_from_url", [url, "原始标题", False])
    assert not is_error and result["ok"] is True
    submit.assert_called_once_with(url, title="原始标题", options={"transcribe_after_download": False}, mode="local")
    before = dict(controller.settings)
    is_error, error = dispatch(window, "save_settings", [])
    assert is_error and error["name"] == "TypeError"
    assert controller.settings == before


def test_expose_rejects_any_existing_recursive_api_object(rpc_surface):
    window, bridge, *_ = rpc_surface
    window._js_api = bridge
    with pytest.raises(ValueError, match="js_api=None"):
        expose_bridge(window, bridge)


def test_actual_desktop_assembly_never_attaches_bridge_as_recursive_js_api(tmp_path):
    from app import desktop_web

    settings = SettingsStore(base_dir=tmp_path / "data")
    settings.update(local_api_enabled=False, worker_auto_start=False)
    created = []

    def create_window(title, **kwargs):
        assert kwargs["js_api"] is None
        window = Window("desktop-fixture", title, **kwargs)
        created.append(window)
        return window

    with mock.patch.object(desktop_web.webview, "create_window", side_effect=create_window), \
         mock.patch.object(desktop_web, "register_identity", return_value=False), \
         mock.patch.object(desktop_web.TrayController, "start", return_value=False):
        app = desktop_web.WebDesktopApp(data_dir=str(settings.base_dir), start_worker=False)
    try:
        assert app._window is created[0]
        assert app._window._js_api is None
        assert set(app._window._functions) == WEBVIEW_API_METHODS
        assert not any("." in name or name.startswith("_") for name in app._window._functions)
        assert app.bridge.controller is app.controller and app.bridge.settings_store is app.settings_store
    finally:
        app._close()
