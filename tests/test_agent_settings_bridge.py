# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Agent and desktop share real settings, CAS and model-readiness checks."""
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from app.bridge import Bridge
from app.controller import AppController
from engine.settings import SettingsStore


@pytest.fixture
def desktop(tmp_path):
    controller = AppController(SettingsStore(base_dir=tmp_path / "profile"))
    bridge = Bridge(controller, Mock(), controller.settings_store, on_quit=lambda: None, window_provider=lambda: None)
    yield bridge, controller
    controller.close()


def test_prompt_model_and_key_settings_persist_without_secret_echo(desktop):
    bridge, controller = desktop
    state = bridge.get_agent_settings()
    key = "fixture-only-nonproduction-key"
    result = bridge.save_agent_settings({"asr_initial_prompt": "医学访谈", "asr_hotwords": "FoilPage", "title_skill_prompt": "用简洁精准标题",
        "agent_model": "fixture-model", "agent_api_key": key}, state["revision"])
    assert result["ok"] and key not in json.dumps(result)
    assert result["secret_fields"]["agent_api_key"]
    actual = controller.settings_store.load()
    assert actual["agent_api_key"] == key and actual["asr_hotwords"] == "FoilPage"
    assert actual["title_skill_prompt"] == "用简洁精准标题"
    assert key not in controller.settings_store.path.read_text(encoding="utf-8")
    assert result["revision"] != state["revision"]


@pytest.mark.parametrize("key", ["account_login_token", "license_token", "credit_balance", "subscription_active", "agent_usage_used", "local_api_token", "database_path", "js_runtime_path"])
def test_agent_cannot_write_account_balance_or_connection_secrets(desktop, key):
    bridge, controller = desktop
    before = dict(controller.settings)
    result = bridge.save_agent_settings({key: "edited"}, bridge.get_agent_settings()["revision"])
    assert result["code"] == "protected_setting" and controller.settings == before


def test_two_agent_edits_and_gui_edit_obey_same_revision(desktop):
    bridge, controller = desktop
    revision = bridge.get_agent_settings()["revision"]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda value: bridge.save_agent_settings({"asr_hotwords": value}, revision), ["A", "B"]))
    assert sum(result["ok"] is True for result in results) == 1
    assert any(result.get("code") == "revision_conflict" for result in results)
    state = bridge.get_agent_settings()
    bridge.save_settings({"asr_hotwords": "界面编辑"})
    assert bridge.save_agent_settings({"asr_hotwords": "旧稿"}, state["revision"])["code"] == "revision_conflict"
    assert controller.settings_store.load()["asr_hotwords"] == "界面编辑"


def test_unavailable_model_cannot_be_selected_through_agent(desktop):
    bridge, controller = desktop
    result = bridge.save_agent_settings({"whisper_model": "large-v3", "model_dir": str(controller.settings_store.base_dir / "empty-cache")}, bridge.get_agent_settings()["revision"])
    assert result["code"] == "model_not_ready" and controller.settings["whisper_model"] == "tiny"


def test_agent_revision_matches_frontend_event_and_does_not_expose_key(desktop):
    bridge, controller = desktop
    snapshot = bridge.get_agent_settings()
    assert bridge.get_settings()["agent_settings_revision"] == snapshot["revision"]
    controller.settings["agent_api_key"] = "fixture-secret"
    assert bridge.get_agent_settings()["revision"] != snapshot["revision"]


def test_agent_provider_change_cannot_move_existing_write_only_key(desktop):
    bridge, controller = desktop
    bridge.save_agent_settings({"agent_api_key": "fixture-old-key"}, bridge.get_agent_settings()["revision"])
    changed = bridge.save_agent_settings({"agent_base_url": "https://fixture.invalid/v1"}, bridge.get_agent_settings()["revision"])
    assert changed["ok"] and not changed["secret_fields"]["agent_api_key"]
    assert controller.settings_store.load()["agent_api_key"] == ""
    replaced = bridge.save_agent_settings({"agent_provider": "openai", "agent_api_key": "fixture-new-key"}, bridge.get_agent_settings()["revision"])
    assert replaced["ok"] and controller.settings_store.load()["agent_api_key"] == "fixture-new-key"
