# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Independent title generation; no transcript rewriting or secret-bearing errors."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Mapping

from engine.agent_client import AgentError, chat_completion

TITLE_SKILL_VERSION = "concise-accurate-title-v1"


def title_skill_instructions() -> str:
    path = Path(__file__).resolve().parents[1] / "app" / "skills" / "concise-accurate-title" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    return text.split("---", 2)[-1].strip()


def _source_excerpt(text: str, limit: int = 18000) -> str:
    # Keep the beginning, middle and ending rather than silently naming a long
    # talk only from its introduction. The omission is explicit to the model.
    if len(text) <= limit:
        return text
    part = limit // 3
    middle = max(part, len(text) // 2 - part // 2)
    return text[:part] + "\n[中间有省略]\n" + text[middle:middle + part] + "\n[中间有省略]\n" + text[-part:]


def optimize_title(raw_text: str, settings: Mapping[str, object], *, original_title: str = "", cancel_check: Callable[[], bool] | None = None) -> str:
    if not raw_text.strip():
        raise AgentError("转写为空，保留原标题")
    effective = dict(settings)
    if settings.get("title_model"):
        effective["agent_model"] = str(settings["title_model"]).strip()
    effective["agent_temperature"] = 0.2
    extra = str(settings.get("title_skill_prompt") or "").strip()
    prompt = title_skill_instructions()
    if extra:
        prompt += "\n附加命名要求（仍须遵守准确性和输出格式）：\n" + extra
    answer = chat_completion(effective, [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps({"original_title": original_title[:240], "transcript": _source_excerpt(raw_text)}, ensure_ascii=False)},
    ], cancel_check=cancel_check, json_mode=True)
    try:
        value = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", answer.strip()))
        title = value.get("title") if isinstance(value, dict) else None
    except (ValueError, TypeError):
        title = None
    if not isinstance(title, str):
        raise AgentError("标题格式无效，保留原标题")
    title = " ".join(title.split()).strip().strip('“”"')
    if not title or len(title) > 48 or re.search(r"[<>#`\x00-\x1f]", title) or title in {"标题", "新标题", "未命名", "无标题"}:
        raise AgentError("标题不符合命名要求，保留原标题")
    return title
