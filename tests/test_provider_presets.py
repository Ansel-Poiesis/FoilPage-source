# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""BYOK 服务商预设库合同（先生 2026-11-16）。

单一真源在 engine/settings.py 的 AGENT_PROVIDER_PRESETS；UI（settings.js
PROVIDER_PRESETS）必须与之逐项一致——本文件用与版本标记检查同款的静态
锁定，防止两份清单漂移。另覆盖：预设切换清空旧 Key 的桥接规则、视觉能
力查表、托管模型白名单放宽后的形态校验。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine.settings import AGENT_PROVIDER_PRESETS, SettingsStore, SettingsValidationError

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_PROVIDERS = {
    "mimo", "deepseek", "openai", "bailian", "zhipu", "kimi",
    "siliconflow", "openrouter", "ark", "custom",
}


def test_preset_table_covers_the_ten_providers() -> None:
    assert set(AGENT_PROVIDER_PRESETS) == EXPECTED_PROVIDERS
    for pid, preset in AGENT_PROVIDER_PRESETS.items():
        assert set(preset) == {"label", "base_url", "model", "vision"}, pid
        assert str(preset["label"]).strip(), pid
        if pid != "custom":
            assert str(preset["base_url"]).startswith("https://"), pid
            assert str(preset["model"]), pid


def test_ui_provider_list_matches_the_source_of_truth() -> None:
    source = (ROOT / "app/webview_ui/js/settings.js").read_text(encoding="utf-8")
    match = re.search(r"const PROVIDER_PRESETS = \{(.*?)\n  \};", source, re.S)
    assert match, "settings.js 未找到 PROVIDER_PRESETS"
    js_ids = set(re.findall(r"^\s{4}([a-z]+):\s*\{", match.group(1), re.M))
    assert js_ids == set(AGENT_PROVIDER_PRESETS), (js_ids, set(AGENT_PROVIDER_PRESETS))
    js_bases = dict(re.findall(r"^\s{4}([a-z]+):\s*\{[^}]*?base: \"([^\"]*)\"", match.group(1), re.M))
    for pid, preset in AGENT_PROVIDER_PRESETS.items():
        assert js_bases.get(pid) == preset["base_url"], pid
    js_models = dict(re.findall(r"^\s{4}([a-z]+):\s*\{[^}]*?model: \"([^\"]*)\"", match.group(1), re.M))
    for pid, preset in AGENT_PROVIDER_PRESETS.items():
        assert js_models.get(pid) == preset["model"], pid


def test_provider_validation_accepts_every_preset() -> None:
    defaults = SettingsStore().defaults()
    for pid in AGENT_PROVIDER_PRESETS:
        SettingsStore().validate({**defaults, "agent_provider": pid})
    with pytest.raises(SettingsValidationError):
        SettingsStore().validate({**defaults, "agent_provider": "not-a-provider"})


def test_hosted_model_accepts_catalog_shaped_ids_only() -> None:
    defaults = SettingsStore().defaults()
    for model in ("", "qwen-plus", "deepseek-flash", "qwen-turbo", "qwen-max", "some/new-model.x"):
        SettingsStore().validate({**defaults, "agent_hosted_model": model})
    for bad in ("Qwen-Plus", "带中文", "x" * 65, "a b"):
        with pytest.raises(SettingsValidationError):
            SettingsStore().validate({**defaults, "agent_hosted_model": bad})


def test_vision_capability_follows_the_preset_table() -> None:
    from engine.agent_client import _vision_capable

    base = {"agent_billing_mode": "byok", "agent_vision_model": ""}
    assert _vision_capable({**base, "agent_provider": "mimo"}) is True
    assert _vision_capable({**base, "agent_provider": "openai"}) is True
    assert _vision_capable({**base, "agent_provider": "openrouter"}) is True
    assert _vision_capable({**base, "agent_provider": "deepseek"}) is False
    assert _vision_capable({**base, "agent_provider": "bailian"}) is False
    # custom（vision=None）保留旧启发式：mimo 船队判定。
    assert _vision_capable({**base, "agent_provider": "custom", "agent_model": "mimo-v2.6-flash"}) is True
    assert _vision_capable({**base, "agent_provider": "custom", "agent_model": "other"}) is False
    # 显式配置的视觉模型始终可信。
    assert _vision_capable({**base, "agent_provider": "deepseek", "agent_vision_model": "qwen-vl-max"}) is True


def test_save_settings_clears_stale_agent_key_on_provider_switch(tmp_path, monkeypatch) -> None:
    """服务商/接口地址变化而未同时提交新 Key → 服务端清空旧 Key 并提示。"""
    from app.bridge import Bridge

    store = SettingsStore(base_dir=tmp_path / "data")
    store.update(agent_provider="mimo",
                 agent_base_url="https://api.xiaomimimo.com/v1",
                 agent_api_key="sk-old")
    bridge = Bridge.__new__(Bridge)
    bridge._lock = __import__("threading").RLock()
    bridge.controller = type("C", (), {
        "settings": store.load(),
        "save_settings": lambda self, changes: store.update(**changes),
    })()
    bridge._clear_account = lambda: None
    bridge._account_revision = 0
    bridge._sync_native_theme = lambda *_: None
    bridge._frontend_settings_view = lambda: {"ok": True}
    bridge.controller.agent_activity = None

    view = bridge.save_settings({"agent_provider": "deepseek", "agent_base_url": "https://api.deepseek.com"})
    assert view.get("agent_key_cleared") is True
    assert store.load().get("agent_api_key") == ""

    # 同批带新 Key：不清空。
    store.update(agent_api_key="sk-2")
    view = bridge.save_settings({"agent_provider": "kimi", "agent_base_url": "https://api.moonshot.cn/v1", "agent_api_key": "sk-3"})
    assert not view.get("agent_key_cleared")
    assert store.load().get("agent_api_key") == "sk-3"

    # 服务商与地址都没变：不动 Key。
    view = bridge.save_settings({"agent_model": "kimi-k2-0905-preview"})
    assert not view.get("agent_key_cleared")
    assert store.load().get("agent_api_key") == "sk-3"
