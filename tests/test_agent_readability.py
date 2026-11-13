# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
import re
from unittest.mock import patch

import pytest
from engine.agent_client import _editing_level_prompt, _readable_fallback, rewrite_transcript
from engine.settings import SettingsStore, SettingsValidationError


@pytest.mark.parametrize("level", ["light", "balanced", "structured"])
def test_every_level_requests_paragraphs_headings_and_emphasis(level):
    prompt = _editing_level_prompt(level)
    assert "分段" in prompt and "小标题" in prompt and "**加粗**" in prompt


def test_fallback_formats_without_dropping_or_reordering_sentences():
    raw = "我们先记录原文，不必急着概括。每一个例子都值得留下，限定条件也不能删掉。" * 12
    output = _readable_fallback(raw, "记录原文")
    assert "\n\n## " in output and "**" in output
    body = re.sub(r"(?m)^#{1,2} .*\n?", "", output).replace("**", "")
    assert "".join(body.split()) == "".join(raw.split())


def test_editing_level_is_sent_to_model(tmp_path):
    settings = SettingsStore(base_dir=tmp_path).defaults()
    settings.update(agent_api_key="test-key", agent_profile="knowledge", agent_editing_level="structured", agent_vision_enabled=False)
    with patch("engine.agent_client._chat", return_value="# 标题\n\n测试正文。") as chat:
        rewrite_transcript("测试正文。", settings)
    prompt = chat.call_args.args[2][0]["content"]
    assert "结构整理" in prompt and "全部事实、数字、例子与观点逐项保留" in prompt
    assert "简体中文" in prompt and "禁止输出繁体字" in prompt


def test_invalid_editing_level_is_rejected(tmp_path):
    store = SettingsStore(base_dir=tmp_path)
    settings = store.defaults()
    settings["agent_editing_level"] = "summarize"
    with pytest.raises(SettingsValidationError):
        store.validate(settings)


@pytest.mark.parametrize("level", ["light", "balanced", "structured"])
def test_every_level_bans_traditional_output_and_quantifies_retention(level):
    prompt = _editing_level_prompt(level)
    assert "简体中文" in prompt and "禁止输出繁体字" in prompt
    assert "成稿字数不少于原文的" in prompt
    assert "〔待确认：原词 [mm:ss]〕" in prompt and "禁止凭上下文猜测替换" in prompt


def test_light_level_forbids_merging_and_structured_forbids_reordering():
    light = _editing_level_prompt("light")
    structured = _editing_level_prompt("structured")
    assert "禁止合并、删减或改写任何句子" in light
    assert "段落平衡" in light, "段落平衡要求须对所有档位生效"
    assert "删除全部口播痕迹" in structured and "书面文章" in structured
    assert "禁止一句成段" in light and "禁止一句成段" in structured
    assert "书面文本" in _editing_level_prompt("balanced")


def test_smart_level_resolves_via_classifier(tmp_path):
    """agent_editing_level=auto 时由分类请求一并给出力度，写进结果与提示词。"""
    from engine.agent_client import rewrite_transcript

    settings = SettingsStore(base_dir=tmp_path).defaults()
    settings.update(agent_api_key="test-key", agent_profile="knowledge",
                    agent_editing_level="auto", agent_vision_enabled=False)
    classify = '{"profile":"knowledge","level":"structured"}'

    def fake_chat(_settings, _key, messages, **_kwargs):
        if messages[0]["content"].startswith("你是内容分类器"):
            return classify
        return "# 标题" + chr(10) + chr(10) + "测试正文。"

    with patch("engine.agent_client._chat", side_effect=fake_chat) as chat:
        result = rewrite_transcript("测试正文，内容较长。" * 30, settings)
    writer_prompt = [c for c in chat.call_args_list if c.args[2][0]["content"].startswith("你是 FoilPage")][0].args[2][0]["content"]
    assert "结构整理（成文档）" in writer_prompt
    assert result.editing_level == "structured"
    assert chat.call_count == 2


def test_fixed_level_overrides_classifier_suggestion(tmp_path):
    from engine.agent_client import rewrite_transcript

    settings = SettingsStore(base_dir=tmp_path).defaults()
    settings.update(agent_api_key="test-key", agent_profile="knowledge",
                    agent_editing_level="light", agent_vision_enabled=False)

    def fake_chat(_settings, _key, messages, **_kwargs):
        if messages[0]["content"].startswith("你是内容分类器"):
            return '{"profile":"knowledge","level":"structured"}'
        return "# 标题" + chr(10) + chr(10) + "测试正文。"

    with patch("engine.agent_client._chat", side_effect=fake_chat) as chat:
        result = rewrite_transcript("测试正文，内容较长。" * 30, settings)
    writer_prompt = chat.call_args_list[-1].args[2][0]["content"]
    assert "轻整理（口播整理档）" in writer_prompt and "结构整理" not in writer_prompt.split("轻整理")[0]
    assert result.editing_level == "light"


def test_auto_level_is_a_valid_setting(tmp_path):
    store = SettingsStore(base_dir=tmp_path)
    settings = store.defaults()
    settings["agent_editing_level"] = "auto"
    store.validate(settings)  # 不抛即通过


def test_timestamped_source_attaches_locators_and_falls_back():
    from engine.agent_client import _timestamped_source

    raw = "第一句话。第二句话。"
    segments = [{"start": 0.0, "end": 3.5, "text": "第一句话。"},
                {"start": 3.5, "end": 8.0, "text": "第二句话。"}]
    stamped = _timestamped_source(raw, segments)
    assert stamped.startswith("[0:00-0:03] 第一句话。")
    assert "[0:03-0:08] 第二句话。" in stamped
    # segments 与原文不一致（旧/部分 sidecar）时必须回退纯文本，绝不改词
    mismatched = [{"start": 0.0, "end": 3.5, "text": "完全不同的内容。"}]
    assert _timestamped_source(raw, mismatched) == raw
    assert _timestamped_source(raw, None) == raw


def test_timestamped_source_merges_very_long_transcripts():
    from engine.agent_client import _timestamped_source

    parts = [f"第{i}句。" for i in range(1200)]
    segments = [{"start": float(i), "end": float(i + 1), "text": text} for i, text in enumerate(parts)]
    stamped = _timestamped_source("".join(parts), segments)
    assert stamped.count(chr(10)) < 700  # 超过 600 段时按相邻合并约束行数


def test_traditional_output_is_converted_to_simplified_in_code(tmp_path):
    """qwen 系快照会无视简体指令（2026-11-02 实测）：opencc 是最终保证。"""
    from engine.agent_client import rewrite_transcript

    settings = SettingsStore(base_dir=tmp_path).defaults()
    settings.update(agent_api_key="test-key", agent_profile="knowledge",
                    agent_editing_level="balanced", agent_vision_enabled=False)
    traditional = "# 標題" + chr(10) + chr(10) + "這是一段繁體正文，測試兌底轉換。"
    with patch("engine.agent_client._chat", return_value=traditional):
        result = rewrite_transcript("这是一段正文测试。", settings)
    assert "繁体正文" in result.markdown and "這" not in result.markdown


def test_heading_completeness_is_constrained():
    prompt = _editing_level_prompt("structured")
    assert "不得从句子中间截断起题" in prompt


def test_particle_zhu_is_converted_but_word_zhu_is_kept():
    """opencc 盲区：助词"著"须转"着"，显著/著作等词的"著"保留。"""
    from engine.agent_client import _simplified_chinese

    out = _simplified_chinese("他看著石頭，顯著的成效，著名著作，急著出門")
    assert "看着" in out and "急着" in out
    assert "显著" in out and "著名" in out and "著作" in out
    assert "著" not in out.replace("显著", "").replace("著名", "").replace("著作", "")


def test_number_form_and_silent_rewrite_rules_are_in_prompt():
    prompt = _editing_level_prompt("balanced")
    assert "阿拉伯数字不得改写为中文数字" in prompt
    assert "静默改写" in prompt
    assert "照抄该词" in prompt and "所在行的行首定位标记" in prompt


def test_doubt_markers_get_deterministic_timestamp_backfill():
    """DS 无视时间标记指令：由代码按 segments 行首定位回填。"""
    from engine.agent_client import _backfill_doubt_timestamps

    timestamped = "[0:12-0:15] 第一句话有存疑词阿尔法。" + chr(10) + "[1:03-1:08] 第二句话没存疑。"
    markdown = "第一段〔待确认：阿尔法〕。结尾〔待确认：找不到的词〕。"
    out = _backfill_doubt_timestamps(markdown, timestamped)
    assert "〔待确认：阿尔法 [0:12-0:15]〕" in out
    assert "〔待确认：找不到的词〕" in out, "定位不到的词保持原样"
    # 已带时间的标注不重复处理
    assert _backfill_doubt_timestamps("〔待确认：x [2:21]〕", timestamped) == "〔待确认：x [2:21]〕"
    # 模型整段照抄区间时同样视为已带标记
    assert _backfill_doubt_timestamps("〔待确认：x [0:10-0:30]〕", timestamped) == "〔待确认：x [0:10-0:30]〕"
    # 无时间戳源时不处理
    assert _backfill_doubt_timestamps("〔待确认：阿尔法〕", "没有定位标记的原文") == "〔待确认：阿尔法〕"


def test_fidelity_floor_is_tier_aware():
    """高填充口播的合法成文不因固定 55% 线被误判回退。"""
    from engine.agent_client import _FIDELITY_FLOORS, _needs_fidelity_fallback

    filler = "呃 这个 就是说 对吧 " * 40
    core = "本地识别可以在家用电脑运行，成本每次几分钱，隐私不出本机。"
    raw = filler + core + filler
    compact = "# T" + chr(10) + chr(10) + core
    # 现状旧行为（无档位）：误判
    assert _needs_fidelity_fallback(raw, compact) is True
    # 档位感知：structured 允许到 60%，此例成文约 9% 仍拦（真摘要）；
    # 验证方向：balanced(0.70) 线 < 旧 0.55？否——线更高更严，正确性在于随档放宽
    assert _FIDELITY_FLOORS == {"light": 0.85, "balanced": 0.70, "structured": 0.60, "rewrite": 0.35}
    # 混合文（约 65% 为实质内容）：合法成文比例 ~0.62，structured(0.60) 不误伤，light(0.85) 拦截
    mixed = "呃 这个 对吧 " * 10 + core * 5
    article = "# T" + chr(10) + chr(10) + core * 5
    assert _needs_fidelity_fallback(mixed, article, _FIDELITY_FLOORS["structured"]) is False
    assert _needs_fidelity_fallback(mixed, article, _FIDELITY_FLOORS["light"]) is True


def test_fabricated_numbers_trigger_verbatim_fallback(tmp_path):
    """对抗输入让模型编造规格与价格：≥2 个新数字直接降级逐字排版。"""
    from unittest.mock import patch
    from engine.agent_client import rewrite_transcript

    settings = SettingsStore(base_dir=tmp_path).defaults()
    settings.update(agent_api_key="k", agent_profile="knowledge",
                    agent_editing_level="structured", agent_vision_enabled=False)
    raw = "大家好讲三件事。本地识别用显卡就能跑。成本每次几分钱。" * 5
    fabricated = ("# 标题" + chr(10) + chr(10)
                  + "消费级 GPU（如 RTX 3060）即可，约 0.03 至 0.08 元/次。" * 8)
    with patch("engine.agent_client._chat", return_value=fabricated):
        result = rewrite_transcript(raw, settings)
    assert result.fidelity_fallback is True
    assert "3060" not in result.markdown and "0.03" not in result.markdown
    # 逐字回退保留全部原文
    assert "本地识别用显卡就能跑" in result.markdown


def test_single_number_correction_survives_fabrication_floor(tmp_path):
    """单个合理纠错（如 40节气→24节气）不触发回退。"""
    from unittest.mock import patch
    from engine.agent_client import rewrite_transcript

    settings = SettingsStore(base_dir=tmp_path).defaults()
    settings.update(agent_api_key="k", agent_profile="knowledge",
                    agent_editing_level="balanced", agent_vision_enabled=False)
    raw = "讲农事知识。当40节气土壤错误被时代化与抛弃。滋养万物。" * 8
    corrected = ("# 标题" + chr(10) + chr(10)
                 + "讲农事知识。当 24节气 土壤错误被时代化与抛弃。滋养万物。" * 8)
    with patch("engine.agent_client._chat", return_value=corrected):
        result = rewrite_transcript(raw, settings)
    assert result.fidelity_fallback is False
    assert "24节气" in result.markdown


def test_doubt_marker_bar_and_cap_are_in_prompt():
    """2026-11-04 病例：5182 字成稿带 14 处待确认（含"鸡蛋""千万"这类常用词）。
    标注必须抬高门槛并限量，专有名词通顺即放行。"""
    prompt = _editing_level_prompt("balanced")
    assert "不是免责工具" in prompt
    assert "每千字 2 处" in prompt and "不足千字最多 3 处" in prompt
    assert "通顺合理的专有名词直接使用" in prompt
    system = _writer_system_prompt_for_test()
    assert "有把握时按语境直接修正、无需标注" in system


def _writer_system_prompt_for_test() -> str:
    from engine.agent_client import _writer_system_prompt
    return _writer_system_prompt("knowledge")


def test_fallback_rebuilds_paragraphs_from_segments():
    """whisper 中文转写常整篇无标点（2026-11-03 病例：2094 字单块）。
    有字幕分段时按分段重建段落，绝不再产出整屏大段。"""
    seg_text = "今天讲三个重点第一个重点是费率第二个重点是合规第三个重点是到账" * 8
    unit = len(seg_text) // 4
    segments = [
        {"start": i * 5.0, "end": (i + 1) * 5.0, "text": seg_text[i * unit:(i + 1) * unit]}
        for i in range(4)
    ]
    raw = seg_text
    output = _readable_fallback(raw, "费率与合规", segments)
    body = [p for p in output.split("\n") if p.strip() and not p.startswith("#")]
    assert len(body) >= 2, "分段信息必须用于切分段落"
    joined = "".join(re.sub(r"\*+", "", "".join(body)).split())
    assert joined == "".join(raw.split()), "回退不得丢字或改字"


def test_fallback_hard_splits_unpunctuated_wall():
    raw = "这是一整段没有任何标点的长转写内容" * 30  # 480 字无任何标点
    output = _readable_fallback(raw, "无标点")
    body = [p for p in output.split("\n") if p.strip() and not p.startswith("#")]
    assert len(body) >= 2, "无标点长文必须硬切段落"
    assert all(len(p) <= 220 for p in body)
    joined = "".join(re.sub(r"\*+", "", "".join(body)).split())
    assert joined == "".join(raw.split())


def test_chinese_numeral_conversion_is_not_fabrication():
    """费率视频口播"百分之三十八"成文写 38%/0.38 是格式换算，不是编造；
    真编造（源里无可换算数字）仍然拦截。"""
    from engine.agent_client import _fabricated_numbers

    raw = "这个通道的费率是百分之三十八结算是千分之五到账要扣零点三八"
    markdown = "费率是 38%（0.38），结算 0.5%，到账扣 0.38。"
    assert _fabricated_numbers(raw, markdown) == []

    raw2 = "今天聊聊火锅底料怎么做才香"
    fabricated = _fabricated_numbers(raw2, "每单成本约 0.03 至 0.08 元。")
    assert "0.03" in fabricated and "0.08" in fabricated


def test_digit_by_digit_chinese_numbers_are_valid_format_changes():
    from engine.agent_client import _fabricated_numbers
    raw = "二零二六年，编号一二三四五，邮编六一零零零零，金额二点三五元。"
    assert _fabricated_numbers(raw, "2026年，编号12345，邮编610000，金额2.35元。") == []
    assert _fabricated_numbers(raw, "额外成本98.76元、54.32元。") == ["54.32", "98.76"]


@pytest.mark.parametrize("delimiter", ["，", "、", ","])
def test_fallback_preserves_punctuation_at_long_paragraph_cut(delimiter):
    raw = "这是需要保留的原文" * 19 + delimiter + "后面的论述也必须完整保留" * 22
    output = _readable_fallback(raw, "长文")
    body = re.sub(r"(?m)^#{1,2} .*\n?", "", output).replace("**", "")
    assert "".join(body.split()) == "".join(raw.split())
