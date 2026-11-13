# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""TASK-8.24（先生 2026-10-25）：文章翻译——中英互译，逐段对齐。

产物两件，落在转写产物旁：
- ``<stem>_译文_<lang>.md``：整篇译文（整页/替换原文两种阅读方式用）。
- ``<stem>_译文_<lang>.segments.json``：``[{"src":…,"dst":…}]`` 段落对照
  （逐段对照用），段序与源文一致。

计费：托管模式经 AgentStage 走既有 ``body`` 阶段积分（服务端 stage 白名单
仅 body/title，翻译不新开计费面，也不改动生产 Worker）；BYOK 直连用户
自配的 API。翻译阶段元数据不写入任务 manifest 的 body_* 字段——那属于
转写整理阶段，翻译只落 translation_* 字段。
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Mapping

from engine.agent_client import AgentError, AgentStage, chat_completion, settle_agent_stages

TRANSLATION_PROMPT_VERSION = "translation-v1-zh-en"
_MAX_BATCH_CHARS = 8000
_MAX_BATCH_SEGMENTS = 12


def detect_language(text: str) -> str:
    """按 CJK 字符占比粗判语言：过半即视为中文源。"""
    value = str(text or "")
    if not value.strip():
        return "zh"
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", value))
    letters = len(re.findall(r"[A-Za-z]", value))
    if letters == 0:
        return "zh" if cjk else "zh"
    return "zh" if cjk >= letters else "en"


def split_segments(markdown: str) -> list[str]:
    """按空行切块；围栏代码块作为单段整体保留，不参与拆分。"""
    text = str(markdown or "").replace("\r\n", "\n").strip("\n")
    if not text.strip():
        return []
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            current.append(line)
            continue
        if not in_fence and not line.strip():
            if any(part.strip() for part in current):
                blocks.append("\n".join(current).strip("\n"))
            current = []
            continue
        current.append(line)
    if any(part.strip() for part in current):
        blocks.append("\n".join(current).strip("\n"))
    return blocks


def _target_language(source_text: str, direction: str) -> str:
    detected = detect_language(source_text)
    if direction == "zh-en":
        return "en"
    if direction == "en-zh":
        return "zh"
    return "en" if detected == "zh" else "zh"


def _system_prompt(target: str) -> str:
    if target == "en":
        return (
            "你是专业的中文到英文翻译。逐段翻译输入 JSON 数组中的每个字符串。"
            "要求：忠实原意，不增删、不总结；保留 Markdown 结构（标题层级、加粗、列表、"
            "链接、引用）；围栏代码块、行内代码、URL 与邮箱地址原样保留；"
            "HTML 注释整行原样返回；专有名词按通行译法处理。"
            '只输出 JSON：{"translations":["第一段译文","第二段译文",…]}，'
            "数组长度必须与输入完全一致，不得添加解释。"
        )
    return (
        "你是专业的英文到简体中文翻译。逐段翻译输入 JSON 数组中的每个字符串。"
        "要求：忠实原意，不增删、不总结；译文自然流畅，避免翻译腔；"
        "保留 Markdown 结构（标题层级、加粗、列表、链接、引用）；"
        "围栏代码块、行内代码、URL 与邮箱地址原样保留；HTML 注释整行原样返回；"
        "专业术语首次出现时可在括号内保留英文原词。"
        '只输出 JSON：{"translations":["第一段译文","第二段译文",…]}，'
        "数组长度必须与输入完全一致，不得添加解释。"
    )


def _parse_translations(reply: str, count: int) -> list[str]:
    try:
        payload = json.loads(reply)
    except json.JSONDecodeError:
        start, end = reply.find("{"), reply.rfind("}")
        if start < 0 or end <= start:
            raise AgentError("翻译返回格式无效")
        payload = json.loads(reply[start:end + 1])
    values = payload.get("translations") if isinstance(payload, dict) else None
    if not isinstance(values, list) or len(values) != count:
        raise AgentError("翻译段落数与输入不一致")
    cleaned: list[str] = []
    for item in values:
        value = str(item if item is not None else "").strip()
        if not value:
            raise AgentError("翻译存在空段落")
        cleaned.append(value)
    return cleaned


def _translate_batch(settings: Mapping[str, object], segments: list[str], target: str) -> list[str]:
    prompt = (
        f"目标语言：{'英文' if target == 'en' else '简体中文'}。"
        f"逐段翻译以下 {len(segments)} 个段落（JSON 数组顺序即段序）：\n"
        + json.dumps(segments, ensure_ascii=False)
    )
    reply = chat_completion(
        settings,
        [
            {"role": "system", "content": _system_prompt(target)},
            {"role": "user", "content": prompt},
        ],
        json_mode=True,
    )
    return _parse_translations(reply, len(segments))


def translation_paths(raw_path: Path, target: str) -> tuple[Path, Path]:
    stem = raw_path.stem.removesuffix("_raw")
    return (
        raw_path.parent / f"{stem}_译文_{target}.md",
        raw_path.parent / f"{stem}_译文_{target}.segments.json",
    )


def translate_article(
    manifest: Mapping[str, object],
    settings: Mapping[str, object],
    direction: str = "auto",
) -> dict:
    """翻译一篇已完成文本并返回更新后的 manifest 字典（不负责落库）。

    源文优先整理稿，其次原稿。调用方负责持久化与积分结算（hosted 模式经
    ``settings["_agent_stage_settlements"]`` 延迟结算，与批量整理同款）。
    """
    if direction not in ("auto", "zh-en", "en-zh"):
        raise AgentError("翻译方向无效")
    source_value = manifest.get("final_path") or manifest.get("raw_path")
    source = Path(str(source_value or "")).expanduser() if source_value else None
    if source is None or not source.is_file():
        raise AgentError("未找到可翻译的文本")
    try:
        markdown = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise AgentError("文本读取失败") from None
    segments = split_segments(markdown)
    if not segments:
        raise AgentError("文本为空，无法翻译")
    target = _target_language(markdown, direction)
    effective = dict(settings)
    operation_id = str(effective.get("_agent_operation_id") or uuid.uuid4().hex)
    # 计费：托管模式搭 body 阶段（服务端白名单限制），input_text 传全文。
    stage = AgentStage(effective, "body", markdown, operation_id)
    with stage:
        translations: list[str] = []
        batch: list[str] = []
        batch_chars = 0

        def run_batch(current: list[str]) -> list[str]:
            try:
                return _translate_batch(effective, current, target)
            except AgentError:
                # 段数错位偶发：原批重问一次，仍失败才判失败。
                return _translate_batch(effective, current, target)

        for segment in segments:
            if batch and (len(batch) >= _MAX_BATCH_SEGMENTS or batch_chars + len(segment) > _MAX_BATCH_CHARS):
                translations.extend(run_batch(batch))
                batch, batch_chars = [], 0
            batch.append(segment)
            batch_chars += len(segment)
        if batch:
            translations.extend(run_batch(batch))
        article_path, segments_path = translation_paths(Path(str(manifest["raw_path"])), target)
        from app.agent_service import _atomic_write
        _atomic_write(article_path, "\n\n".join(translations) + "\n")
        _atomic_write(segments_path, json.dumps(
            [{"src": src, "dst": dst} for src, dst in zip(segments, translations)],
            ensure_ascii=False, indent=2) + "\n")
    detected = detect_language(markdown)
    updated = dict(manifest)
    updated.update({
        "translation_status": "done",
        "translation_direction": direction if direction != "auto" else f"{detected}-{target}",
        "translation_lang": target,
        "translation_path": str(article_path),
        "translation_segments_path": str(segments_path),
        "translation_error": "",
    })
    return updated


__all__ = [
    "TRANSLATION_PROMPT_VERSION",
    "detect_language",
    "split_segments",
    "translation_paths",
    "translate_article",
    "settle_agent_stages",
]
