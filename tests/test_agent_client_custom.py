# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import json
from unittest import mock

import pytest

from engine.agent_client import AgentError, rewrite_transcript


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _settings(**overrides) -> dict:
    values = {
        "agent_api_key": "test-key-not-a-real-secret",
        "agent_profile": "custom:movie",
        "agent_base_url": "https://api.deepseek.com",
        "agent_model": "deepseek-chat",
        "agent_temperature": 0.6,
        "agent_timeout_seconds": 10,
        "agent_custom_profiles": [
            {
                "id": "movie",
                "name": "电影解说",
                "system_prompt": "你是电影解说员，只写分镜式解说。",
                "enabled": True,
                "model": "deepseek-reasoner",
                "temperature": 0.1,
            }
        ],
    }
    values.update(overrides)
    return values


def _article_response() -> dict:
    return {"choices": [{"message": {"content": "# 解说的标题\n\n正文"}}]}


def test_custom_profile_uses_own_prompt_model_and_temperature() -> None:
    captured: dict = {}

    def fake_urlopen(request, *_args, **_kwargs):
        payload = json.loads(request.data.decode("utf-8"))
        captured["model"] = payload["model"]
        captured["temperature"] = payload["temperature"]
        captured["system"] = payload["messages"][0]["content"]
        return FakeResponse(_article_response())

    with mock.patch("engine.agent_client.urlopen", side_effect=fake_urlopen):
        result = rewrite_transcript("一段原始转写", _settings())

    assert captured["model"] == "deepseek-reasoner"
    assert captured["temperature"] == 0.1
    assert captured["system"].startswith("你是电影解说员，只写分镜式解说。")
    assert "<!-- FoilPage 标签:" in captured["system"]
    assert result.profile == "custom:movie"
    assert result.title == "解说的标题"
    assert result.request_count == 1
    assert result.usage_units == 3  # reasoner 按成本倍率折算


def test_custom_profile_without_overrides_follows_global_settings() -> None:
    settings = _settings()
    settings["agent_custom_profiles"] = [
        {"id": "movie", "name": "电影解说", "system_prompt": "只写解说。", "enabled": True, "model": "", "temperature": None}
    ]
    captured: dict = {}

    def fake_urlopen(request, *_args, **_kwargs):
        payload = json.loads(request.data.decode("utf-8"))
        captured["model"] = payload["model"]
        captured["temperature"] = payload["temperature"]
        return FakeResponse(_article_response())

    with mock.patch("engine.agent_client.urlopen", side_effect=fake_urlopen):
        rewrite_transcript("一段原始转写", settings)

    assert captured["model"] == "deepseek-chat"
    assert captured["temperature"] == 0.6


def test_missing_custom_profile_raises_without_request() -> None:
    settings = _settings(agent_profile="custom:missing")
    with mock.patch("engine.agent_client.urlopen") as request:
        with pytest.raises(AgentError, match="自定义 Agent 不存在或未启用"):
            rewrite_transcript("内容", settings)
    request.assert_not_called()


def test_disabled_custom_profile_raises() -> None:
    settings = _settings()
    settings["agent_custom_profiles"][0]["enabled"] = False
    with mock.patch("engine.agent_client.urlopen") as request:
        with pytest.raises(AgentError, match="自定义 Agent 不存在或未启用"):
            rewrite_transcript("内容", settings)
    request.assert_not_called()
