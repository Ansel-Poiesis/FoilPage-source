# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent_service import apply_agent_translation  # noqa: E402
from engine.agent_client import AgentError, rewrite_transcript  # noqa: E402


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _settings(profile="auto") -> dict:
    return {
        "agent_api_key": "test-key-not-a-real-secret",
        "agent_profile": profile,
        "agent_base_url": "https://api.deepseek.com",
        "agent_model": "deepseek-chat",
        "agent_temperature": 0.3,
        "agent_timeout_seconds": 10,
    }


def test_agent_auto_classifies_then_returns_markdown_without_leaking_key() -> None:
    responses = iter([
        {"choices": [{"message": {"content": '{"profile":"consulting"}'}}]},
        {"choices": [{"message": {"content": "<!-- FoilPage 标签: 方案, 客户需求 -->\n# 建议\n\n正文"}}]},
    ])
    with mock.patch("engine.agent_client.urlopen", side_effect=lambda *_args, **_kwargs: FakeResponse(next(responses))) as request:
        result = rewrite_transcript("客户需要一个可以执行的方案", _settings(), title="咨询素材")
    assert result.profile == "consulting"
    assert result.markdown == "# 建议\n\n正文\n"
    assert result.title == "建议"
    assert result.tags == ("方案", "客户需求")
    assert result.usage_units == 1
    assert result.input_characters == len("客户需要一个可以执行的方案")
    assert result.request_count == 2
    assert request.call_count == 2
    assert "test-key-not-a-real-secret" not in str(result)


def test_agent_requires_key_without_sending_request() -> None:
    with mock.patch("engine.agent_client.urlopen") as request:
        try:
            rewrite_transcript("内容", {"agent_profile": "knowledge"})
        except AgentError as exc:
            assert "API Key" in str(exc)
        else:
            raise AssertionError("缺少 key 必须失败")
    request.assert_not_called()


def test_agent_service_preserves_raw_and_writes_article_manifest() -> None:
    with tempfile.TemporaryDirectory(prefix="agent_service_") as temp:
        root = Path(temp)
        raw = root / "原视频_raw.md"
        raw.write_text("原始转写", encoding="utf-8")
        manifest = {
            "job_id": "job-1", "source_path": "source.mp4", "source_sha256": None,
            "backend": "local", "model": "tiny", "language": "zh", "duration_sec": 1.0,
            "raw_path": str(raw), "final_path": str(root / "原视频.md"),
            "content_mode": "raw", "prompt_version": "v1", "created_at": "2026-01-01T00:00:00",
        }
        with mock.patch("app.agent_service.rewrite_transcript", return_value=mock.Mock(profile="knowledge", markdown="# 文章\n", model="deepseek-chat", tags=("复盘", "访谈"))):
            updated = apply_agent_translation(manifest, _settings("knowledge"), source_title="原视频")
        assert Path(updated["raw_path"]).read_text(encoding="utf-8") == "原始转写"
        assert Path(updated["final_path"]).read_text(encoding="utf-8") == "# 原视频\n"
        assert updated["source_title"] == "原视频", "正文整理不得隐式优化标题"
        saved = json.loads((root / "原视频_manifest.json").read_text(encoding="utf-8"))
        assert saved["agent_profile"] == "knowledge"
        assert saved["agent_tags"] == ["复盘", "访谈"]


def test_agent_tag_marker_is_optional_and_never_leaks_into_markdown() -> None:
    responses = iter([{"choices": [{"message": {"content": "# 无标签\n\n正文"}}]}])
    with mock.patch("engine.agent_client.urlopen", side_effect=lambda *_args, **_kwargs: FakeResponse(next(responses))):
        result = rewrite_transcript("内容", _settings("knowledge"))
    assert result.tags == ()
    assert result.markdown == "# 无标签\n\n正文\n"


def test_hosted_quota_exceeded_keeps_server_code_and_message() -> None:
    """积分用完（402/quota_exceeded）必须原样到达用户：code 保留、充值文案透传。

    先生 2026-10-14：积分耗尽的弹窗提醒依赖这条语义链——此前 402 的响应
    体被丢弃，用户只看到"HTTP 402"。
    """
    import io
    from urllib.error import HTTPError
    from engine.agent_client import AgentStage

    body = json.dumps({"error": {"code": "quota_exceeded", "message": "积分不足，请充值后继续"}}).encode("utf-8")
    error = HTTPError("https://pay.example/v1/agent/stage/begin", 402, "Payment Required",
                      None, io.BytesIO(body))

    def hosted_settings() -> dict:
        return {**_settings(),
                "agent_billing_mode": "hosted",
                "subscription_server_url": "https://pay.example",
                "account_login_token": "t" * 20,
                "_agent_device_id": "d" * 16,
                "_agent_operation_id": "o" * 12}

    with mock.patch("engine.agent_client.urlopen", side_effect=error):
        try:
            with AgentStage(hosted_settings(), "body", "内容", "o" * 12):
                pass
        except AgentError as exc:
            assert exc.code == "quota_exceeded"
            assert "积分不足" in str(exc)
            assert "HTTP 402" not in str(exc)
        else:
            raise AssertionError("配额耗尽必须抛 AgentError")

    other = HTTPError("https://pay.example/v1/agent/stage/begin", 500, "Server Error",
                      None, io.BytesIO(json.dumps({"error": {"code": "relay_error", "message": "上游超时"}}).encode()))
    with mock.patch("engine.agent_client.urlopen", side_effect=other):
        try:
            with AgentStage(hosted_settings(), "body", "内容", "o" * 12):
                pass
        except AgentError as exc:
            assert exc.code == "relay_error"
            assert "HTTP 500" in str(exc) and "上游超时" in str(exc)
        else:
            raise AssertionError("服务端错误必须抛 AgentError")
