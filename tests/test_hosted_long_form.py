# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""托管长文分段：单次转发有 85 秒死线，长转写拆成顺序调用，且不越过服务端给定的转发名额。"""
from unittest import mock

import pytest

from engine import agent_client
from engine.agent_client import (
    HOSTED_BODY_CALL_LIMIT, HOSTED_CHUNK_CHARS, AgentError, AgentStage, _hosted_long_plan,
    _merge_hosted_parts, _split_hosted_text, agent_failure_metadata, rewrite_transcript,
)


def _settings(**extra):
    base = {"agent_billing_mode": "hosted", "agent_hosted_model": "qwen-plus",
            "subscription_server_url": "https://pay.example.com", "_agent_device_id": "device-x",
            "agent_model": "qwen-plus", "account_login_token": "token",
            "agent_profile": "auto", "agent_editing_level": "auto", "agent_vision_enabled": False}
    base.update(extra)
    return base


class _FakeStage(AgentStage):
    """托管阶段桩：记录每次转发调用，按序返回预设文本。

    call_limit=None 模拟不带 call_limit 字段的旧服务端（按 3 次计）。
    """

    def __init__(self, settings, replies=(), call_limit=None):
        self.replies = list(replies)
        self.chats = []
        self.call_limit_announced = call_limit
        super().__init__(settings, "body", "转写", "op-long")

    def _post(self, action, payload):
        if action == "begin":
            result = {"ok": True, "request_id": "r1", "receipt": "k1", "status": "pending",
                      "units": 1, "charge": "credits", "model": self.settings["agent_hosted_model"]}
            if self.call_limit_announced is not None:
                result["call_limit"] = self.call_limit_announced
            return result
        if action == "chat":
            self.chats.append(payload)
            return {"ok": True, "reply": self.replies.pop(0)}
        return {"ok": True}


def _transcript(chars):
    """带定位行的中文长转写，正文不含阿拉伯数字，避免触发编造数字判定。"""
    sentence = "这一段话用于测试长篇内容的分段整理，内容本身没有数字，也不会被判定为编造。"
    rows, total, index = [], 0, 0
    while total < chars:
        text = (sentence * 4)[:118]
        rows.append({"start": index * 20.0, "end": index * 20.0 + 19.0, "text": text})
        total += len(text)
        index += 1
    return rows


def _replies(count, per_chunk_chars, level_names=("开头部分", "中间部分", "续篇部分", "收尾部分")):
    """每段回复足够长，保真底线不会触发降级；后续各段故意重复标签行与一级标题。"""
    body = "整理后的书面正文，保留原来的观点与限定条件，只做通顺化处理。"
    replies = []
    for index in range(count):
        lead = ("<!-- FoilPage 标签: 长文, 托管 -->\n# 长文标题\n\n" if index == 0
                else "<!-- FoilPage 标签: 重复标签 -->\n# 重复标题\n\n")
        tail = "\n\n## 存疑\n\n- 「？」原词 [0:01-0:09]（推测：某词）" if index < 2 else ""
        replies.append(f"{lead}## {level_names[index]}\n\n{(body * 200)[:per_chunk_chars]}{tail}")
    return replies


def test_split_is_lossless_and_within_limit():
    rows = _transcript(HOSTED_CHUNK_CHARS * 2 + 300)
    timestamped = agent_client._timestamped_source("".join(r["text"] for r in rows), rows)
    chunks = _split_hosted_text(timestamped)
    assert 2 <= len(chunks) <= HOSTED_BODY_CALL_LIMIT
    assert all(len(chunk) <= HOSTED_CHUNK_CHARS for chunk in chunks)
    assert "\n".join(chunks) == timestamped
    wall = "没有任何标点的长转写内容" * 1500  # 15000 字单块：按固定窗口硬切
    pieces = _split_hosted_text(wall)
    assert max(map(len, pieces)) == HOSTED_CHUNK_CHARS and "".join(pieces) == wall


def test_stage_reads_the_call_limit_announced_by_the_server():
    with _FakeStage(_settings(), call_limit=5) as stage:
        assert stage.call_limit == 5
    with _FakeStage(_settings()) as stage:  # 旧服务端不带该字段
        assert stage.call_limit == 0
    with _FakeStage(_settings(), call_limit="many") as stage:
        assert stage.call_limit == 0


def test_plan_ranks_body_chunks_above_classification_and_vision():
    assert _hosted_long_plan("字" * HOSTED_CHUNK_CHARS, 3, True, True) is None
    long_text = "字" * (HOSTED_CHUNK_CHARS * 2 + 1)
    # 旧服务端的 3 个名额：先保正文，再让视觉，最后让分类。
    two = _hosted_long_plan("字" * (HOSTED_CHUNK_CHARS + 1), 3, True, True)
    assert len(two["chunks"]) == 2 and two["classify"] is True and two["vision"] is False
    three = _hosted_long_plan(long_text, 3, True, True)
    assert len(three["chunks"]) == 3 and three["classify"] is False and three["vision"] is False
    # 新服务端按每 6000 字一段给名额：3 段正文 + 分类 + 视觉 = 5，全部保留。
    roomy = _hosted_long_plan(long_text, 5, True, True)
    assert len(roomy["chunks"]) == 3 and roomy["classify"] is True and roomy["vision"] is True
    tighter = _hosted_long_plan(long_text, 4, True, True)
    assert tighter["classify"] is True and tighter["vision"] is False
    only_body = _hosted_long_plan(long_text, 4, False, True)
    assert only_body["classify"] is False and only_body["vision"] is True
    with pytest.raises(AgentError) as caught:
        _hosted_long_plan(long_text, 2, True, True)
    assert caught.value.code == "hosted_input_too_long"
    assert agent_failure_metadata(caught.value, "body") == {
        "body_error_code": "hosted_input_too_long",
        "body_error": "转写过长，托管单次整理不支持，请改用自带密钥或分段整理"}


def test_merge_keeps_one_tag_line_title_and_single_doubt_list():
    merged = _merge_hosted_parts([
        "<!-- FoilPage 标签: 长文, 整理 -->\n# 首段标题\n\n首段正文。\n\n## 存疑\n\n- 「？」原词 [0:01-0:09]（推测：某词）",
        "<!-- FoilPage 标签: 重复标签 -->\n# 重复标题\n\n次段正文。\n\n## 存疑\n\n- 「？」次词 [1:00-1:09]（推测：另词）",
        "末段正文。",
    ])
    assert merged.count("<!--") == 1 and "长文, 整理" in merged
    assert merged.count("\n# ") == 1 and "重复标题" not in merged
    assert merged.index("首段正文") < merged.index("次段正文") < merged.index("末段正文")
    assert merged.count("## 存疑") == 1 and "原词" in merged and "次词" in merged
    assert merged.rstrip().endswith("（推测：另词）")


def test_on_an_old_server_every_call_goes_to_the_body():
    rows = _transcript(HOSTED_CHUNK_CHARS * 2 + 2000)
    raw = "".join(r["text"] for r in rows)
    replies = _replies(3, 4000)
    settings = _settings(agent_hosted_model="deepseek-flash", agent_vision_enabled=True)
    with mock.patch("engine.agent_client._review_video_captions") as vision, _FakeStage(settings, replies) as stage:
        result = rewrite_transcript(raw, stage.settings, title="素材标题", source_path="source.mp4",
                                    segments=rows, duration_sec=600.0)
    assert len(stage.chats) == HOSTED_BODY_CALL_LIMIT == stage.request_count == result.request_count
    assert [call["call_id"] for call in stage.chats] == ["1", "2", "3"]
    assert all(not call["json_mode"] for call in stage.chats)  # 分类调用让位给正文
    vision.assert_not_called()
    assert "第 1/3 部分" in stage.chats[0]["messages"][1]["content"]
    assert "不要重复标签行和一级标题" in stage.chats[1]["messages"][1]["content"]
    assert result.profile == "knowledge" and result.editing_level == "balanced"
    assert result.title == "长文标题" and result.tags == ("长文", "托管") and not result.fidelity_fallback
    assert result.markdown.startswith("# 长文标题\n") and "重复标题" not in result.markdown
    body = result.markdown
    assert body.index("开头部分") < body.index("中间部分") < body.index("续篇部分")
    assert body.count("## 存疑") == 1


def test_with_roomy_server_long_form_keeps_classification_and_vision():
    rows = _transcript(HOSTED_CHUNK_CHARS * 2 + 2000)
    raw = "".join(r["text"] for r in rows)
    replies = ['{"profile":"consulting","level":"structured"}', *_replies(3, 4000)]
    settings = _settings(agent_hosted_model="deepseek-flash", agent_vision_enabled=True)
    with mock.patch("engine.agent_client._review_video_captions",
                    return_value=([{"word": "原词", "note": "画面写作圆词"}], 1)) as vision, \
            _FakeStage(settings, replies, call_limit=5) as stage:
        result = rewrite_transcript(raw, stage.settings, title="素材标题", source_path="source.mp4",
                                    segments=rows, duration_sec=600.0)
    vision.assert_called_once()
    assert [call["json_mode"] for call in stage.chats] == [True, False, False, False]
    assert result.request_count == 5 and result.vision_used is True and not result.fidelity_fallback
    assert result.profile == "consulting" and result.editing_level == "structured"
    assert "画面写作圆词" in stage.chats[1]["messages"][1]["content"]  # 核对结果随每一段正文一起发出


def test_two_chunk_long_form_keeps_classification_and_skips_vision():
    rows = _transcript(HOSTED_CHUNK_CHARS + 2000)
    raw = "".join(r["text"] for r in rows)
    replies = ['{"profile":"consulting","level":"structured"}', *_replies(2, 4000)]
    settings = _settings(agent_hosted_model="deepseek-flash", agent_vision_enabled=True)
    with mock.patch("engine.agent_client._review_video_captions") as vision, _FakeStage(settings, replies) as stage:
        result = rewrite_transcript(raw, stage.settings, title="素材标题", source_path="source.mp4",
                                    segments=rows, duration_sec=300.0)
    assert len(stage.chats) == HOSTED_BODY_CALL_LIMIT == result.request_count
    assert [call["json_mode"] for call in stage.chats] == [True, False, False]
    vision.assert_not_called()
    assert result.profile == "consulting" and result.editing_level == "structured"
    assert result.markdown.startswith("# 长文标题\n") and not result.fidelity_fallback


def test_short_hosted_transcript_stays_a_single_writer_call():
    rows = _transcript(1000)
    raw = "".join(r["text"] for r in rows)
    replies = ['{"profile":"knowledge","level":"balanced"}', "<!-- FoilPage 标签: 短文 -->\n# 短文标题\n\n" + raw]
    with _FakeStage(_settings(), replies, call_limit=3) as stage:
        result = rewrite_transcript(raw, stage.settings, segments=rows)
    assert len(stage.chats) == 2 == result.request_count and result.tags == ("短文",)
    assert "这是一份长转写" not in stage.chats[1]["messages"][1]["content"]


def test_oversized_hosted_transcript_is_refused_before_any_relay_call():
    rows = _transcript(HOSTED_CHUNK_CHARS * HOSTED_BODY_CALL_LIMIT + 2000)
    raw = "".join(r["text"] for r in rows)
    with pytest.raises(AgentError) as caught, _FakeStage(_settings()) as stage:
        rewrite_transcript(raw, stage.settings, segments=rows)
    assert caught.value.code == "hosted_input_too_long" and stage.chats == []
    assert "请改用自带密钥整理" in str(caught.value)


def test_the_same_transcript_fits_once_the_server_announces_more_slots():
    rows = _transcript(HOSTED_CHUNK_CHARS * HOSTED_BODY_CALL_LIMIT + 2000)
    raw = "".join(r["text"] for r in rows)
    replies = ['{"profile":"knowledge","level":"balanced"}', *_replies(4, 4000)]
    with _FakeStage(_settings(), replies, call_limit=6) as stage:
        result = rewrite_transcript(raw, stage.settings, segments=rows)
    assert [call["json_mode"] for call in stage.chats] == [True, False, False, False, False]
    assert result.request_count == 5 and not result.fidelity_fallback


def test_byok_never_chunks_even_for_very_long_transcripts():
    raw = "自带密钥的整理由用户自己的接口决定超时，" * 900  # 约 18000 字
    settings = {"agent_billing_mode": "byok", "agent_api_key": "fixture", "agent_model": "fixture",
                "agent_profile": "knowledge", "agent_editing_level": "balanced", "agent_vision_enabled": False}
    with mock.patch("engine.agent_client._chat", return_value="# 标题\n\n" + raw) as chat:
        rewrite_transcript(raw, settings)
    assert chat.call_count == 1 and "这是一份长转写" not in chat.call_args.args[2][1]["content"]


def test_unlisted_hosted_model_gives_an_actionable_message():
    """服务端摘下某档（如配额未开通）后仍选着它的用户，看到可操作的提示而不是笼统的失败。"""
    error = AgentError("所选托管模型不可用")
    error.code = "model_unavailable"
    assert agent_failure_metadata(error, "body") == {
        "body_error_code": "model_unavailable",
        "body_error": "所选托管模型当前不可用，请到设置的托管模型里重新选择"}
