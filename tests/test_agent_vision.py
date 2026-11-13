# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import json
from unittest import mock

from engine.agent_client import rewrite_transcript


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_mimo_vision_notes_are_given_to_fidelity_writer() -> None:
    responses = iter([
        {"choices": [{"message": {"content": '{"corrections":[{"frame":1,"visible_text":"米莫","use_when":"纠正同音字"}]}'}}]},
        {"choices": [{"message": {"content": "<!-- FoilPage 标签: 校订 -->\n# 原文\n\n米莫的原话"}}]},
    ])
    captured: list[dict] = []

    def fake_urlopen(request, *_args, **_kwargs):
        captured.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(next(responses))

    frames = [{"timestamp": 1.0, "data_url": "data:image/jpeg;base64,ZmFrZQ=="}]
    settings = {
        "agent_api_key": "mimo-test-key",
        "agent_provider": "mimo",
        "agent_base_url": "https://api.xiaomimimo.com/v1",
        "agent_model": "mimo-v2.5",
        "agent_vision_model": "mimo-v2.5",
        "agent_vision_enabled": True,
        "agent_vision_frame_count": 1,
        "agent_profile": "knowledge",
        "agent_temperature": 0.2,
        "agent_timeout_seconds": 10,
    }
    with mock.patch("engine.agent_client.extract_frame_data_urls", return_value=frames), mock.patch(
        "engine.agent_client.urlopen", side_effect=fake_urlopen
    ):
        result = rewrite_transcript(
            "米墨的原话",
            settings,
            source_path="sample.mp4",
            segments=[{"start": 0.5, "end": 1.5, "text": "米墨的原话"}],
            duration_sec=2.0,
        )

    assert result.vision_used is True
    assert result.vision_frames == 1
    assert len(captured) == 2
    vision_content = captured[0]["messages"][1]["content"]
    assert any(item.get("type") == "image_url" for item in vision_content)
    assert "视觉模型" in captured[1]["messages"][1]["content"]



def test_explicit_vision_model_enables_vision_with_any_provider():
    """先生 2026-11-02：显式配置视觉模型即启用（DS 文本 + 专用 VL 的组合），
    不再按 MiMo 白名单猜。"""
    from engine.agent_client import _vision_capable

    assert _vision_capable({
        "agent_billing_mode": "byok", "agent_provider": "deepseek",
        "agent_model": "deepseek-v4-flash", "agent_vision_model": "glm-4v-plus",
    }) is True
    assert _vision_capable({
        "agent_billing_mode": "hosted", "agent_vision_model": "glm-4v-plus",
    }) is False, "托管合同仍为纯文本"
    assert _vision_capable({
        "agent_billing_mode": "byok", "agent_provider": "deepseek",
        "agent_model": "deepseek-v4-flash", "agent_vision_model": "",
    }) is False, "未配置视觉模型且主模型非 MiMo 时不启用"


def test_missing_vision_model_skips_review_without_model_guess():
    """主模型与视觉模型都为空时直接跳过，不再回退死名 mimo-v2.5。"""
    from engine.agent_client import _review_video_captions

    notes, frames = _review_video_captions(
        {"agent_vision_model": "", "agent_model": ""}, "key", "unused.mp4", [], 10.0, None)
    assert notes == [] and frames == 0
